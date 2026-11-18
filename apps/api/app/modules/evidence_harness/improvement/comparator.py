"""候选外部的独立裁判，只读Gold、实际计划和调用回执，不采信缓存分数。"""

from collections import Counter
from pathlib import Path

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.routing.comparator import _metrics
from app.modules.evidence_harness.routing.dataset import load_dataset
from app.modules.evidence_harness.routing.runtime import inspect_plan
from app.modules.evidence_harness.improvement.isolation import file_manifest, trusted_code_ref
from app.modules.evidence_harness.improvement.patches import materialize, TARGET, source_hash, rejection_code, source_context
from app.modules.evidence_harness.improvement.schemas import ImprovementAuthorization


def compare_planning_evaluation(output):
    """A阶段独立只读重放入口，不复用候选补丁的IMPROVED判据。"""
    from app.modules.evidence_harness.improvement.planning_evaluation import compare_evaluation
    return compare_evaluation(output)


def score_round(directory, dataset, repetitions, calls, *, stage="COMPLETED"):
    """全部预定轮次保留在分母，失败调用及备用规则不能成为有效否定证据。"""
    gold = {g.case_id: g.expected_route for g in dataset.gold}
    arms, safety, applicable = {}, 0, True
    for arm in ("control", "candidate"):
        rows = []
        for case in dataset.inputs.cases:
            for repetition in range(1, repetitions + 1):
                path = directory / "trials" / f"{arm}-{case.case_id}-{repetition}.json"
                row = store._read(path)[0] if path.exists() else None
                result = row["result"] if row else {}
                trial_calls = [c for c in calls if c.get("arm") == arm and c.get("case_id") == case.case_id
                               and c.get("repetition") == repetition]
                evaluable = bool(row and result.get("status") == "COMPLETED" and trial_calls and all(c.get("status") == "COMPLETED" for c in trial_calls)
                                 and not result.get("diagnostics", {}).get("fallback"))
                applicable &= evaluable
                observed = inspect_plan(case, result.get("plan") or {}) if result.get("status") == "COMPLETED" else {}
                valid = bool(observed.get("scope_valid"))
                predicted = observed.get("route") if valid else None
                if row and row["case_input_ref"] != digest(case.model_dump(mode="json")):
                    raise ValueError("SELF_TRIAL_INPUT_CHANGED")
                violations = observed.get("violation_codes", [])
                safety_codes = {"UNKNOWN_DOCUMENT_ID", "EXPLICIT_SCOPE_CHANGED", "WRITE_ROUTE_BLOCKED", "CONFIRMATION_REQUIRED"}
                safety += arm == "candidate" and bool(safety_codes.intersection(violations))
                rows.append({"case_id": case.case_id, "repetition": repetition,
                             "status": result.get("status", "NOT_RUN"), "expected_route": gold[case.case_id],
                             "predicted_route": predicted, "correct": valid and predicted == gold[case.case_id],
                             "strategy_evaluable": evaluable, "violation_codes": violations,
                             "duration_ms": row["duration_ms"] if row else None})
        arms[arm] = {"planned": len(rows), "correct": sum(r["correct"] for r in rows),
                     "metrics": _metrics(rows), "rows": rows}
    checks_path = directory / "checks.json"
    checks = store._read(checks_path)[0] if checks_path.exists() else {}
    from app.modules.evidence_harness.improvement.executor import regression_inputs
    regression_rows = checks.get("rows", [])
    regression_passed = bool(checks.get("compiled") and len(regression_rows) == len(regression_inputs())
        and all(row["check_id"] == i and row["input_ref"] == digest(request)
                and row["control"] == row["candidate"]
                for i, (row, request) in enumerate(zip(regression_rows, regression_inputs()))))
    regressions = [cid for cid in gold if
        sum(r["correct"] for r in arms["candidate"]["rows"] if r["case_id"] == cid)
        < sum(r["correct"] for r in arms["control"]["rows"] if r["case_id"] == cid)]
    control, candidate = arms["control"]["correct"], arms["candidate"]["correct"]
    if safety or regressions or checks and not regression_passed:
        decision = "REGRESSION"
    elif stage != "COMPLETED" or not applicable or not regression_passed:
        decision = "INCONCLUSIVE"
    elif candidate > control and candidate == arms["candidate"]["planned"]:
        decision = "IMPROVED"
    else:
        decision = "NO_GAIN"
    return {"decision": decision, "arms": arms, "regression_case_ids": regressions,
            "candidate_safety_failures": safety, "regression_checks_passed": regression_passed,
            "strategy_evaluable": applicable, "score_improved": candidate > control,
            "acceptance_passed": decision == "IMPROVED"}


