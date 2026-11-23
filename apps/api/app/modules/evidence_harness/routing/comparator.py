"""独立首轮路由裁判，从冻结Gold和规划结果重算，失败及未执行计入分母。"""

from collections import Counter
from pathlib import Path
import re
from types import SimpleNamespace

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.routing.dataset import load_dataset
from app.modules.evidence_harness.routing.runtime import STRATEGY_ID, inspect_plan
from app.modules.evidence_harness.routing.schemas import RoutingAuthorization


def _metrics(rows: list[dict]) -> dict:
    """失败轮次没有预测标签，三个路由类别统一计算P/R/F1。"""
    result = {}
    for category in ("CONTENT_ANSWER", "FILE_LIST", "OTHER"):
        tp = sum(r["expected_route"] == category and r["predicted_route"] == category for r in rows)
        fp = sum(r["expected_route"] != category and r["predicted_route"] == category for r in rows)
        fn = sum(r["expected_route"] == category and r["predicted_route"] != category for r in rows)
        result[category] = {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}
    return result


def _compare(directory: Path) -> dict:
    """不使用缓存comparison中的分数，完整性和绑定失败先返回无效实验。"""
    from app.modules.evidence_harness.routing.service import configuration, feedback_payload, validate_proposal
    directory = _directory(directory)
    if not directory.is_dir() or any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("ROUTING_EXPERIMENT_PATH_INVALID")
    if not (directory / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "execution_stage": "UNFINALIZED",
                "evaluation_level": "INITIAL_ROUTING", "auto_resume": False}
    integrity, _ = store._read(directory / "integrity.json")
    actual = {p.relative_to(directory).as_posix(): digest(store._read(p)[0]) for p in directory.rglob("*.json")
              if p not in {directory / "integrity.json", directory / "comparison.json", directory / "manifest.json"}}
    if actual != integrity:
        raise ValueError("ROUTING_ARTIFACT_CHANGED")
    metadata, _ = store._read(directory / "experiment.json")
    if metadata["schema_version"] != "routing-experiment-v1" or metadata["evaluation_level"] != "INITIAL_ROUTING":
        raise ValueError("ROUTING_EXPERIMENT_SCHEMA_INVALID")
    if metadata["control_strategy"] != "baseline" or metadata["candidate_strategy"] != STRATEGY_ID:
        raise ValueError("ROUTING_STRATEGY_MISMATCH")
    dataset = load_dataset(store.TEST_ROOT / metadata["dataset_source"])
    if (dataset.inputs_sha256 != metadata["inputs_sha256"] or dataset.gold_sha256 != metadata["gold_sha256"]
        or (metadata["source"] == "live") != (dataset.inputs.source == "real")
        or metadata["source"] not in {"fake", "live"}):
        raise ValueError("ROUTING_DATASET_MISMATCH")
    config = metadata["configuration"]
    current = configuration(SimpleNamespace(llm_chat_model="", llm_base_url=""), dataset.inputs.catalog_snapshot)
    # 生成时代码指纹是历史事实，新增CLI参数后仍应能核验旧报告。
    # 校验其形态和原配置绑定，比较契约的其余参数继续独立核对。
    if (digest(config) != metadata["configuration_ref"]
        or not re.fullmatch(r"[0-9a-f]{64}", config["code_ref"])
        or any(config[k] != current[k] for k in ("catalog_ref", "dependencies", "timeout_seconds", "max_output_tokens", "max_retries"))):
        raise ValueError("ROUTING_CONFIGURATION_INVALID")
    authorization = RoutingAuthorization.model_validate(metadata["authorization"])
    summary, _ = store._read(directory / "run-summary.json")
    if (summary["stage"] not in {"COMPLETED", "NO_CHANGE", "REJECTED", "OPTIMIZER_FAILED", "INTERRUPTED", "STOPPED", "FAILED"}
        or summary["source"] != metadata["source"] or type(summary["active_duration_ms"]) is not int
        or summary["active_duration_ms"] < 0 or summary["automatically_adopted"] is not False):
        raise ValueError("ROUTING_SUMMARY_INVALID")
    if summary["active_duration_ms"] > authorization.max_active_seconds * 1000 and summary["stage"] == "COMPLETED":
        raise ValueError("ROUTING_TIME_BUDGET_INVALID")
    feedback, _ = store._read(directory / "feedback.json")
    rebuilt = feedback_payload(dataset, [store.TEST_ROOT / p for p in metadata["history_sources"]],
                               metadata["configuration_ref"], metadata["source"])
    if feedback != rebuilt:
        raise ValueError("ROUTING_FEEDBACK_CHANGED")
    if (directory / "proposal.json").exists():
        saved, _ = store._read(directory / "proposal.json")
        proposal, stage, code = validate_proposal(saved["proposal"], feedback)
        if (saved != {"proposal": proposal, "stage": stage, "reason_code": code}
            or summary["validated"] != (stage == "VALIDATED")):
            raise ValueError("ROUTING_PROPOSAL_BINDING_INVALID")
    elif summary["validated"]:
        raise ValueError("ROUTING_PROPOSAL_MISSING")
    starts = {}
    known, unknown, failed_calls = Counter(), 0, 0
    advisor_count = 0
    trial_calls = Counter()
    for path in sorted((directory / "calls").glob("*.started.json")):
        record, _ = store._read(path)
        number = record["sequence"]
        output_limit = (authorization.advisor_max_output_tokens
                        if record["role"] == "ADVISOR" else config["max_output_tokens"])
        if (type(number) is not int or number != len(starts) + 1
            or path.name != f"{number:04d}.started.json"
            or record["timeout_seconds"] != 60 or record["max_output_tokens"] != output_limit
            or type(record["active_ms_before"]) is not int or record["active_ms_before"] < 0
            or record["active_ms_before"] + 62000 > authorization.max_active_seconds * 1000):
            raise ValueError("ROUTING_CALL_JOURNAL_INVALID")
        if record["role"] == "ADVISOR":
            advisor_count += 1
            if number != 1 or record["arm"] is not None or record["case_id"] is not None or record["repetition"] is not None:
                raise ValueError("ROUTING_ADVISOR_BINDING_INVALID")
        elif record["role"] == "PLANNER":
            key = (record["arm"], record["case_id"], record["repetition"])
            trial_calls[key] += 1
        else:
            raise ValueError("ROUTING_CALL_ROLE_INVALID")
        starts[number] = record
        finish_path = path.with_name(path.name.replace("started", "finished"))
        if not finish_path.exists():
            unknown += 1
            continue
        finish, _ = store._read(finish_path)
        if any(finish.get(k) != v for k, v in record.items()):
            raise ValueError("ROUTING_CALL_BINDING_INVALID")
        if finish["status"] not in {"COMPLETED", "FAILED"}:
            raise ValueError("ROUTING_CALL_STATUS_INVALID")
        failed_calls += finish["status"] == "FAILED"
        values = [finish.get(k) for k in ("input_tokens", "output_tokens")]
        if any(v is not None and (type(v) is not int or v < 0) for v in values):
            raise ValueError("ROUTING_TOKEN_USAGE_INVALID")
        if finish["token_usage_complete"] != all(v is not None for v in values):
            raise ValueError("ROUTING_TOKEN_COMPLETENESS_INVALID")
        unknown += not finish["token_usage_complete"]
        for name, value in zip(("input_tokens", "output_tokens"), values):
            if value is not None:
                known[name] += value
    if len(list((directory / "calls").glob("*.finished.json"))) > len(starts):
        raise ValueError("ROUTING_FINISH_WITHOUT_START")
    if (advisor_count > 1 or (starts and advisor_count != 1) or len(starts) > authorization.max_total_requests
        or summary["requests_used"] != len(starts)
        or summary["model_calls"] != (len(starts) if metadata["source"] == "live" else 0)
        or summary["fake_calls"] != (len(starts) if metadata["source"] == "fake" else 0)):
        raise ValueError("ROUTING_REQUEST_BUDGET_INVALID")
    cases = {c.case_id: c for c in dataset.inputs.cases}
    gold = {g.case_id: g.expected_route for g in dataset.gold}
    slots = {(arm, cid, n) for arm in ("control", "candidate") for cid in cases
             for n in range(1, authorization.repetitions + 1)}
    if set(trial_calls) - slots or any(v > 3 for v in trial_calls.values()):
        raise ValueError("ROUTING_TRIAL_REQUESTS_INVALID")
    records = {}
    for arm in ("control", "candidate"):
        for path in (directory / arm / "rounds").glob("*.json"):
            row, _ = store._read(path)
            key = (row["arm"], row["case_id"], row["repetition"])
            if key not in slots or key[0] != arm or key in records:
                raise ValueError("ROUTING_ROUND_SLOT_INVALID")
            if row["case_input_ref"] != digest(cases[key[1]].model_dump(mode="json")):
                raise ValueError("ROUTING_ROUND_INPUT_CHANGED")
            result = row["result"]
            if result["status"] == "COMPLETED":
                observed = inspect_plan(cases[key[1]], result["plan"])
                if any(result.get(k) != v for k, v in observed.items()):
                    raise ValueError("ROUTING_ROUND_SCORE_CHANGED")
            elif result["status"] != "FAILED":
                raise ValueError("ROUTING_ROUND_STATUS_INVALID")
            records[key] = row
    if not summary["validated"] and (records or trial_calls):
        raise ValueError("ROUTING_UNAUTHORIZED_TRIAL")
    if summary["stage"] == "COMPLETED" and set(records) != slots:
        raise ValueError("ROUTING_COMPLETED_ROUNDS_MISSING")
    arms = {}
    safety_codes = {"UNKNOWN_DOCUMENT_ID", "EXPLICIT_SCOPE_CHANGED", "WRITE_ROUTE_BLOCKED", "CONFIRMATION_REQUIRED"}
    safety_failures = 0
    for arm in ("control", "candidate"):
        rows = []
        for cid in cases:
            for repetition in range(1, authorization.repetitions + 1):
                row = records.get((arm, cid, repetition))
                result = row["result"] if row else {"status": "NOT_RUN"}
                success = result["status"] == "COMPLETED" and result["scope_valid"]
                predicted = result["route"] if success else None
                if arm == "candidate":
                    safety_failures += bool(safety_codes.intersection(result.get("violation_codes", [])))
                rows.append({"case_id": cid, "repetition": repetition, "status": result["status"],
                    "expected_route": gold[cid], "predicted_route": predicted,
                    "correct": success and predicted == gold[cid],
                    "duration_ms": row["duration_ms"] if row else None,
                    "violation_codes": result.get("violation_codes", [])})
        arms[arm] = {"planned": len(rows), "recorded": sum(r["status"] != "NOT_RUN" for r in rows),
                     "correct": sum(r["correct"] for r in rows), "metrics": _metrics(rows), "rows": rows}
    regressions = [cid for cid in cases if
        sum(r["correct"] for r in arms["candidate"]["rows"] if r["case_id"] == cid) <
        sum(r["correct"] for r in arms["control"]["rows"] if r["case_id"] == cid)]
    if not summary["validated"]:
        decision = "NOT_RUN"
    elif summary["stage"] != "COMPLETED":
        decision = "INCONCLUSIVE"
    elif regressions or safety_failures:
        decision = "REGRESSION"
    elif arms["candidate"]["correct"] > arms["control"]["correct"]:
        decision = "IMPROVED"
    else:
        decision = "NO_GAIN"
    return {"decision": decision, "execution_stage": summary["stage"], "evaluation_level": "INITIAL_ROUTING",
        "arms": arms, "regression_case_ids": regressions, "candidate_safety_failures": safety_failures,
        "requests_used": len(starts), "failed_model_calls": failed_calls,
        "known_tokens": dict(known), "unknown_token_calls": unknown,
        "token_usage_complete": unknown == 0, "active_duration_ms": summary["active_duration_ms"],
        "stop_reason": summary["stop_reason"], "automatically_adopted": False,
        "target_file_retrieval": "NOT_EXECUTED", "answer_quality": "NOT_EXECUTED"}


def compare(directory: Path) -> dict:
    """只读失败关闭，错误只输出固定说明，避免转储任务正文或凭证。"""
    try:
        return _compare(directory)
    except Exception:
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "ROUTING_VALIDATION_FAILED",
                "evaluation_level": "INITIAL_ROUTING", "automatically_adopted": False}
