"""只读整理已完成的固定输入报告；不调用模型、不连接数据库、不改变Shadow链路。"""

import hashlib
import json
from pathlib import Path

from app.modules.evidence_evaluation.frozen_inputs import digest, private_dataset_path, write_new_json, TEST_ROOT
from app.modules.evidence_evaluation.schemas import EvaluationResult
from app.modules.evidence_evaluation.validator import validate_issues
from app.modules.evidence_harness.schemas import (
    CaseStatistics, FeedbackPacket, ReportFeedback, RoundFeedback, RuntimeBudget,
)
from app.modules.llm.diagnostics import SafeErrorDiagnostic
from app.scripts.evaluate_evidence import load_cases


def _dataset_file(path: Path) -> Path:
    """除数据集子路径检查外，拒绝整个datasets根被符号链接替换到测试区外。"""
    path = private_dataset_path(path)
    if not path.is_relative_to(TEST_ROOT.resolve()):
        raise ValueError("HARNESS_INPUT_OUTSIDE_TEST_ROOT")
    return path


def _read(path: Path) -> tuple[dict, str]:
    """限制源文件大小并拒绝重复键/非有限数，错误不转储报告原文。"""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError("NONFINITE_JSON_NUMBER")

    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("REPORT_TOO_LARGE")
    raw = path.read_bytes()
    data = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(data, dict):
        raise ValueError("REPORT_OBJECT_REQUIRED")
    return data, hashlib.sha256(raw).hexdigest()


def _diagnostics(checks: list[dict]) -> list[SafeErrorDiagnostic]:
    """诊断仅允许既有枚举与计数，丢弃自由message、响应正文及任意来源字段。"""
    result = []
    for check in checks:
        for raw in check.get("error_diagnostics", []):
            item = {key: raw[key] for key in SafeErrorDiagnostic.model_fields if key in raw}
            # 旧日志可带固定summary；仍按严格形态schema投影，不透传该文本。
            if isinstance(item.get("response_shape"), dict):
                from app.modules.llm.response_shape import ResponseShape
                item["response_shape"] = {
                    k: v for k, v in item["response_shape"].items() if k in ResponseShape.model_fields
                }
            result.append(SafeErrorDiagnostic.model_validate(item))
    return result


def _round(raw: dict, *, report_ref: str, case, repetition: int) -> RoundFeedback:
    """按每轮真实状态生成信号；用量不完整时用null，不把失败0当零消耗。"""
    # 在内存校验完整结果，但不复制reason、requirements、quote或供应商自由文本。
    result = EvaluationResult.model_validate(raw)
    if result.evaluated_evidence_fingerprint != case.package.evidence_fingerprint:
        raise ValueError("ROUND_EVIDENCE_MISMATCH")
    if result.issues != result.version_check.issues + result.semantic_check.issues:
        raise ValueError("ROUND_ISSUES_INCONSISTENT")
    checks = [raw["version_check"], raw["semantic_check"]]
    states = {result.version_check.status, result.semantic_check.status}
    expected = (
        "EVALUATION_FAILED" if "EVALUATION_FAILED" in states
        else "COMPLETED" if states == {"COMPLETED"} else "PARTIAL"
    )
    if result.evaluation_status != "DISABLED" and result.evaluation_status != expected:
        raise ValueError("INCONSISTENT_EVALUATION_STATE")
    if min(result.duration_ms, result.semantic_check.llm_calls,
           result.semantic_check.input_tokens, result.semantic_check.output_tokens) < 0:
        raise ValueError("NEGATIVE_REPORT_COUNTER")
    validate_issues(result.issues, case.package)
    available = result.evaluation_status == "COMPLETED"
    issue_types = sorted({issue.type for issue in result.issues}) if available else []
    diagnostics = _diagnostics(checks)
    failure_kinds = set()
    for diagnostic in diagnostics:
        failure_kinds.add(diagnostic.category)
        if diagnostic.response_shape and diagnostic.response_shape.output_truncated:
            failure_kinds.add("OUTPUT_TRUNCATED")
    if result.evaluation_status == "EVALUATION_FAILED" and not failure_kinds:
        failure_kinds.add("UNCLASSIFIED_FAILURE")
    semantic = result.semantic_check
    return RoundFeedback(
        feedback_id=digest([report_ref, case.case_id, repetition]), case_ref=digest(case.case_id),
        repetition=repetition, evaluation_status=result.evaluation_status,
        version_status=result.version_check.status, semantic_status=semantic.status,
        semantic_judgment_available=available,
        semantic_outcome=("ISSUES_DETECTED" if issue_types else "NO_ISSUES_DETECTED") if available else "UNAVAILABLE",
        issue_types=issue_types, duration_ms=result.duration_ms, llm_calls=semantic.llm_calls,
        input_tokens=semantic.input_tokens if semantic.token_usage_complete else None,
        output_tokens=semantic.output_tokens if semantic.token_usage_complete else None,
        token_usage_complete=semantic.token_usage_complete,
        failure_kinds=sorted(failure_kinds), diagnostics=diagnostics,
    )


