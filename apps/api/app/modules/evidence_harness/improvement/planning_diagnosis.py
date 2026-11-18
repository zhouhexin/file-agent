"""在可信宿主重验规划参数，把可核实的约束错误投影为开发反馈，不执行业务Tool。"""

from pydantic import ValidationError

from app.modules.agent.tool_registry import ToolRegistry
from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness.improvement.feedback import call_details, task_signal
from app.modules.evidence_harness.improvement.planning_feedback import task_diagnostic


# 只翻译真实校验器的固定消息；未知异常不外发，也不推测业务根因。
RULES = {
    "show_all_results requires a resolved show-all-results clarification": (
        "SHOW_ALL_REQUIRES_RESOLVED_CLARIFICATION", "展示全部结果必须引用后端真实完成的全部结果澄清选择，不能编造澄清ID。"),
    "clarification id and option id must be provided together": (
        "CLARIFICATION_PAIR_REQUIRED", "澄清ID与选项ID必须成对提供，并来自真实上下文。"),
    "AUTO search cannot include explicit phrases": (
        "AUTO_WITH_EXPLICIT_PHRASES", "AUTO模式不能同时带显式phrases。"),
}


def _field_path(schema, location):
    """字段名只能取真实schema，额外键可能含私有输入，统一隐藏；数组只保留整数位置。"""
    node, path = schema, []
    for part in location:
        while "$ref" in node:
            node = schema.get("$defs", {}).get(node["$ref"].rsplit("/", 1)[-1], {})
        if "anyOf" in node:
            node = next((v for v in node["anyOf"] if v.get("type") != "null"), {})
            while "$ref" in node:
                node = schema.get("$defs", {}).get(node["$ref"].rsplit("/", 1)[-1], {})
        if isinstance(part, int) and node.get("type") == "array":
            path.append(part)
            node = node.get("items", {})
        elif isinstance(part, str) and part in node.get("properties", {}):
            path.append(part)
            node = node["properties"][part]
        else:
            path.append("<unrecognized-field>")
            break
    return path


def parameter_diagnostics(result):
    """复用实际输入模型定位错误；不含错误输入值、异常原文或候选自报根因。"""
    registry, rows = ToolRegistry(), []
    steps = ((result.get("plan") or {}).get("tool_plan") or {}).get("steps") or []
    for index, step in enumerate(steps):
        try:
            definition = registry.get(step.get("tool_name"))
        except Exception:
            rows.append({"step_index": index, "tool_name": "UNKNOWN", "code": "TOOL_NOT_REGISTERED"})
            continue
        if index and step.get("bindings"):
            continue  # 和真实首轮检查一致：后续动态绑定的输入尚未求解。
        try:
            definition.input_model.model_validate(step.get("input") or {})
        except ValidationError as exc:
            schema = definition.input_model.model_json_schema()
            for error in exc.errors(include_input=False, include_url=False):
                context = error.get("ctx") or {}
                rule = RULES.get(str(context.get("error", "")))
                row = {"step_index": index, "tool_name": definition.name,
                    "field_path": _field_path(schema, error["loc"]), "error_type": error["type"],
                    "code": rule[0] if rule else "TOOL_PARAMETER_CONSTRAINT",
                    "certainty": "OBSERVED", "source": "ACTUAL_TOOL_INPUT_VALIDATION"}
                if rule:
                    row["constraint"] = rule[1]
                row["limits"] = {k: v for k, v in context.items()
                    if k in {"min_length", "max_length", "ge", "gt", "le", "lt"} and type(v) in {int, float}}
                rows.append(row)
        except Exception:
            rows.append({"step_index": index, "tool_name": definition.name, "code": "TOOL_VALIDATION_UNCLASSIFIED"})
    return rows


def tool_contracts():
    """向建议器展示实际只读入口的输入schema，提供通用契约而非针对错题的人工修复。"""
    registry = ToolRegistry()
    return {name: registry.get(name).input_model.model_json_schema()
            for name in ("hybrid-search", "managed-file-list", "evidence-answer")}


def feedback_packet(cases, records, scores, calls):
    """调用方先切成开发集；这里只投影对错，不接受预期路由、人工答案或理由。"""
    lookup = {c.case_id: c for c in cases}
    score_lookup = {(r["arm"], r["case_id"], r["repetition"]): r for r in scores}
    items = []
    for record in sorted(records, key=lambda r: (r["arm"], r["case_id"], r["repetition"])):
        if record["case_id"] not in lookup:
            raise ValueError("ITERATION_NON_DEVELOPMENT_FEEDBACK")
        arm, cid, n = record["arm"], record["case_id"], record["repetition"]
        result = record["result"]
        selected = [c for c in calls if (c.get("arm"), c.get("case_id"), c.get("repetition")) == (arm, cid, n)]
        audit = result.get("planning_audit") or {}
        item = {"case_ref": digest(cid), "arm": arm, "repetition": n,
            "effective_mode": audit.get("effective_mode", "UNAVAILABLE"),
            "fallback_kind": audit.get("fallback_kind", "UNKNOWN"),
            "validation_events": audit.get("validation_events", []),
            "task_diagnostic": task_diagnostic(lookup[cid], result),
            "parameter_diagnostics": parameter_diagnostics(result),
            "violation_codes": result.get("violation_codes", []), "call_diagnostics": call_details(selected),
            "optimization_scope": "OUTSIDE_PROMPT_POLICY" if audit.get("effective_mode") == "PREFLIGHT" else "PROMPT_POLICY_UNVERIFIED",
            **task_signal(score_lookup.get((arm, cid, n)), {"failed_calls": sum(c["status"] != "COMPLETED" for c in selected),
                "fallback": bool((result.get("diagnostics") or {}).get("fallback"))})}
        item["feedback_id"] = digest(item)
        items.append(item)
    return {"schema_version": "adaptive-iteration-feedback-v1", "split": "DEVELOPMENT",
        "evaluation_level": "INITIAL_ROUTING", "holdout_usage": "NOT_READ_BY_ADVISOR", "items": items}
