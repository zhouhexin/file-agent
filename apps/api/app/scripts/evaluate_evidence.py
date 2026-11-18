"""固定证据包的规则/回放/真实模型评测；默认只运行规则，不连接模型或数据库。"""

from __future__ import annotations

import argparse
from copy import copy, deepcopy
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from app.core.config import get_settings
from app.modules.evidence_answer.schemas import EvidencePackage
from app.modules.evidence_evaluation.frozen_inputs import (
    RecordedSemanticClient, RecordingSemanticClient, digest, runtime_config, validate_versions, write_new_json,
)
from app.modules.evidence_evaluation.schemas import StrictModel
from app.modules.evidence_evaluation.semantic_checker import SemanticChecker
from app.modules.evidence_evaluation.service import EvidenceEvaluationService, build_semantic_checker
from app.modules.evidence_evaluation.version_checker import VersionChecker, VersionSnapshot
from app.modules.llm.client import LLMResponseError

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "tests/fixtures/evidence_evaluation_cases.json"


class ExpectedIssue(StrictModel):
    """独立标注的问题类型与引用，用于核对检测和引用定位。"""

    type: Literal["MISSING", "CONFLICT", "OUTDATED"]
    evidence_ids: list[str]


class EvaluationCase(StrictModel):
    """每条样本保存问题、证据、冻结版本事实与人工可审阅的预期解释。"""

    case_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    group: Literal["NORMAL", "MISSING", "CONFLICT", "OUTDATED"]
    package: EvidencePackage
    versions: dict[str, dict[str, str | None]]
    expected_issues: list[ExpectedIssue]
    expected_explanation: str
    recorded_semantic_response: dict[str, Any] | None = None
    recorded_semantic_calls: list[dict[str, Any]] | None = None
    temporal_focus: Literal["CURRENT", "HISTORICAL", "UNKNOWN"] | None = None
    original_question: str | None = None
    input_sha256: str | None = None
    capture_config: dict[str, Any] = Field(default_factory=dict)
    source: Literal["synthetic", "real", "derived"] = "synthetic"

    @model_validator(mode="after")
    def validate_frozen(self):
        validate_versions(self.package, self.versions)
        allowed = {i.evidence_id for i in self.package.evidence_items}
        for issue in self.expected_issues:
            if not set(issue.evidence_ids) <= allowed:
                raise ValueError("GOLD_UNKNOWN_EVIDENCE_ID")
            if issue.type == "CONFLICT" and len(set(issue.evidence_ids)) < 2:
                raise ValueError("GOLD_CONFLICT_NEEDS_PAIR")
            if issue.type == "OUTDATED" and not issue.evidence_ids:
                raise ValueError("GOLD_OUTDATED_NEEDS_EVIDENCE")
        if (self.group == "NORMAL") != (not self.expected_issues):
            raise ValueError("GOLD_GROUP_INCONSISTENT")
        if self.expected_issues and self.group not in {i.type for i in self.expected_issues}:
            raise ValueError("GOLD_GROUP_INCONSISTENT")
        if self.input_sha256 is not None:
            if self.original_question is None or self.temporal_focus is None:
                raise ValueError("FROZEN_CONTEXT_REQUIRED")
            actual = digest({"package": self.package.model_dump(mode="json"), "versions": self.versions,
                             "context": {"original_question": self.original_question,
                                         "temporal_focus": self.temporal_focus}})
            if actual != self.input_sha256:
                raise ValueError("FROZEN_INPUT_HASH_MISMATCH")
        return self


class ReplaySemanticClient:
    """只重放固定模型响应，验证工程协议；回放分数不是模型准确率。"""

    def __init__(self, response: dict[str, Any]) -> None:
        """冻结响应与样本标签独立存放，调用方不得把标签发给真实模型。"""
        self.response = response
        self.called = False

    def complete_json_with_usage(self, **_kwargs: Any) -> tuple[dict, dict]:
        """零网络、零计费；不虚构 token 用量。"""
        if self.called:
            raise LLMResponseError("REPLAY_REQUIRES_PER_CALL_RESPONSES")
        self.called = True
        return deepcopy(self.response), {"input_tokens": 0, "output_tokens": 0}


