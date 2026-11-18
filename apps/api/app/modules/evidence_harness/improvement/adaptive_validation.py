"""C阶段固定候选的扩大重复验证：无建议器调用，分开开发/保留与独立只读重放。"""

from collections import Counter
import math
import re
import statistics
import time

from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import validation_dataset as data_store
from app.modules.evidence_harness.improvement import planning_evaluation as baseline
from app.modules.evidence_harness.improvement import codex_backend
from app.modules.evidence_harness.improvement.executor import Journal, run_trial, run_checks
from app.modules.evidence_harness.improvement.isolation import snapshot, file_manifest, probe_sandbox, trusted_code_ref
from app.modules.evidence_harness.improvement.planning_feedback import task_diagnostic
from app.modules.evidence_harness.improvement.patches import source_hash
from app.modules.evidence_harness.routing.comparator import _metrics
from app.modules.evidence_harness.improvement.schemas import PlanningEvaluationAuthorization

# 只兼容已知旧C执行器，不放宽未知代码身份；旧记录继续按原同模型协议重放。
KNOWN_C_V1_TRUSTED_REF = "6d1f4185af9a05b3fe2c355f2d7c69f2fe4725bf495e8bd8e44b3bd4ae9d043b"
# 新增认证适配只改变宿主；旧C-v2仍按其原API Profile核验，不要求重新登录Codex。
KNOWN_C_V2_TRUSTED_REF = "61372fd1ca6e303b37fe6e545b51e210fd38d43256a7c782e4be243975dc202a"
KNOWN_C_V3_TRUSTED_REF = "886b2907c4e17ac4fa9ce7eafb0af9a377d6c0d7f5d6123c72e805441e951345"
KNOWN_C_V3_STARTUP_REF = "c9902ecf2b0c0b6af67e47e1823d9f2e56c7e4bb1128fdeb04ae9642db944926"
# 新增离线迭代宿主不改变旧C评分；仅登记已经完成独立核验的HTTPS执行器身份。
KNOWN_C_V3_HTTPS_REF = "a50658c26ffaa50353a498496e0117e1b9f795afe956199e197a10e36f4e1367"


class ValidationAuthorization(PlanningEvaluationAuthorization):
    """扩大实验必须显式请求额度，重复2—3次，不能沿用建议器或自动增加预算。"""

    max_total_requests: int = Field(ge=1, le=1000)
    max_active_seconds: int = Field(default=3600, ge=1, le=7200)
    repetitions: int = Field(default=3, ge=2, le=3)


def schedule(cases, repetitions):
    """逐轮交替两组先后，固定全部试次而不依赖运行中得分选择次序。"""
    return [{"arm": arm, "case_id": case.case_id, "repetition": n}
            for n in range(1, repetitions + 1) for case in cases
            for arm in (("control", "candidate") if n % 2 else ("candidate", "control"))]


def _profile_details(origin, profile, allow_model_profile_change):
    """明确区分来源模型与本轮两组共用模型；只记录脱敏身份，不包含访问凭据。"""
    if type(allow_model_profile_change) is not bool:
        raise ValueError("VALIDATION_MODEL_CHANGE_AUTHORIZATION_INVALID")
    model_changed = origin["model_ref"] != profile["model_ref"]
    endpoint_changed = origin["endpoint_ref"] != profile["endpoint_ref"]
    if (model_changed or endpoint_changed) and not allow_model_profile_change:
        raise ValueError("VALIDATION_MODEL_PROFILE_CHANGED")
    return {"origin_profile": {k: origin[k] for k in ("model_ref", "endpoint_ref")}, "profile": profile,
        "allow_model_profile_change": allow_model_profile_change, "model_changed": model_changed,
        "endpoint_changed": endpoint_changed, "comparison_scope": "CROSS_MODEL" if model_changed or endpoint_changed else "SAME_MODEL"}


def _execution_profile(settings, authorization):
    """额度来自本轮显式授权，两组只使用执行开始时复制的同一模型配置。"""
    return {"model_ref": digest(settings.llm_chat_model), "endpoint_ref": digest(settings.llm_base_url),
        "planner_max_output_tokens": authorization.planner_max_output_tokens, "timeout_seconds": 60, "max_retries": 0,
        "generation_settings": "EXISTING_PROVIDER_DEFAULTS"}


