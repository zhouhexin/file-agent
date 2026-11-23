"""A阶段独立规划评测：冻结模式、隔离运行、原始决策重放及独立路由评分。"""

from collections import Counter
from copy import deepcopy
import hashlib
from pathlib import Path
import time

from app.core.config import Settings
from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.routing.dataset import current_catalog, load_dataset
from app.modules.evidence_harness.routing.runtime import run_planning
from app.modules.evidence_harness.routing.comparator import _metrics
from app.modules.evidence_harness.improvement.executor import Journal, run_trial
from app.modules.evidence_harness.improvement.feedback import task_signal
from app.modules.evidence_harness.improvement.isolation import (
    API_ROOT, file_manifest, snapshot, probe_sandbox, trusted_code_ref, compatible_trusted_refs, canonical_code,
)
from app.modules.evidence_harness.improvement.schemas import PlanningEvaluationAuthorization
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import SafeErrorDiagnostic

# 只兼容已验收A-v1的执行器身份；下方仍核对全部原快照源码并独立重放。
# B仅增加宿主编排和受控Prompt代理，不改变A所用规划/评分语义。
A_V1_TRUSTED_REF = "1ddc155c057fc27fe36349b51d5994d1cbf9e43229a305d395cf5f9cf43a2a2e"


def effective_case(case, mode):
    """仅在内存输入副本中设置模式，原题目、附件及上下文都不改写。"""
    if mode not in {"legacy", "enabled"}:
        raise ValueError("PLANNING_MODE_INVALID")
    return case.model_copy(update={"adaptive_mode": mode}, deep=True)


def check_evaluation(*, dataset_path, output, planner_mode, settings, authorization, source="live"):
    """只检查身份、授权和真实Catalog，不建目录、不调用模型或系统隔离。"""
    from app.modules.evidence_harness.improvement.service import new_output
    dataset_path = store._dataset_file(dataset_path)
    dataset = load_dataset(dataset_path)
    cases = [effective_case(c, planner_mode) for c in dataset.inputs.cases]
    output = new_output(output)
    if source not in {"live", "fake"} or (source == "live") != (dataset.inputs.source == "real"):
        raise ValueError("PLANNING_SOURCE_INVALID")
    if source == "live" and (not settings.llm_enabled or not {"CONTENT_ANSWER", "FILE_LIST"} <= {g.expected_route for g in dataset.gold}):
        raise ValueError("PLANNING_LIVE_CONFIGURATION_INVALID")
    if dataset.inputs.catalog_snapshot != current_catalog():
        raise ValueError("PLANNING_CATALOG_CHANGED")
    upper = len(cases) * authorization.repetitions * 3
    return {"dataset": dataset, "cases": cases, "output": output, "dataset_path": dataset_path,
            "execution_inputs_ref": digest([c.model_dump(mode="json") for c in cases]),
            "planned_request_upper_bound": upper, "authorization_covers_upper_bound": authorization.max_total_requests >= upper}


class ReplayClient:
    """只读使用已记录的模型结构重放规划，绝不触发外部模型或Tool handler。"""

    def __init__(self, responses):
        """逐次请求身份必须与实际发送记录一致，不能只相信最后一个intent。"""
        self.responses, self.index = responses, 0

    def complete_json(self, *, system_prompt, user_payload):
        """异常按安全诊断复现，schema及后端校验仍交给真实规划节点。"""
        if self.index >= len(self.responses):
            raise ExperimentStopped("PLANNING_REPLAY_EXHAUSTED")
        item = self.responses[self.index]
        self.index += 1
        if digest([system_prompt, user_payload]) != item["request_ref"]:
            raise ExperimentStopped("PLANNING_REPLAY_REQUEST_MISMATCH")
        if item["status"] == "FAILED":
            diagnostic = SafeErrorDiagnostic.model_validate(item["diagnostic"]) if item["diagnostic"] else None
            raise LLMResponseError("BROKER_MODEL_FAILED", diagnostic=diagnostic)
        if item["status"] != "COMPLETED":
            raise ExperimentStopped("PLANNING_REPLAY_STATUS_INVALID")
        return deepcopy(item["raw"])


