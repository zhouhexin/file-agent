"""将已校验路由实验转成机器反馈；事实与推测分开，历史只读。"""

from pathlib import Path

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.routing.comparator import compare
from app.modules.evidence_harness.routing.diagnostics import normalized_intent
from app.modules.llm.diagnostics import SafeErrorDiagnostic


def call_details(calls):
    """逐字段保留受控错误，HTTP状态和解析阶段不能在下一轮反馈中丢失。"""
    result = []
    for call in calls:
        diagnostic = call.get("diagnostic")
        if not isinstance(diagnostic, dict):
            continue
        # 老日志可能多一个固定summary；只投影协议允许字段，再严格验证枚举。
        safe = SafeErrorDiagnostic.model_validate({k: diagnostic[k] for k in SafeErrorDiagnostic.model_fields if k in diagnostic})
        result.append({"category": safe.category, "stage": safe.stage, "exception_type": safe.exception_type,
                       "http_status": safe.http_status,
                       "output_truncated": bool(safe.response_shape and safe.response_shape.output_truncated)})
    return result


def summarize_trial(result, calls):
    """旧记录仅恢复已经保存的意图；没有实际规则轨迹时不冒充已确认根因。"""
    plan = result.get("plan") or {}
    old_intent = ((plan.get("tool_plan") or {}).get("slots") or {}).get("llm_intent_plan") or {}
    trace = result.get("diagnostics") or {}
    model = trace.get("model_route", normalized_intent(old_intent.get("intent")))
    final = normalized_intent(plan.get("intent"))
    failed = sum(c.get("status") != "COMPLETED" for c in calls)
    fallback = trace.get("fallback")
    missing = []
    if not trace:
        missing.append("RULE_TRACE_NOT_RECORDED")
        if fallback is None:
            fallback = True if calls and failed == len(calls) and not old_intent else None
    rules = trace.get("rule_ids") or []
    mismatch = model != "UNKNOWN" and model != final
    reason = ("MODEL_CALL_FAILED" if calls and failed == len(calls) else
              "BACKEND_ROUTE_OVERRIDE" if mismatch else "NO_OVERRIDE_OBSERVED" if model != "UNKNOWN" else "UNKNOWN")
    return {"model_route": model, "final_route": final, "fallback": fallback,
            "failed_calls": failed, "call_count": len(calls), "rule_ids": rules,
            "reason": reason, "certainty": "OBSERVED" if rules or reason == "MODEL_CALL_FAILED" else "PARTIAL",
            "missing_information": missing,
            "strategy_evaluable": bool(calls and not failed and model != "UNKNOWN" and not fallback)}


def task_signal(score_row, observation, *, execution_stage=None):
    """仅投影开发集对错，不向建议器泄漏预期类别、Gold理由或未运行的假分数。"""
    completed = score_row is not None and score_row.get("status") == "COMPLETED"
    outcome = ("CORRECT" if score_row["correct"] else "INCORRECT") if completed else "UNSCORED"
    state = ("MODEL_FAILED" if observation.get("failed_calls", 0) else
             "RULE_FALLBACK" if observation.get("fallback") else
             "COMPLETED" if completed else execution_stage or "NOT_RUN")
    return {"task_outcome": outcome, "execution_state": state,
            "score_source": "INDEPENDENT_DEVELOPMENT_SCORER" if completed else "NO_VERDICT"}