def check(*, dataset_path, output, settings, authorization, source="live", allow_model_profile_change=False,
          provider="openai-compatible", codex_model=None, codex_transport=None):
    """默认零调用、不建目录，完整验证冻结题库、锁定候选和实际执行Profile。"""
    dataset_path = store._dataset_file(dataset_path)
    dataset, locked = data_store.load(dataset_path)
    meta = locked["metadata"]
    if source not in {"live", "fake"} or meta["source"] != source or source == "live" and not settings.llm_enabled:
        raise ValueError("VALIDATION_SOURCE_INVALID")
    # Provider选择只属于本次C宿主，不修改Settings或候选Planner，不允许自动回落到旧API。
    if provider == "codex-login":
        backend_spec = codex_backend.inspect(codex_model, **({"transport": codex_transport} if codex_transport is not None else {}))
        execution_profile = codex_backend.profile(backend_spec, authorization.planner_max_output_tokens)
    elif provider == "openai-compatible" and codex_model is None and codex_transport is None:
        backend_spec = None
        execution_profile = _execution_profile(settings, authorization)
    else:
        raise ValueError("VALIDATION_PROVIDER_INVALID")
    profile_details = _profile_details(meta, execution_profile, allow_model_profile_change)
    cases = [baseline.effective_case(c, "enabled") for c in dataset.inputs.cases]
    upper = 2 * len(cases) * authorization.repetitions * 3
    return {"dataset": dataset, "locked": locked, "cases": cases, "dataset_path": dataset_path,
        "output": policy.new_output(output), "planned_request_upper_bound": upper,
        "authorization_covers_upper_bound": authorization.max_total_requests >= upper,
        "worst_request_wait_seconds": upper * 60,
        "time_budget_covers_request_wait_only": authorization.max_active_seconds >= upper * 60,
        "provider": provider, "backend_spec": backend_spec, **profile_details}