def replay_result(case, result, calls, catalog):
    """独立重放原始模型结构，检测伪造的有效模式、校验轨迹及最终计划。"""
    audit = result.get("planning_audit") or {}
    responses = audit.get("responses")
    if not isinstance(responses, list) or len(responses) != len(calls):
        raise ValueError("PLANNING_AUDIT_CALL_MISMATCH")
    for item, call in zip(responses, calls):
        if (item["request_ref"] != call["request_ref"] or item["status"] != call["status"]):
            raise ValueError("PLANNING_AUDIT_REQUEST_MISMATCH")
    client = ReplayClient(responses)
    settings = Settings(database_url="postgresql://offline:offline@127.0.0.1/file_agent_unit_tests",
                        llm_enabled=True, adaptive_planner_mode=case.adaptive_mode)
    expected = run_planning(case, catalog=catalog, client=client, settings=settings, capture_audit=True, quiet=True)
    target_mode = "ADAPTIVE" if case.adaptive_mode == "enabled" else "LEGACY"
    evaluable = bool(calls and all(c["status"] == "COMPLETED" for c in calls)
                     and not expected["diagnostics"]["fallback"] and expected["planning_audit"]["effective_mode"] == target_mode)
    if result.get("strategy_evaluable") != evaluable:
        raise ValueError("PLANNING_APPLICABILITY_CHANGED")
    if client.index != len(responses) or expected != {k: v for k, v in result.items() if k != "strategy_evaluable"}:
        raise ValueError("PLANNING_REPLAY_RESULT_MISMATCH")
    return expected