def _report(data: dict, report_ref: str, cases: list, dataset: dict, dataset_sha: str) -> ReportFeedback:
    """验证报告与固定输入绑定；不信任顶层首轮指标或模型自述的优化效果。"""
    if data["dataset_sha256"] != dataset_sha or digest(data["dataset_manifest"]) != digest(dataset["manifest"]):
        raise ValueError("REPORT_DATASET_MISMATCH")
    config = data["runtime_config"]
    if digest(config) != data["runtime_config_sha256"]:
        raise ValueError("REPORT_CONFIG_HASH_MISMATCH")
    # 第一步只接受真实live历史；回放结果不能冒充付费实验或有效策略尝试。
    if data["mode"] != "live" or data["evaluator_version"] != "evidence-eval-v1":
        raise ValueError("LIVE_REPORT_REQUIRED")
    repetitions = data["repetitions"]
    if type(repetitions) is not int or not 1 <= repetitions <= 100:
        raise ValueError("INVALID_REPETITIONS")
    rows = data["cases"]
    by_id = {row["case_id"]: row for row in rows}
    if len(by_id) != len(rows) or set(by_id) != {c.case_id for c in cases} or data["sample_count"] != len(cases):
        raise ValueError("REPORT_CASE_SET_MISMATCH")
    trials = []
    for case in cases:
        row = by_id[case.case_id]
        if row["input_sha256"] != case.input_sha256:
            raise ValueError("REPORT_INPUT_MISMATCH")
        rounds = row["rounds"]
        indices = [r["repetition"] for r in rounds]
        if any(type(i) is not int for i in indices) or sorted(indices) != list(range(1, repetitions + 1)):
            raise ValueError("REPORT_ROUNDS_INCOMPLETE")
        for round_data in sorted(rounds, key=lambda r: r["repetition"]):
            trials.append(_round(round_data["result"], report_ref=report_ref, case=case,
                                 repetition=round_data["repetition"]))
    completed = sum(t.evaluation_status == "COMPLETED" for t in trials)
    failed = sum(t.evaluation_status == "EVALUATION_FAILED" for t in trials)
    budget = RuntimeBudget(**{name: config[name] for name in RuntimeBudget.model_fields})
    # 条件指纹保留模型/提示词/代码/依赖等，只排除A阶段允许调整的两项预算。
    context = {k: v for k, v in config.items() if k not in {"max_output_tokens", "timeout_seconds"}}
    condition_ref = digest([dataset_sha, dataset["manifest"]["annotation_sha256"], context])
    return ReportFeedback(
        report_ref=report_ref, mode="live", config_sha256=data["runtime_config_sha256"],
        condition_ref=condition_ref, attempt_ref=digest([condition_ref, budget.model_dump()]),
        model_ref=digest(config["model"]), endpoint_ref=config["endpoint_sha256"],
        prompt_sha256=config["semantic_prompt_sha256"], evaluator_version=data["evaluator_version"],
        code_ref=digest(config["code_sha256"]), budget=budget, run_count=len(trials),
        completed_count=completed, failed_count=failed, partial_count=len(trials) - completed - failed,
        observation="HAS_FAILURES" if failed else "ALL_COMPLETED" if completed == len(trials) else "INCOMPLETE",
        trials=trials,
    )