def run(*, dataset_path, output, settings, authorization, source="live", backend_factory=None, allow_model_profile_change=False,
        provider="openai-compatible", codex_model=None, codex_transport=None):
    """只运行已固定的一个候选，所有重复共享预算，失败和中断不自动续跑。"""
    started = time.monotonic()
    # 禁止轮次中重新加载env或被调用方修改原Settings导致两组使用不同模型。
    settings = settings.model_copy(deep=True)
    checked = check(dataset_path=dataset_path, output=output, settings=settings, authorization=authorization, source=source,
        allow_model_profile_change=allow_model_profile_change, provider=provider, codex_model=codex_model, codex_transport=codex_transport)
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("VALIDATION_BACKEND_SOURCE_INVALID")
    output, dataset, locked = checked["output"], checked["dataset"], checked["locked"]
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    journal = Journal(output, authorization, profile_ref=digest(checked["profile"]))
    journal.started = started
    manifest = snapshot(output / "baseline")
    if manifest != locked["metadata"]["baseline_manifest"]:
        raise ValueError("VALIDATION_EXECUTION_SOURCE_CHANGED")
    order = schedule(checked["cases"], authorization.repetitions)
    profile_details = {k: checked[k] for k in ("origin_profile", "profile", "allow_model_profile_change", "model_changed", "endpoint_changed", "comparison_scope")}
    write_new_json(output / "experiment.json", {"schema_version": "adaptive-validation-experiment-v3", "source": source, "provider": provider,
        "dataset_source": str(checked["dataset_path"].relative_to(store.TEST_ROOT)), "inputs_sha256": dataset.inputs_sha256,
        "annotations_sha256": dataset.annotations_sha256, "candidate_source_ref": locked["source_ref"],
        "policy_ref": locked["candidate"]["prompt_ref"], "patch_ref": locked["candidate"]["patch_ref"],
        "execution_inputs_ref": digest([c.model_dump(mode="json") for c in checked["cases"]]),
        "baseline_manifest": manifest, "trusted_code_ref": trusted_code_ref(), "authorization": authorization.model_dump(),
        **profile_details, "evaluation_level": "INITIAL_ROUTING", "holdout_usage": "VALIDATION_ONLY", "advisor_calls": 0})
    write_new_json(output / "schedule.json", {"trials": order})
    stage, reason, execution = "COMPLETED", "VALIDATION_COMPLETED", "PREPARING"
    try:
        write_new_json(output / "sandbox-checks.json", probe_sandbox(output / "baseline", output, remaining=journal.remaining))
        policy.prepare_copies(output, locked["candidate"]["source"])
        checks = run_checks(output, journal)
        write_new_json(output / "checks.json", checks)
        if not checks["passed"]:
            execution, stage, reason = "CHECKS_FAILED", "FAILED", "VALIDATION_PROTECTED_CHECK_FAILED"
        else:
            execution = "EXECUTING"
            cases = {c.case_id: c for c in checked["cases"]}
            for slot in order:
                journal.remaining()
                if journal.used >= authorization.max_total_requests:
                    raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
                arm, case_id, n = slot["arm"], slot["case_id"], slot["repetition"]
                case = cases[case_id]
                trial_started = time.monotonic()
                try:
                    backend = (backend_factory(arm, case, n) if source == "fake" else
                        codex_backend.CodexLoginTransport(checked["backend_spec"]) if provider == "codex-login" else policy._backend(settings))
                    result = run_trial(code=output / arm, scratch=output / "scratch" / f"{arm}-{case_id}-{n}",
                        case=case, catalog=dataset.inputs.catalog_snapshot, journal=journal, backend=backend,
                        round_id=1, arm=arm, repetition=n, capture_audit=True,
                        adaptive_prompt=locked["candidate"]["prompt"] if arm == "candidate" else None)
                except Exception as exc:
                    cause = str(exc)
                    result = {"status": "FAILED", "error_code": "VALIDATION_ISOLATED_TRIAL_FAILED", "stage": "ISOLATED_PLANNING",
                        "exception_type": type(exc).__name__ if type(exc).__name__ in {"ValueError", "RuntimeError", "TimeoutError"} else "Exception",
                        "cause_code": cause if re.fullmatch(r"[A-Z_]{1,90}", cause) else "UNSPECIFIED_EXECUTION_ERROR"}
                write_new_json(output / "trials" / f"{arm}-{case_id}-{n}.json", {**slot,
                    "case_input_ref": digest(case.model_dump(mode="json")), "duration_ms": max(0, int((time.monotonic() - trial_started) * 1000)),
                    "result": result})
            execution = "EXECUTED"
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "FAILED", "VALIDATION_ISOLATED_EXECUTION_FAILED"
    if journal.elapsed() > authorization.max_active_seconds * 1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    write_new_json(output / "run-summary.json", {"stage": stage, "execution_stage": execution, "stop_reason": reason,
        "source": source, "requests_used": journal.used, "model_calls": journal.used if source == "live" else 0,
        "active_duration_ms": journal.elapsed(), "advisor_calls": 0, "automatically_adopted": False})
    write_new_json(output / "integrity.json", file_manifest(output))
    report = compare(output)
    write_new_json(output / "comparison.json", report)
    policy._write_text(output / "summary.md", f"锁定候选：{locked['candidate']['prompt_ref']}。\n"
        f"结论：{report['decision']}；停止原因：{reason}；请求：{journal.used}。\n"
        f"比较范围：{checked['comparison_scope']}；两组均使用本轮固定模型，不与旧模型分数相减。\n"
        "没有调用建议器、修改候选或上线；本次只测首轮规划。\n")
    return report


def aggregate(rows):
    """分母包含全部预定试次，分别展示路线、决策类型、契约和多轮稳定性。"""
    latencies = sorted(r["duration_ms"] for r in rows if r["duration_ms"] is not None)
    cases = []
    for cid in sorted({r["case_id"] for r in rows}):
        selected = [r for r in rows if r["case_id"] == cid]
        patterns = {(r["predicted_route"], r["predicted_decision_type"]) for r in selected}
        cases.append({"case_id": cid, "correct": sum(r["correct"] for r in selected), "planned": len(selected),
            "accuracy": sum(r["correct"] for r in selected) / len(selected),
            "route_and_decision_stable": len(patterns) == 1 and all(r["status"] == "COMPLETED" for r in selected),
            "predicted_routes": sorted({r["predicted_route"] or "UNAVAILABLE" for r in selected}),
            "predicted_decision_types": sorted({r["predicted_decision_type"] or "UNAVAILABLE" for r in selected})})
    eligible = [r for r in rows if r["contract_mapping_status"] in {"RECOGNIZED", "UNRECOGNIZED"}]
    typed = [r for r in rows if r["expected_decision_type"] is not None]
    return {"planned": len(rows), "recorded": sum(r["status"] != "NOT_RUN" for r in rows),
        "correct": sum(r["correct"] for r in rows), "route_metrics": _metrics(rows),
        "decision_type_accuracy": sum(r["decision_type_correct"] for r in typed) / len(typed) if typed else None,
        "contract_recognition_rate": sum(r["contract_mapping_status"] == "RECOGNIZED" for r in eligible) / len(eligible) if eligible else None,
        "known_tokens": {k: sum(r[k] for r in rows) for k in ("input_tokens", "output_tokens")},
        "unknown_token_calls": sum(r["unknown_token_calls"] for r in rows),
        "latency_ms": {"median": statistics.median(latencies) if latencies else None,
                       "p95": latencies[math.ceil(len(latencies) * .95) - 1] if latencies else None},
        "cases": cases, "rows": rows}