def run_evaluation(*, dataset_path, output, planner_mode, settings, authorization, source="live", backend_factory=None):
    """只评测一个固定规划模式，没有建议、补丁、业务Tool或自动上线。"""
    from app.modules.evidence_harness.improvement.service import _backend
    started = time.monotonic()
    checked = check_evaluation(dataset_path=dataset_path, output=output, planner_mode=planner_mode,
                               settings=settings, authorization=authorization, source=source)
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("PLANNING_BACKEND_SOURCE_INVALID")
    output, dataset = checked["output"], checked["dataset"]
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    journal = Journal(output, authorization)
    journal.started = started
    manifest = snapshot(output / "baseline")
    metadata = {"schema_version": "planning-baseline-v1", "source": source, "planner_mode": planner_mode,
        "dataset_source": checked["dataset_path"].relative_to(store.TEST_ROOT).as_posix(),
        "inputs_sha256": dataset.inputs_sha256, "gold_sha256": dataset.gold_sha256,
        "source_modes": {c.case_id: c.adaptive_mode for c in dataset.inputs.cases},
        "execution_inputs_ref": checked["execution_inputs_ref"], "catalog_ref": digest(dataset.inputs.catalog_snapshot),
        "authorization": authorization.model_dump(), "baseline_manifest": manifest, "trusted_code_ref": trusted_code_ref(),
        "model_ref": digest(settings.llm_chat_model), "endpoint_ref": digest(settings.llm_base_url),
        "evaluation_level": "INITIAL_ROUTING"}
    write_new_json(output / "experiment.json", metadata)
    stage, reason = "COMPLETED", "PLANNING_EVALUATION_COMPLETED"
    try:
        probes = probe_sandbox(output / "baseline", output, remaining=journal.remaining)
        write_new_json(output / "sandbox-checks.json", probes)
        for case in checked["cases"]:
            for repetition in range(1, authorization.repetitions + 1):
                journal.remaining()
                if journal.used >= authorization.max_total_requests:
                    raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
                trial_started = time.monotonic()
                try:
                    backend = backend_factory(case, repetition) if source == "fake" else _backend(settings)
                    result = run_trial(code=output / "baseline", scratch=output / "scratch" / f"{case.case_id}-{repetition}",
                        case=case, catalog=dataset.inputs.catalog_snapshot, journal=journal, backend=backend,
                        round_id=1, arm="baseline", repetition=repetition, capture_audit=True)
                except Exception as exc:
                    result = {"status": "FAILED", "error_code": "PLANNING_ISOLATED_TRIAL_FAILED",
                              "exception_type": type(exc).__name__ if type(exc).__name__ in {"RuntimeError", "ValueError", "TimeoutError"} else "Exception"}
                write_new_json(output / "trials" / f"{case.case_id}-{repetition}.json", {
                    "case_id": case.case_id, "repetition": repetition,
                    "case_input_ref": digest(case.model_dump(mode="json")),
                    "duration_ms": max(0, int((time.monotonic() - trial_started) * 1000)), "result": result})
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "BLOCKED", "PLANNING_ISOLATION_FAILED"
    if journal.elapsed() > authorization.max_active_seconds * 1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    summary = {"stage": stage, "stop_reason": reason, "source": source, "requests_used": journal.used,
               "model_calls": journal.used if source == "live" else 0, "active_duration_ms": journal.elapsed(),
               "automatically_adopted": False}
    write_new_json(output / "run-summary.json", summary)
    write_new_json(output / "integrity.json", file_manifest(output))
    report = compare_evaluation(output)
    write_new_json(output / "comparison.json", report)
    write_new_json(output / "feedback.json", report.get("feedback", {}))
    # feedback是可重建的投影，独立比较不信此缓存。
    text = (f"规划模式：{planner_mode}。\n结果：{report['decision']}。\n"
            f"整条备用链路由正确：{report.get('correct', 0)}/{report.get('planned', 0)}。\n"
            f"指定模式独立正确：{report.get('requested_planner_correct', 0)}/{report.get('planned', 0)}。\n"
            f"实际计划来源：{report.get('effective_modes', {})}。\n"
            f"请求数：{journal.used}；停止原因：{reason}。\n"
            "这是原版规划基线，没有生成补丁或评测实际检索、最终答案。\n")
    path = output / "summary.md"
    with path.open("x", encoding="utf-8") as stream:
        path.chmod(0o600)
        stream.write(text)
    return report