def build_feedback(experiment: Path, *, version="root-cause-feedback-v2") -> dict:
    """先独立核验来源，再逐题整理；不读取Gold内容参与原因猜测。"""
    experiment = _directory(experiment)
    verdict = compare(experiment)
    if verdict["decision"] in {"INVALID_EXPERIMENT"} or verdict.get("execution_stage") == "UNFINALIZED":
        raise ValueError("ROOT_CAUSE_SOURCE_INVALID")
    metadata, _ = store._read(experiment / "experiment.json")
    if version not in {"root-cause-feedback-v1", "root-cause-feedback-v2"}:
        raise ValueError("ROOT_CAUSE_SCHEMA_UNSUPPORTED")
    scores = {(arm, row["case_id"], row["repetition"]): row
              for arm, values in verdict.get("arms", {}).items() for row in values["rows"]}
    calls = [store._read(p)[0] for p in sorted((experiment / "calls").glob("*.finished.json"))]
    items = []
    for arm in ("control", "candidate"):
        for path in sorted((experiment / arm / "rounds").glob("*.json")):
            row, _ = store._read(path)
            selected = [c for c in calls if c.get("arm") == arm and c.get("case_id") == row["case_id"]
                        and c.get("repetition") == row["repetition"]]
            observation = summarize_trial(row["result"], selected)
            # 自由异常文本不外发，只保留既有受控诊断字段。
            details = call_details(selected)
            categories = sorted({d["category"] for d in details})
            items.append({"feedback_id": digest([digest(row), observation]), "case_ref": digest(row["case_id"]),
                          "arm": arm, "repetition": row["repetition"], "source_ref": digest(row),
                          "observation": observation, "error_categories": categories,
                          "call_diagnostics": details,
                          "output_truncated": any(d["output_truncated"] for d in details)})
            if version == "root-cause-feedback-v2":
                items[-1].update(task_signal(scores.get((arm, row["case_id"], row["repetition"])), observation))
                items[-1]["change_kind"] = "PROMPT_STRATEGY"
    packet = {"schema_version": version, "evaluation_level": "INITIAL_ROUTING",
            "source": metadata["source"], "source_experiment_ref": digest(store._read(experiment / "integrity.json")[0]),
            "inputs_sha256": metadata["inputs_sha256"], "code_ref": metadata["configuration"]["code_ref"],
            "items": items, "target_file_retrieval": "NOT_EXECUTED", "answer_quality": "NOT_EXECUTED"}
    if version == "root-cause-feedback-v2":
        packet["source_change_kind"] = "PROMPT_STRATEGY"
        packet["source_comparison"] = {"decision": verdict["decision"],
            "strategy_evaluable": bool(items) and all(i["observation"]["strategy_evaluable"] for i in items)}
    return packet


def prepare_feedback(*, experiment: Path, output: Path) -> dict:
    """独立目录保存反馈和来源绑定，禁止向已有实验内部追加文件。"""
    from app.modules.evidence_harness.improvement.service import new_output
    experiment, output = _directory(experiment), new_output(output)
    packet = build_feedback(experiment)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_new_json(output / "root-cause-feedback.json", packet)
    write_new_json(output / "source.json", {"experiment": experiment.relative_to(store.TEST_ROOT).as_posix(),
                                           "feedback_ref": digest(packet)})
    return {"stage": "ROOT_CAUSE_READY", "items": len(packet["items"]), "model_calls": 0}


def load_feedback(directory: Path) -> dict:
    """反馈必须可从原始实验重建，禁止手写诊断冒充来源。"""
    directory = _directory(directory)
    source, _ = store._read(directory / "source.json")
    packet, _ = store._read(directory / "root-cause-feedback.json")
    rebuilt = build_feedback(store.TEST_ROOT / source["experiment"], version=packet.get("schema_version"))
    if packet != rebuilt or digest(packet) != source["feedback_ref"]:
        raise ValueError("ROOT_CAUSE_FEEDBACK_CHANGED")
    return packet


def load_advisor_feedback(directory: Path) -> dict:
    """旧反馈先按旧协议核验，再从同一来源重建新版模型输入，绝不改写旧文件。"""
    packet = load_feedback(directory)
    if packet["schema_version"] == "root-cause-feedback-v2":
        return packet
    source, _ = store._read(_directory(directory) / "source.json")
    return build_feedback(store.TEST_ROOT / source["experiment"], version="root-cause-feedback-v2")