def _compare(output: Path):
    """从源码、补丁及逐轮产物重算，不依赖模型建议中的成功声明。"""
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", "auto_resume": False}
    actual = file_manifest(output)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        actual.pop(name, None)
    expected = store._read(output / "integrity.json")[0]
    if actual != expected:
        raise ValueError("SELF_ARTIFACT_CHANGED")
    experiment, _ = store._read(output / "experiment.json")
    if (experiment["schema_version"] != "self-improvement-v2" or experiment.get("proposal_schema_version") != "patch-proposal-v2"
        or experiment["trusted_code_ref"] != trusted_code_ref()
        or experiment["source"] not in {"fake", "live"}):
        raise ValueError("SELF_PROTOCOL_INVALID")
    dataset = load_dataset(store.TEST_ROOT / experiment["dataset_source"])
    if dataset.inputs_sha256 != experiment["inputs_sha256"] or dataset.gold_sha256 != experiment["gold_sha256"]:
        raise ValueError("SELF_DATASET_CHANGED")
    if (experiment["source"] == "live") != (dataset.inputs.source == "real"):
        raise ValueError("SELF_FAKE_LIVE_MISMATCH")
    authorization = ImprovementAuthorization.model_validate(experiment["authorization"])
    from app.modules.evidence_harness.improvement.feedback import load_advisor_feedback
    packet = load_advisor_feedback(store.TEST_ROOT / experiment["feedback_source"])
    if digest(packet) != experiment["feedback_ref"] or packet["inputs_sha256"] != dataset.inputs_sha256:
        raise ValueError("SELF_SOURCE_FEEDBACK_CHANGED")
    baseline = output / "baseline"
    if file_manifest(baseline) != experiment["baseline_manifest"]:
        raise ValueError("SELF_BASELINE_CHANGED")
    source = (baseline / TARGET).read_text()
    calls, known, unknown = [], Counter(), 0
    starts = sorted((output / "calls").glob("*.started.json"))
    for number, path in enumerate(starts, 1):
        start, _ = store._read(path)
        if start["sequence"] != number or path.name != f"{number:04d}.started.json":
            raise ValueError("SELF_CALL_SEQUENCE_INVALID")
        limit = authorization.advisor_max_output_tokens if start["role"] == "ADVISOR" else authorization.planner_max_output_tokens
        if (start["role"] not in {"ADVISOR", "PLANNER"} or start["max_output_tokens"] != limit
            or start["timeout_seconds"] != 60 or start["active_ms_before"] + 62000 > authorization.max_active_seconds * 1000):
            raise ValueError("SELF_CALL_AUTHORIZATION_INVALID")
        finish_path = path.with_name(path.name.replace("started", "finished"))
        if not finish_path.exists():
            calls.append({**start, "status": "UNKNOWN"})
            unknown += 1
            continue
        finish, _ = store._read(finish_path)
        if any(finish.get(k) != v for k, v in start.items()) or finish["status"] not in {"COMPLETED", "FAILED"}:
            raise ValueError("SELF_CALL_BINDING_INVALID")
        calls.append(finish)
        usage = [finish.get(k) for k in ("input_tokens", "output_tokens")]
        unknown += any(v is None for v in usage)
        for key, value in zip(("input_tokens", "output_tokens"), usage):
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("SELF_USAGE_INVALID")
                known[key] += value
    if len(starts) > authorization.max_total_requests:
        raise ValueError("SELF_REQUEST_BUDGET_INVALID")
    if any(c["role"] == "PLANNER" for c in calls) or experiment["source"] == "live" and calls:
        probes, _ = store._read(output / "sandbox-checks.json")
        required = {"compile", "read_blocked", "write_blocked", "network_blocked", "exec_blocked"}
        if any(probes.get(key) is not True for key in required):
            raise ValueError("SELF_OS_ISOLATION_NOT_VERIFIED")
    summary, _ = store._read(output / "run-summary.json")
    if summary["requests_used"] != len(starts) or summary["automatically_adopted"] is not False:
        raise ValueError("SELF_SUMMARY_INVALID")
    if (type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"] < 0
        or summary["stage"] == "COMPLETED" and summary["active_duration_ms"] > authorization.max_active_seconds * 1000):
        raise ValueError("SELF_TIME_BUDGET_INVALID")
    rounds = []
    directories = sorted((output / "rounds").glob("[0-9][0-9][0-9][0-9]"))
    if len(directories) > authorization.max_rounds:
        raise ValueError("SELF_ROUND_BUDGET_INVALID")
    seen, history = set(), []
    for index, directory in enumerate(directories, 1):
        record, _ = store._read(directory / "round-summary.json")
        selected = [c for c in calls if c["round_id"] == index]
        advisor_count = sum(c["role"] == "ADVISOR" for c in selected)
        if advisor_count != 1 and not (not selected and record["stage"] in {"STOPPED", "INTERRUPTED"}):
            raise ValueError("SELF_ADVISOR_COUNT_INVALID")
        saved_feedback, _ = store._read(directory / "feedback.json")
        if saved_feedback != packet:
            raise ValueError("SELF_ROUND_FEEDBACK_INVALID")
        saved_history, _ = store._read(directory / "history.json")
        if saved_history != {"attempts": history}:
            raise ValueError("SELF_ROUND_HISTORY_INVALID")
        from app.modules.evidence_harness.improvement.service import proposal_payload, PATCH_PROMPT, next_feedback, history_entry
        advisor = next((c for c in selected if c["role"] == "ADVISOR"), None)
        if advisor and advisor["request_ref"] != digest([PATCH_PROMPT, proposal_payload(packet, source_context(source), history)]):
            raise ValueError("SELF_ADVISOR_REQUEST_MISMATCH")
        saved_path = directory / "proposal.json"
        proposal = None
        if saved_path.exists() and record["stage"] != "OPTIMIZER_FAILED":
            proposal, _ = store._read(saved_path)
            try:
                candidate = materialize(source, proposal, packet, history=history)
            except Exception as exc:
                if record["stage"] != "REJECTED" or record.get("error_code") != rejection_code(exc):
                    raise ValueError("SELF_REJECTION_BINDING_INVALID") from None
                candidate = None
            if candidate is None:
                if any(c["role"] == "PLANNER" for c in selected):
                    raise ValueError("SELF_UNAUTHORIZED_PLANNING")
                rounds.append({"round_id": index, "decision": "NOT_RUN", "stage": record["stage"],
                               "acceptance_passed": False, "rejection_code": record["error_code"]})
                continue
            if record["stage"] == "REJECTED" or candidate["stage"] == "NO_CHANGE" and record["stage"] != "NO_CHANGE":
                raise ValueError("SELF_PROPOSAL_STAGE_MISMATCH")
            if candidate["stage"] == "VALIDATED" and record["stage"] != "DUPLICATE_PATCH":
                if candidate["patch_ref"] in seen:
                    raise ValueError("SELF_REPEATED_PATCH_EXECUTED")
                seen.add(candidate["patch_ref"])
                if (directory / "candidate.patch").read_text() != candidate["patch"]:
                    raise ValueError("SELF_PATCH_CHANGED")
                baseline_files = experiment["baseline_manifest"]
                for arm in ("control", "candidate"):
                    arm_manifest = file_manifest(directory / arm)
                    expected_manifest = dict(baseline_files)
                    if arm == "candidate":
                        expected_manifest[TARGET] = source_hash(candidate["source"])
                    if arm_manifest != expected_manifest:
                        raise ValueError("SELF_EXECUTED_SOURCE_MISMATCH")
                score = score_round(directory, dataset, authorization.repetitions, selected, stage=record["stage"])
                rounds.append({"round_id": index, **score})
                history.append(history_entry(index, candidate, score))
                packet = next_feedback(packet, directory, index, selected, score=score)
                continue
        if any(c["role"] == "PLANNER" for c in selected):
            raise ValueError("SELF_UNAUTHORIZED_PLANNING")
        rounds.append({"round_id": index, "decision": "NOT_RUN", "stage": record["stage"], "acceptance_passed": False,
                       "diagnosis": proposal.get("diagnosis") if proposal else None,
                       "no_change_reason": proposal.get("no_change_reason") if proposal else None})
    if any(c["round_id"] not in range(1, len(directories) + 1) for c in calls):
        raise ValueError("SELF_CALL_ROUND_INVALID")
    for index in range(1, len(directories) + 1):
        planner = Counter((c["arm"], c["case_id"], c["repetition"]) for c in calls
                          if c["round_id"] == index and c["role"] == "PLANNER")
        slots = {(arm, case.case_id, repetition) for arm in ("control", "candidate")
                 for case in dataset.inputs.cases for repetition in range(1, authorization.repetitions + 1)}
        if any(n > 3 for n in planner.values()) or set(planner) - slots:
            raise ValueError("SELF_PLANNER_LIMIT_INVALID")
    if summary["stage"] != "COMPLETED":
        decision = "INCONCLUSIVE"
    else:
        decision = next((r["decision"] for r in reversed(rounds) if r["decision"] != "NOT_RUN"), "NOT_RUN")
    return {"decision": decision, "stage": summary["stage"], "rounds": rounds,
            "requests_used": len(calls), "failed_model_calls": sum(c["status"] != "COMPLETED" for c in calls),
            "known_tokens": dict(known), "unknown_token_calls": unknown, "token_usage_complete": unknown == 0,
            "active_duration_ms": summary["active_duration_ms"], "stop_reason": summary["stop_reason"],
            "evaluation_level": "INITIAL_ROUTING", "target_file_retrieval": "NOT_EXECUTED",
            "answer_quality": "NOT_EXECUTED", "automatically_adopted": False,
            "candidate_retained": decision == "IMPROVED"}


def compare(output: Path) -> dict:
    """只读失败关闭，避免把验证异常中的私有内容输出到终端。"""
    try:
        return _compare(output)
    except Exception:
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "SELF_VALIDATION_FAILED",
                "automatically_adopted": False}