def load_cases(path: Path = DEFAULT_DATASET) -> list[EvaluationCase]:
    """校验冻结数据，防止样本缺字段时静默参与评分。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = [EvaluationCase.model_validate(item) for item in data["cases"]]
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise ValueError("EMPTY_OR_DUPLICATE_CASES")
    if data.get("manifest") is not None:
        if data["manifest"].get("cases_sha256") != digest(data["cases"]):
            raise ValueError("DATASET_CASES_HASH_MISMATCH")
        if data["manifest"].get("annotation_sha256") != digest(data.get("annotations")):
            raise ValueError("DATASET_GOLD_HASH_MISMATCH")
    return cases


def _signature(issues) -> list[tuple[str, tuple[str, ...]]]:
    return sorted((item.type, tuple(sorted(item.evidence_ids))) for item in issues)


def _percentile(values: list[int], q: float) -> float | None:
    """与常见inclusive百分位口径一致；单条也有定义。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def run_evaluation(
    cases: list[EvaluationCase], *, mode: str = "replay", repetitions: int = 2,
    live_checker: SemanticChecker | None = None, config: dict | None = None,
) -> dict[str, Any]:
    """逐类评分、引用追溯、稳定性和延迟；失败也计入总样本，不当作通过。"""
    if mode not in {"replay", "rules", "live"} or repetitions < 1 or not cases:
        raise ValueError("评测模式或重复次数无效")
    if mode == "live" and live_checker is None:
        raise ValueError("live 模式必须显式配置模型")
    cases = [EvaluationCase.model_validate(c.model_dump()) for c in cases]
    if mode == "replay" and any(c.recorded_semantic_calls is None and c.recorded_semantic_response is None for c in cases):
        raise ValueError("REPLAY_RECORDING_REQUIRED")
    totals = {kind: dict(tp=0, fp=0, fn=0) for kind in ("MISSING", "CONFLICT", "OUTDATED")}
    records, durations, round_results = [], [], []
    traceable = citations = calls = input_tokens = output_tokens = stable = 0
    failures = partial = mismatches = false_positives = 0
    usage_complete = True
    for case in cases:
        snapshots = validate_versions(case.package, case.versions)
        results, call_records = [], []
        for _ in range(repetitions):
            semantic = copy(live_checker) if mode == "live" else None
            if mode != "live":
                client = ((RecordedSemanticClient(case.recorded_semantic_calls) if case.recorded_semantic_calls is not None
                           else ReplaySemanticClient(case.recorded_semantic_response)) if mode == "replay" else None)
                budgets = {k: case.capture_config[k] for k in ("max_input_chars", "max_calls", "max_output_tokens")
                           if k in case.capture_config}
                semantic = SemanticChecker(client=client, **budgets)
            recorder = RecordingSemanticClient(semantic.client) if semantic.client is not None else None
            if recorder is not None:
                semantic.client = recorder
            evaluator = EvidenceEvaluationService(version_checker=VersionChecker(lambda _p: snapshots), semantic_checker=semantic)
            results.append(evaluator.evaluate(case.package.model_copy(deep=True), temporal_focus=case.temporal_focus))
            call_records.append(recorder.calls if recorder is not None else [])
            if mode == "replay" and isinstance(client, RecordedSemanticClient):
                if client.alignment_error:
                    raise ValueError(client.alignment_error)
                if client.position != len(client.calls):
                    raise ValueError("REPLAY_UNUSED_CALLS")
        round_results.append(results)
        result = results[0]
        signature = _signature(result.issues)
        stable += int(all((_signature(r.issues), r.evaluation_status) == (signature, result.evaluation_status)
                          for r in results))
        exact = signature == _signature(case.expected_issues)
        mismatches += int(not exact)
        expected = {issue.type for issue in case.expected_issues}
        predicted = {issue.type for issue in result.issues}
        for kind, counts in totals.items():
            counts["tp"] += int(kind in expected and kind in predicted)
            counts["fp"] += int(kind not in expected and kind in predicted)
            counts["fn"] += int(kind in expected and kind not in predicted)
        false_positives += int(not expected and bool(predicted))
        failures += int(result.evaluation_status == "EVALUATION_FAILED")
        partial += int(result.evaluation_status == "PARTIAL")
        allowed = {item.evidence_id for item in case.package.evidence_items}
        for item in result.issues:
            for eid in item.evidence_ids:
                citations += 1
                traceable += int(eid in allowed)
        for repeat in results:
            durations.append(repeat.duration_ms)
            calls += repeat.semantic_check.llm_calls
            input_tokens += repeat.semantic_check.input_tokens
            output_tokens += repeat.semantic_check.output_tokens
            usage_complete = usage_complete and repeat.semantic_check.token_usage_complete
        records.append(dict(case_id=case.case_id, source=case.source, input_sha256=case.input_sha256,
                            issue_signature_matches=exact,
                            matches_annotation=exact and result.evaluation_status == "COMPLETED",
                            expected=[i.model_dump() for i in case.expected_issues], result=result.model_dump(),
                            rounds=[dict(repetition=n, result=r.model_dump(), semantic_calls=call_records[n-1])
                                    for n, r in enumerate(results, 1)]))
    metrics = {}
    for kind, counts in totals.items():
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        metrics[kind] = dict(
            **counts, precision=tp / (tp + fp) if tp + fp else None,
            recall=tp / (tp + fn) if tp + fn else None,
            f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
        )
    durations.sort()
    per_round = []
    for index in range(repetitions):
        scores = {k: dict(tp=0, fp=0, fn=0) for k in ("MISSING", "CONFLICT", "OUTDATED", "NORMAL")}
        states = {"COMPLETED": 0, "PARTIAL": 0, "EVALUATION_FAILED": 0}
        for case, results in zip(cases, round_results):
            result = results[index]
            states[result.evaluation_status] = states.get(result.evaluation_status, 0) + 1
            expected = {i.type for i in case.expected_issues} or {"NORMAL"}
            predicted = {i.type for i in result.issues}
            if result.evaluation_status == "COMPLETED" and not predicted:
                predicted = {"NORMAL"}
            for kind, counts in scores.items():
                counts["tp"] += int(kind in expected and kind in predicted)
                counts["fp"] += int(kind not in expected and kind in predicted)
                counts["fn"] += int(kind in expected and kind not in predicted)
        for counts in scores.values():
            tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
            counts.update(precision=tp/(tp+fp) if tp+fp else None, recall=tp/(tp+fn) if tp+fn else None,
                          f1=2*tp/(2*tp+fp+fn) if 2*tp+fp+fn else None)
        per_round.append(dict(repetition=index+1, per_class=scores, execution_states=states))
    all_complete = [rs for rs in round_results if all(r.evaluation_status == "COMPLETED" for r in rs)]
    metrics["NORMAL"] = per_round[0]["per_class"]["NORMAL"]
    return dict(
        mode=mode, evaluator_version="evidence-eval-v1", sample_count=len(cases), repetitions=repetitions,
        dataset_sha256=digest([c.model_dump(mode="json") for c in cases]),
        runtime_config=config or {"mode": mode}, runtime_config_sha256=digest(config or {"mode": mode}),
        capture_config_matches_runtime={c.case_id: digest(c.capture_config) == digest(config)
                                        if config is not None else None for c in cases},
        measurement=("工程回放；语义响应已冻结，不能作为真实模型准确率" if mode == "replay"
                     else "仅技术版本规则；语义未评估" if mode == "rules"
                     else "真实模型在固定证据包上的测量；样本来源：" + ",".join(sorted({c.source for c in cases}))),
        per_round=per_round,
        exact_complete_matches=sum(r["matches_annotation"] for r in records),
        per_class=metrics, exact_issue_mismatches=mismatches, evaluation_failures=failures,
        partial_evaluations=partial, normal_false_positives=false_positives,
        normal_false_positive_rate=false_positives / max(1, sum(not c.expected_issues for c in cases)),
        citation_count=citations, traceability_rate=traceable / citations if citations else None,
        repeated_stability_rate=stable / len(cases) if cases and repetitions > 1 else None,
        completed_repeated_stability_rate=(sum(all((_signature(r.issues), r.evaluation_status) ==
            (_signature(rs[0].issues), rs[0].evaluation_status) for r in rs) for rs in all_complete) / len(all_complete)
            if all_complete and repetitions > 1 else None),
        llm_calls=calls, input_tokens=input_tokens, output_tokens=output_tokens,
        token_usage_complete=usage_complete,
        p50_duration_ms=_percentile(durations, .5),
        p95_duration_ms=_percentile(durations, .95),
        cases=records,
    )


def main() -> None:
    """默认仅运行规则；--mode live 是额外模型调用的显式授权入口。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--mode", choices=("replay", "rules", "live"), default="rules")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("OUTPUT_ALREADY_EXISTS；请使用新文件名")
    cases = load_cases(args.dataset)
    live_checker = configuration = None
    if args.mode == "live":
        settings = get_settings()
        if not settings.llm_enabled:
            parser.error("live 模式还需要显式配置 LLM_ENABLED=true")
        live_checker = build_semantic_checker(settings)
        configuration = runtime_config(settings)
    report = run_evaluation(cases, mode=args.mode,
                            repetitions=args.repetitions, live_checker=live_checker, config=configuration)
    report["dataset_manifest"] = json.loads(args.dataset.read_text()).get("manifest")
    write_new_json(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # 不在控制台转储Pydantic输入值、数据库DSN或模型异常原文。
        import re
        import sys
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[A-Z_]+", str(exc)) else type(exc).__name__
        print(f"评测失败：{code}", file=sys.stderr)
        raise SystemExit(2)