def _compare_evaluation(output):
    """核验所有身份与调用后重放，缓存结果、模式自述不能成为评分依据。"""
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", "auto_resume": False}
    actual = file_manifest(output)
    for name in ("integrity.json", "comparison.json", "feedback.json", "summary.md"):
        actual.pop(name, None)
    if actual != store._read(output / "integrity.json")[0]:
        raise ValueError("PLANNING_ARTIFACT_CHANGED")
    metadata, _ = store._read(output / "experiment.json")
    if (metadata["schema_version"] != "planning-baseline-v1" or metadata["trusted_code_ref"] not in {*compatible_trusted_refs(), A_V1_TRUSTED_REF}
        or metadata["source"] not in {"live", "fake"} or metadata["evaluation_level"] != "INITIAL_ROUTING"):
        raise ValueError("PLANNING_PROTOCOL_INVALID")
    dataset = load_dataset(store.TEST_ROOT / metadata["dataset_source"])
    if (dataset.inputs_sha256 != metadata["inputs_sha256"] or dataset.gold_sha256 != metadata["gold_sha256"]
        or (metadata["source"] == "live") != (dataset.inputs.source == "real")
        or digest(dataset.inputs.catalog_snapshot) != metadata["catalog_ref"]
        or {c.case_id: c.adaptive_mode for c in dataset.inputs.cases} != metadata["source_modes"]):
        raise ValueError("PLANNING_SOURCE_CHANGED")
    cases = [effective_case(c, metadata["planner_mode"]) for c in dataset.inputs.cases]
    if digest([c.model_dump(mode="json") for c in cases]) != metadata["execution_inputs_ref"]:
        raise ValueError("PLANNING_EFFECTIVE_INPUT_CHANGED")
    manifest = metadata["baseline_manifest"]
    if file_manifest(output / "baseline") != manifest:
        raise ValueError("PLANNING_SNAPSHOT_CHANGED")
    for name, sha in manifest.items():
        path = API_ROOT / name
        if not path.is_relative_to(API_ROOT) or hashlib.sha256(canonical_code(path)).hexdigest() != sha:
            raise ValueError("PLANNING_REPLAY_CODE_CHANGED")
    authorization = PlanningEvaluationAuthorization.model_validate(metadata["authorization"])
    calls, known, unknown = [], Counter(), 0
    slots = {(c.case_id, n) for c in cases for n in range(1, authorization.repetitions + 1)}
    for number, path in enumerate(sorted((output / "calls").glob("*.started.json")), 1):
        start, _ = store._read(path)
        if (start["sequence"] != number or path.name != f"{number:04d}.started.json" or start["role"] != "PLANNER"
            or start["arm"] != "baseline" or start["round_id"] != 1
            or (start["case_id"], start["repetition"]) not in slots or start["timeout_seconds"] != 60
            or start["max_output_tokens"] != authorization.planner_max_output_tokens
            or type(start["active_ms_before"]) is not int or start["active_ms_before"] < 0
            or start["active_ms_before"] + 62000 > authorization.max_active_seconds * 1000):
            raise ValueError("PLANNING_CALL_BINDING_INVALID")
        finish_path = path.with_name(path.name.replace("started", "finished"))
        finish = store._read(finish_path)[0] if finish_path.exists() else {**start, "status": "UNKNOWN"}
        if any(finish.get(k) != v for k, v in start.items()) or finish["status"] not in {"COMPLETED", "FAILED", "UNKNOWN"}:
            raise ValueError("PLANNING_FINISH_INVALID")
        calls.append(finish)
        usage = [finish.get(k) for k in ("input_tokens", "output_tokens")]
        unknown += any(v is None for v in usage)
        for key, value in zip(("input_tokens", "output_tokens"), usage):
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("PLANNING_USAGE_INVALID")
                known[key] += value
    counts = Counter((c["case_id"], c["repetition"]) for c in calls)
    if len(calls) > authorization.max_total_requests or any(n > 3 for n in counts.values()):
        raise ValueError("PLANNING_REQUEST_LIMIT_INVALID")
    if len(list((output / "calls").glob("*.finished.json"))) > len(calls):
        raise ValueError("PLANNING_FINISH_WITHOUT_START")
    summary, _ = store._read(output / "run-summary.json")
    if (summary["source"] != metadata["source"] or summary["requests_used"] != len(calls)
        or summary["model_calls"] != (len(calls) if metadata["source"] == "live" else 0)
        or summary["automatically_adopted"] is not False or summary["stage"] not in {"COMPLETED", "STOPPED", "INTERRUPTED", "BLOCKED"}
        or type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"] < 0
        or summary["stage"] == "COMPLETED" and summary["active_duration_ms"] > authorization.max_active_seconds * 1000):
        raise ValueError("PLANNING_SUMMARY_INVALID")
    if calls:
        probes, _ = store._read(output / "sandbox-checks.json")
        if any(probes.get(k) is not True for k in ("compile", "read_blocked", "write_blocked", "network_blocked", "exec_blocked")):
            raise ValueError("PLANNING_SANDBOX_NOT_VERIFIED")
    records = {}
    for path in (output / "trials").glob("*.json"):
        record, _ = store._read(path)
        key = (record["case_id"], record["repetition"])
        if key not in slots or key in records or path.name != f"{key[0]}-{key[1]}.json":
            raise ValueError("PLANNING_TRIAL_SLOT_INVALID")
        records[key] = record
    if summary["stage"] == "COMPLETED" and set(records) != slots:
        raise ValueError("PLANNING_COMPLETED_SLOTS_MISSING")
    gold = {g.case_id: g.expected_route for g in dataset.gold}
    rows, modes, feedback = [], Counter(), []
    for case in cases:
        for n in range(1, authorization.repetitions + 1):
            record = records.get((case.case_id, n))
            result = record["result"] if record else {"status": "NOT_RUN"}
            selected = [c for c in calls if c["case_id"] == case.case_id and c["repetition"] == n]
            if record and record["case_input_ref"] != digest(case.model_dump(mode="json")):
                raise ValueError("PLANNING_TRIAL_INPUT_CHANGED")
            if result["status"] == "COMPLETED":
                replay_result(case, result, selected, dataset.inputs.catalog_snapshot)
            elif result["status"] not in {"FAILED", "NOT_RUN"}:
                raise ValueError("PLANNING_TRIAL_STATUS_INVALID")
            audit = result.get("planning_audit") or {}
            effective = audit.get("effective_mode", "UNAVAILABLE")
            modes[effective] += 1
            predicted = result.get("route") if result.get("scope_valid") else None
            correct = predicted == gold[case.case_id] and result["status"] == "COMPLETED"
            target = "ADAPTIVE" if metadata["planner_mode"] == "enabled" else "LEGACY"
            independent = effective == target and all(c["status"] == "COMPLETED" for c in selected)
            row = {"case_id": case.case_id, "repetition": n, "status": result["status"],
                   "expected_route": gold[case.case_id], "predicted_route": predicted, "correct": correct,
                   "effective_mode": effective, "fallback_kind": audit.get("fallback_kind", "UNKNOWN"),
                   "requested_planner_exercised": independent, "requested_planner_correct": bool(correct and independent),
                   "violation_codes": result.get("violation_codes", []),
                   "duration_ms": record["duration_ms"] if record else None}
            rows.append(row)
            feedback.append({"case_ref": digest(case.case_id), "repetition": n, "requested_mode": metadata["planner_mode"],
                "effective_mode": effective, "fallback_kind": row["fallback_kind"], "change_kind": "BASELINE",
                **task_signal(row, {"failed_calls": sum(c["status"] != "COMPLETED" for c in selected),
                                   "fallback": effective in {"RULE", "LEGACY"} and metadata["planner_mode"] == "enabled"})})
    fully = all(r["requested_planner_exercised"] for r in rows)
    return {"decision": "EVALUATED" if summary["stage"] == "COMPLETED" and fully else "INCONCLUSIVE",
        "stage": summary["stage"], "planner_mode": metadata["planner_mode"], "evaluation_level": "INITIAL_ROUTING",
        "planned": len(rows), "recorded": len(records), "correct": sum(r["correct"] for r in rows), "metrics": _metrics(rows),
        "requested_planner_correct": sum(r["requested_planner_correct"] for r in rows), "requested_planner_fully_exercised": fully,
        "effective_modes": dict(modes), "rows": rows, "requests_used": len(calls),
        "failed_model_calls": sum(c["status"] != "COMPLETED" for c in calls), "known_tokens": dict(known),
        "unknown_token_calls": unknown, "token_usage_complete": unknown == 0, "active_duration_ms": summary["active_duration_ms"],
        "stop_reason": summary["stop_reason"], "target_file_retrieval": "NOT_EXECUTED", "answer_quality": "NOT_EXECUTED",
        "automatically_adopted": False, "feedback": {"schema_version": "planning-feedback-v1", "items": feedback}}


def compare_evaluation(output):
    """只读重放失败关闭，安全错误码不携带原始模型输出或私有正文。"""
    try:
        return _compare_evaluation(output)
    except (Exception, ExperimentStopped):
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "PLANNING_VALIDATION_FAILED", "automatically_adopted": False}
