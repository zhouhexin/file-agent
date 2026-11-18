"""可信宿主为建议器生成有界历史视图；完整试次仍留在原实验，不用摘要替代评分。"""

from collections import Counter, defaultdict
from copy import deepcopy
import json

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.executor import ExperimentStopped

POLICY = {"version": "campaign-advisor-history-v1", "request_characters": 360000,
    "history_characters": 200000, "example_characters": 12000,
    "examples_per_attempt": 2, "instruction_limits": [1600, 800, 0], "maximum_attempts": 100,
    "current_feedback": "FULL", "counts": "ALL_OBSERVATIONS", "summary_provider": "DETERMINISTIC_LOCAL"}


def request_characters(prompt, payload):
    """与Journal同口径计量Python结构表示的字符数，不把字符数冒充Token数。"""
    return len(prompt) + len(str(payload))


def observation_summary(packet):
    """全部观察都进入分组计数，多次调用/约束事件单列，不把示例数量当失败总数。"""
    counts, totals = defaultdict(Counter), Counter()

    def add(arm, name, value):
        """只统计受控诊断字段，禁止把原问题、人工Gold或异常原文拼入分组键。"""
        counts[arm, name][json.dumps(value, ensure_ascii=False, sort_keys=True)] += 1

    items = packet.get("items", [])
    for item in items:
        arm = item["arm"]
        totals[arm] += 1
        for name in ("task_outcome", "execution_state", "effective_mode", "fallback_kind", "optimization_scope"):
            add(arm, name, item.get(name))
        task = item.get("task_diagnostic") or {}
        contract = task.get("intent_contract") or {}
        add(arm, "intent_contract", {k: contract.get(k) for k in (
            "mapping_status", "model_application_mapping", "backend_application_mapping", "observed_initial_route")})
        for name, events, fields in (
            ("mismatch_events", task.get("mismatch_observations", []), ("code", "certainty", "source")),
            ("parameter_events", item.get("parameter_diagnostics", []),
                ("tool_name", "code", "field_path", "error_type", "limits", "constraint", "certainty")),
            ("call_events", item.get("call_diagnostics", []),
                ("category", "stage", "exception_type", "http_status", "output_truncated")),
            ("validation_events", item.get("validation_events", []), ("phase", "status")),
        ):
            for event in events:
                add(arm, name, {k: event[k] for k in fields if k in event})
        for code in item.get("violation_codes", []):
            add(arm, "violation_events", code)
    return {"source_ref": digest(packet), "item_count": len(items), "arms": {
        arm: {"items": totals[arm], "counts": {name: [
            {"value": json.loads(value), "count": n} for value, n in sorted(counts[arm, name].items())]
            for current, name in sorted(counts) if current == arm}}
        for arm in sorted(totals)}, "examples": [], "causal_explanation": "NOT_VERIFIED"}


def _instruction(text, limit):
    """只节选展示文字并明确省略，不改写用于冻结、去重或执行的完整策略。"""
    shown = text if len(text) <= limit else text[:limit // 2] + text[-(limit - limit // 2):] if limit else ""
    return shown, {"source_ref": digest(text), "source_characters": len(text),
        "omitted_characters": len(text) - len(shown),
        "mode": "FULL" if shown == text else "HEAD_AND_TAIL" if shown else "REFERENCE_ONLY"}


def project(base_payload, history, prompt):
    """保留所有尝试与完整错误计数，只在明确的长度额度内展开历史示例。"""
    if len(history) > POLICY["maximum_attempts"]:
        raise ExperimentStopped("ADVISOR_HISTORY_ATTEMPT_LIMIT")
    profiles, prepared = {}, []
    for item in history:
        entry = {k: deepcopy(item[k]) for k in ("origin", "round_id", "attempt", "policy_ref", "decision",
            "strategy_evaluable", "regression_case_refs", "candidate_parameter_failures", "proposal_stage", "outcome") if k in item}
        entry["source_ref"] = digest(item)
        if "arms" in item:
            entry["arms"] = {arm: {k: value[k] for k in ("correct", "planned") if k in value}
                for arm, value in item["arms"].items()}
        if "profile" in item:
            ref = digest(item["profile"])
            profiles[ref] = deepcopy(item["profile"])
            entry["profile_ref"] = ref
        if isinstance(item.get("observations"), dict):
            entry["observations"] = observation_summary(item["observations"])
        prepared.append(entry)
    projection = {"schema_version": POLICY["version"], "policy": deepcopy(POLICY),
        "source_ref": digest(history), "source_characters": len(str(history)), "attempts": len(history),
        "profiles": profiles, "example_count": 0, "omitted_instruction_characters": 0,
        "detail_scope": "BOUNDED_EXAMPLES_NOT_ALL_RECORDS", "counts_scope": "ALL_HISTORY_OBSERVATIONS"}
    payload = {**deepcopy(base_payload), "history": [], "history_projection": projection}
    if request_characters(prompt, payload) > POLICY["request_characters"]:
        raise ExperimentStopped("ADVISOR_REQUIRED_CONTEXT_TOO_LARGE")

    def fits():
        """不扩大400000宿主边界，历史和整条请求分别受更小的固定额度限制。"""
        return (len(str({k: payload[k] for k in ("history", "history_projection")})) <= POLICY["history_characters"]
            and request_characters(prompt, payload) <= POLICY["request_characters"])

    for limit in POLICY["instruction_limits"]:
        payload["history"] = deepcopy(prepared)
        omitted = 0
        for original, entry in zip(history, payload["history"]):
            if isinstance(original.get("instruction"), str):
                entry["instruction"], entry["instruction_projection"] = _instruction(original["instruction"], limit)
                omitted += entry["instruction_projection"]["omitted_characters"]
        projection["omitted_instruction_characters"] = omitted
        if fits():
            break
    else:
        raise ExperimentStopped("ADVISOR_HISTORY_SUMMARY_TOO_LARGE")

    used = 0
    # 近期失败优先展开，但所有早期/正确/回退观察都已计数，不能通过挑示例改成绩。
    for index in reversed(range(len(history))):
        packet = history[index].get("observations") or {}
        items = sorted(packet.get("items", []), key=lambda i: (
            i.get("task_outcome") == "CORRECT", i.get("arm") != "candidate", i.get("feedback_id", "")))
        examples = payload["history"][index].get("observations", {}).get("examples")
        if examples is None:
            continue
        for example in items:
            size = len(str(example))
            if len(examples) >= POLICY["examples_per_attempt"]:
                break
            if used + size > POLICY["example_characters"]:
                continue
            examples.append(deepcopy(example))
            projection["example_count"] += 1
            if not fits():
                examples.pop()
                projection["example_count"] -= 1
            else:
                used += size
    return payload


def audit(prompt, payload):
    """留存模型实际看到的视图和输入身份，比较器必须独立重建后逐字段核对。"""
    return {"schema_version": "campaign-advisor-memory-audit-v1", "history": deepcopy(payload["history"]),
        "history_projection": deepcopy(payload["history_projection"]), "request_ref": digest([prompt, payload]),
        "request_characters": request_characters(prompt, payload), "request_limit": POLICY["request_characters"]}