def _compare(output):
    """完整重建冻结候选、分组、顺序和原始模型响应，不读取缓存结论参与评分。"""
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", "automatically_adopted": False}
    actual = file_manifest(output)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        actual.pop(name, None)
    if actual != store._read(output / "integrity.json")[0]:
        raise ValueError("VALIDATION_ARTIFACT_CHANGED")
    meta = store._read(output / "experiment.json")[0]
    legacy = meta["schema_version"] == "adaptive-validation-experiment-v1" and meta["trusted_code_ref"] == KNOWN_C_V1_TRUSTED_REF
    compatible_v2 = meta["schema_version"] == "adaptive-validation-experiment-v2" and meta["trusted_code_ref"] == KNOWN_C_V2_TRUSTED_REF
    current = meta["schema_version"] == "adaptive-validation-experiment-v3" and meta["trusted_code_ref"] in {trusted_code_ref(), KNOWN_C_V3_TRUSTED_REF, KNOWN_C_V3_STARTUP_REF, KNOWN_C_V3_HTTPS_REF}
    if (not (legacy or compatible_v2 or current)
        or meta["holdout_usage"] != "VALIDATION_ONLY" or meta["advisor_calls"] != 0 or meta["evaluation_level"] != "INITIAL_ROUTING"):
        raise ValueError("VALIDATION_PROTOCOL_INVALID")
    dataset, locked = data_store.load(store.TEST_ROOT / meta["dataset_source"])
    cases = [baseline.effective_case(c, "enabled") for c in dataset.inputs.cases]
    if (meta["source"] != locked["metadata"]["source"] or meta["inputs_sha256"] != dataset.inputs_sha256
        or meta["annotations_sha256"] != dataset.annotations_sha256 or meta["candidate_source_ref"] != locked["source_ref"]
        or meta["policy_ref"] != locked["candidate"]["prompt_ref"] or meta["patch_ref"] != locked["candidate"]["patch_ref"]
        or meta["execution_inputs_ref"] != digest([c.model_dump(mode="json") for c in cases])
        or meta["baseline_manifest"] != locked["metadata"]["baseline_manifest"] or file_manifest(output / "baseline") != meta["baseline_manifest"]):
        raise ValueError("VALIDATION_SOURCE_BINDING_INVALID")
    auth = ValidationAuthorization.model_validate(meta["authorization"])
    profile = meta["profile"]
    expected = {"model_ref": locked["metadata"]["model_ref"], "endpoint_ref": locked["metadata"]["endpoint_ref"],
        "planner_max_output_tokens": auth.planner_max_output_tokens, "timeout_seconds": 60, "max_retries": 0,
        "generation_settings": "EXISTING_PROVIDER_DEFAULTS"}
    profile_details = {}
    if current or compatible_v2:
        # 跨模型仅由操作者显式启用，来源仍由B独立重建；实际配置再与逐次调用相互核对。
        for key in ("model_ref", "endpoint_ref"):
            if not isinstance(profile[key], str) or not re.fullmatch(r"[0-9a-f]{64}", profile[key]):
                raise ValueError("VALIDATION_PROFILE_INVALID")
            expected[key] = profile[key]
        if current:
            if meta["provider"] == "codex-login":
                if (not re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?", profile["codex_version"])
                    or not re.fullmatch(r"[0-9a-f]{64}", profile["codex_binary_ref"])):
                    raise ValueError("VALIDATION_CODEX_PROFILE_INVALID")
                if profile.get("transport") not in {None, "HTTPS_SSE"}:
                    raise ValueError("VALIDATION_CODEX_TRANSPORT_INVALID")
                expected = codex_backend.profile({"model": "unused-for-replay", "version": profile["codex_version"],
                    "binary_ref": profile["codex_binary_ref"], "transport": "https" if profile.get("transport") == "HTTPS_SSE" else "auto"}, auth.planner_max_output_tokens)
                expected["model_ref"] = profile["model_ref"]
            elif meta["provider"] != "openai-compatible":
                raise ValueError("VALIDATION_PROVIDER_INVALID")
        profile_details = _profile_details(locked["metadata"], profile, meta["allow_model_profile_change"])
        if any(meta.get(k) != v for k, v in profile_details.items()):
            raise ValueError("VALIDATION_PROFILE_BINDING_INVALID")
    if profile != expected:
        raise ValueError("VALIDATION_PROFILE_INVALID")
    order = schedule(cases, auth.repetitions)
    if store._read(output / "schedule.json")[0] != {"trials": order}:
        raise ValueError("VALIDATION_SCHEDULE_CHANGED")
    calls, known, unknown = policy.read_calls(output, meta, auth, cases)
    if (current or compatible_v2) and (any(c.get("profile_ref") != digest(profile) for c in calls)
        or any(store._read(p)[0].get("profile_ref") != digest(profile) for p in (output / "calls").glob("*.started.json"))):
        raise ValueError("VALIDATION_CALL_PROFILE_INVALID")
    if any(c["role"] != "PLANNER" for c in calls):
        raise ValueError("VALIDATION_ADVISOR_FORBIDDEN")
    positions = {(s["arm"], s["case_id"], s["repetition"]): i for i, s in enumerate(order)}
    observed_order = [positions[(c["arm"], c["case_id"], c["repetition"])] for c in calls]
    if observed_order != sorted(observed_order):
        raise ValueError("VALIDATION_CALL_ORDER_CHANGED")
    summary = store._read(output / "run-summary.json")[0]
    if (summary["source"] != meta["source"] or summary["requests_used"] != len(calls)
        or summary["model_calls"] != (len(calls) if meta["source"] == "live" else 0) or summary["advisor_calls"] != 0
        or summary["automatically_adopted"] is not False or summary["stage"] not in {"COMPLETED", "STOPPED", "INTERRUPTED", "FAILED"}
        or type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"] < 0
        or summary["stage"] == "COMPLETED" and summary["active_duration_ms"] > auth.max_active_seconds * 1000):
        raise ValueError("VALIDATION_SUMMARY_INVALID")
    checks_path = output / "checks.json"
    checks_ok = checks_path.exists() and store._read(checks_path)[0] == policy.expected_checks()
    if calls and not checks_ok:
        raise ValueError("VALIDATION_CHECKS_NOT_VERIFIED")
    for arm in ("control", "candidate"):
        if (output / arm).exists():
            expected = {**meta["baseline_manifest"], **({policy.TARGET: source_hash(locked["candidate"]["source"])} if arm == "candidate" else {})}
            if file_manifest(output / arm) != expected:
                raise ValueError("VALIDATION_EXECUTED_SOURCE_CHANGED")
        elif calls:
            raise ValueError("VALIDATION_EXECUTED_SOURCE_MISSING")
    records = {}
    for path in (output / "trials").glob("*.json"):
        record = store._read(path)[0]
        key = record["arm"], record["case_id"], record["repetition"]
        if key not in positions or key in records or path.name != f"{key[0]}-{key[1]}-{key[2]}.json":
            raise ValueError("VALIDATION_TRIAL_SLOT_INVALID")
        records[key] = record
    if summary["stage"] == "COMPLETED" and (set(records) != set(positions) or summary["execution_stage"] != "EXECUTED"):
        raise ValueError("VALIDATION_COMPLETED_SLOTS_MISSING")
    annotations = {a.case_id: a for a in dataset.annotations}
    rows_by_arm = {a: [] for a in ("control", "candidate")}
    for case in cases:
        annotation = annotations[case.case_id]
        for n in range(1, auth.repetitions + 1):
            for arm in rows_by_arm:
                record = records.get((arm, case.case_id, n))
                result = record["result"] if record else {"status": "NOT_RUN"}
                selected = [c for c in calls if (c["arm"], c["case_id"], c["repetition"]) == (arm, case.case_id, n)]
                if record and (record["case_input_ref"] != digest(case.model_dump(mode="json")) or type(record["duration_ms"]) is not int or record["duration_ms"] < 0):
                    raise ValueError("VALIDATION_TRIAL_INPUT_INVALID")
                if result["status"] == "COMPLETED":
                    policy.replay_trial(case, result, selected, dataset.inputs.catalog_snapshot,
                        locked["candidate"]["prompt"] if arm == "candidate" else policy.policy_context((output / "baseline" / policy.TARGET).read_text())["original_prompt"])
                elif result["status"] not in {"FAILED", "NOT_RUN"}:
                    raise ValueError("VALIDATION_TRIAL_STATUS_INVALID")
                valid = result.get("scope_valid", False)
                predicted = result.get("route") if valid else None
                decision = (result.get("plan") or {}).get("decision_type")
                route_correct = result["status"] == "COMPLETED" and predicted == annotation.expected_route
                type_correct = annotation.expected_decision_type is None or decision == annotation.expected_decision_type
                audit = result.get("planning_audit") or {}
                diagnostic = task_diagnostic(case, result)
                rows_by_arm[arm].append({"case_id": case.case_id, "repetition": n, "split": annotation.split,
                    "status": result["status"], "expected_route": annotation.expected_route, "predicted_route": predicted,
                    "expected_decision_type": annotation.expected_decision_type, "predicted_decision_type": decision,
                    "route_correct": route_correct, "decision_type_correct": bool(type_correct and result["status"] == "COMPLETED"),
                    "correct": bool(route_correct and type_correct), "strategy_evaluable": result.get("strategy_evaluable", False),
                    "effective_mode": audit.get("effective_mode", "UNAVAILABLE"), "fallback_kind": audit.get("fallback_kind", "UNKNOWN"),
                    "contract_mapping_status": diagnostic["intent_contract"]["mapping_status"], "violation_codes": result.get("violation_codes", []),
                    "duration_ms": record["duration_ms"] if record else None,
                    "input_tokens": sum(c.get("input_tokens") or 0 for c in selected), "output_tokens": sum(c.get("output_tokens") or 0 for c in selected),
                    "unknown_token_calls": sum(c.get("input_tokens") is None or c.get("output_tokens") is None for c in selected)})
    arms = {arm: {"overall": aggregate(rows), **{split: aggregate([r for r in rows if r["split"] == split])
        for split in ("DEVELOPMENT", "HOLDOUT")}} for arm, rows in rows_by_arm.items()}
    pairs = list(zip(rows_by_arm["control"], rows_by_arm["candidate"]))
    # 未运行或模型失败不是已观察业务退步；配对比较必须两边均有效使用Adaptive。
    evaluable_pairs = [(c, n) for c, n in pairs if c["strategy_evaluable"] and n["strategy_evaluable"]]
    regressions = sorted({c["case_id"] for c, n in evaluable_pairs if c["correct"] and not n["correct"]})
    contract_regressions = sorted({c["case_id"] for c, n in evaluable_pairs if c["contract_mapping_status"] == "RECOGNIZED" and n["contract_mapping_status"] == "UNRECOGNIZED"})
    # 规则兜底的越界观察仍保留在rows，但不能归因于已经失败的候选模型策略。
    safety = sum(bool(r["violation_codes"]) and r["effective_mode"] == "ADAPTIVE" for r in rows_by_arm["candidate"])
    fully = checks_ok and all(r["strategy_evaluable"] for rows in rows_by_arm.values() for r in rows)
    dev_gain = arms["candidate"]["DEVELOPMENT"]["correct"] > arms["control"]["DEVELOPMENT"]["correct"]
    holdout = arms["candidate"]["HOLDOUT"]
    decision = ("REGRESSION" if safety or regressions or contract_regressions else
        "INCONCLUSIVE" if summary["stage"] != "COMPLETED" or not fully else
        "VALIDATED" if dev_gain and holdout["correct"] == holdout["planned"] else "NO_GAIN")
    return {"decision": decision, "stage": summary["stage"], "arms": arms, "regression_case_ids": regressions,
        "contract_regression_case_ids": contract_regressions, "candidate_safety_failures": safety, "strategy_evaluable": fully,
        "policy_ref": locked["candidate"]["prompt_ref"], "patch_ref": locked["candidate"]["patch_ref"],
        "case_count": len(cases), "repetitions": auth.repetitions, "advisor_calls": 0,
        "coverage": {"attachment_cases": sum(bool(c.attachments) for c in cases),
            "clarification_cases": sum(a.expected_decision_type == "CLARIFY" for a in dataset.annotations)},
        "holdout_usage": "VALIDATION_ONLY", "candidate_retained": decision == "VALIDATED", **profile_details,
        **({"provider": meta["provider"]} if current else {}),
        **policy.cost_report(meta, summary, calls, known, unknown)}


def compare(output):
    """失败关闭且不回显私有题干、标签或异常全文，比较本身零模型调用。"""
    try:
        return _compare(output)
    except (Exception, ExperimentStopped):
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "VALIDATION_INTEGRITY_FAILED", "automatically_adopted": False}