def build_feedback(*, dataset_path: Path, baseline_path: Path, history_paths: list[Path]) -> tuple[FeedbackPacket, dict]:
    """只从独立测试datasets读取；返回安全反馈和仅供本机追溯的来源索引。"""
    dataset_path = _dataset_file(dataset_path)
    dataset, dataset_file_sha = _read(dataset_path)
    cases = load_cases(dataset_path)
    if not dataset.get("manifest") or any(case.input_sha256 is None for case in cases):
        raise ValueError("FROZEN_DATASET_REQUIRED")
    dataset_sha = digest([case.model_dump(mode="json") for case in cases])
    reports, refs, sources = [], set(), []
    baseline_ref = None
    for path in [*history_paths, baseline_path]:
        path = _dataset_file(path)
        raw, report_ref = _read(path)
        if report_ref in refs:
            raise ValueError("DUPLICATE_REPORT")
        refs.add(report_ref)
        reports.append(_report(raw, report_ref, cases, dataset, dataset_sha))
        sources.append({"report_ref": report_ref, "source": path.relative_to(TEST_ROOT).as_posix()})
        baseline_ref = report_ref
    packet = FeedbackPacket(
        dataset_sha256=dataset_sha, annotation_sha256=dataset["manifest"]["annotation_sha256"],
        baseline_ref=baseline_ref,
        cases=[CaseStatistics(case_ref=digest(c.case_id), input_sha256=c.input_sha256,
                              evidence_count=len(c.package.evidence_items), question_chars=len(c.package.question),
                              quote_chars=sum(len(e.quote) for e in c.package.evidence_items)) for c in cases],
        reports=reports,
    )
    index = {"dataset_source": dataset_path.relative_to(TEST_ROOT).as_posix(), "dataset_file_sha256": dataset_file_sha,
             "reports": sources, "cases": [{"case_ref": digest(c.case_id), "case_id": c.case_id} for c in cases]}
    return packet, index


def prepare_feedback(*, dataset_path: Path, baseline_path: Path, history_paths: list[Path], output: Path) -> dict:
    """在受控私有目录新增反馈产物；全部校验通过后才创建，不覆盖历史。"""
    output = output.resolve()
    allowed = (TEST_ROOT / "experiments/harness-v1").resolve()
    if not allowed.is_relative_to(TEST_ROOT.resolve()) or output == allowed or not output.is_relative_to(allowed):
        raise ValueError("HARNESS_OUTPUT_OUTSIDE_TEST_ROOT")
    packet, index = build_feedback(dataset_path=dataset_path, baseline_path=baseline_path, history_paths=history_paths)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(mode=0o700, exist_ok=False)
    payload = packet.model_dump(mode="json")
    optimizer = packet.optimizer_input().model_dump(mode="json")
    write_new_json(output / "feedback.json", payload)
    write_new_json(output / "optimizer-input.json", optimizer)
    write_new_json(output / "source-index.json", index)
    manifest = {"stage": "FEEDBACK_READY", "feedback_sha256": digest(payload),
                "optimizer_input_sha256": digest(optimizer), "source_index_sha256": digest(index),
                "report_count": len(packet.reports), "trial_count": sum(r.run_count for r in packet.reports),
                "model_calls": 0}
    # 最后发布完成标记；中断留下的半成品不能冒充FEEDBACK_READY。
    write_new_json(output / "manifest.json", manifest)
    return manifest
