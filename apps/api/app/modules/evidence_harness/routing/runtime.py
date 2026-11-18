"""用现有LangGraph规划节点复测候选提示策略，图中只有规划边且不调用Tool。"""

from langgraph.graph import StateGraph, START, END

from app.core.logging import log_context
from app.modules.agent.adaptive_planner import AdaptivePlannerService
from app.modules.agent.context import AgentContextLoader
from app.modules.agent.graph import planning
from app.modules.agent.planner import DeterministicPlanner
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentGraphState
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.llm.service import LLMIntentService
from app.modules.evidence_harness.routing.diagnostics import PlanningTrace
from app.modules.evidence_harness.routing.diagnostics import normalized_intent

STRATEGY_ID = "content_question_priority_v1"
STRATEGY_HINT = """
本次采用正文任务优先的路由策略。
区分用户要列出文件本身，还是列出文件正文中的人员、条件、人数、时间、金额或原文依据。
后者属于证据回答目标，不能仅因句中含“列出”“目录”“2024年”等字样选择managed-file-list。
有后端确认的文件范围时沿用该范围，无范围时通过证据发现路线先查找所指来源。
用户只要求找文件或文件清单时仍走文件搜索或列表。缺少依据时保留澄清，不编造ID。
所有Tool、输入schema、权限、风险及确认要求仍以本次Catalog和后端校验为准。
"""


def classify_route(intent: str, entry_tool: str) -> str:
    """依据实际计划入口分类，人工预期标签不参与分类。"""
    if entry_tool == "evidence-answer":
        return "CONTENT_ANSWER"
    if entry_tool == "hybrid-search":
        return "CONTENT_ANSWER" if normalized_intent(intent) == "CONTENT_ANSWER" else "FILE_LIST"
    if entry_tool == "managed-file-list":
        return "FILE_LIST"
    return "OTHER"


def _document_ids(value) -> set[str]:
    """检查声明式计划内所有ID字段，不能只验证表层slots而漏掉实际Tool参数。"""
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "document_id" and isinstance(item, str):
                result.add(item)
            elif key == "document_ids" and isinstance(item, list):
                if any(not isinstance(i, str) for i in item):
                    raise ValueError("ROUTING_DOCUMENT_ID_INVALID")
                result.update(item)
            else:
                result.update(_document_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_document_ids(item))
    return result


def inspect_plan(case, update: dict, registry=None) -> dict:
    """真实Tool定义校验首轮计划和原附件范围，所有handler均未执行。"""
    registry = registry or ToolRegistry()
    plan = update.get("tool_plan") or {}
    steps = plan.get("steps") or []
    entry = steps[0].get("tool_name", "OTHER") if steps else "OTHER"
    route = classify_route(str(update.get("intent") or ""), entry)
    violations = set()
    if update.get("decision_type") == "TOOL_PLAN":
        for step in steps:
            name = step.get("tool_name")
            try:
                definition = registry.get(name)
                if name not in {"hybrid-search", "managed-file-list", "evidence-answer"}:
                    violations.add("UNSUPPORTED_ROUTE_TOOL")
                if definition.requires_confirmation or step.get("requires_confirmation"):
                    violations.add("CONFIRMATION_REQUIRED")
                # 三个观察入口在生产中可能写审计或回答记录；路由图不执行它们。
                # 文件变更等其余有副作用Tool仍按危险路线处理。
                if definition.side_effects and name not in {"hybrid-search", "managed-file-list", "evidence-answer"}:
                    violations.add("WRITE_ROUTE_BLOCKED")
                # 规划图不求解后续绑定，首步必须有完整参数且通过真实输入schema。
                if step is steps[0] or not step.get("bindings"):
                    definition.input_model.model_validate(step.get("input") or {})
            except Exception:
                violations.add("TOOL_SCHEMA_INVALID")
    explicit = {d.document_id for d in case.attachments}
    available = explicit | {d.document_id for d in case.context_documents}
    ids = _document_ids({"steps": steps, "scope": (update.get("planner_decision") or {}).get("scope", {})})
    if not ids <= available:
        violations.add("UNKNOWN_DOCUMENT_ID")
    # 只有真实Tool参数内的范围才能满足明确附件，顶层scope自述不能替代调用参数。
    used = _document_ids(steps)
    if explicit and used != explicit:
        violations.add("EXPLICIT_SCOPE_CHANGED")
    return {"route": route, "entry_tool": entry if entry in {"hybrid-search", "managed-file-list", "evidence-answer"} else "OTHER",
            "scope_valid": not violations, "violation_codes": sorted(violations),
            "target_file_retrieval": "NOT_EXECUTED", "answer_quality": "NOT_EXECUTED"}


def run_planning(case, *, catalog: dict, client, settings, capture_audit=False, quiet=False) -> dict:
    """原问题只进入原规划服务，小型图在首轮规划完成后结束。"""
    effective = settings.model_copy(update={"llm_enabled": True, "adaptive_planner_mode": case.adaptive_mode})
    trace = PlanningTrace(client, capture=capture_audit)
    registry = ToolRegistry()
    context = AgentRuntimeContext(
        planner=DeterministicPlanner(), registry=registry, context_loader=AgentContextLoader(),
        llm_intent_service=LLMIntentService(settings=effective, client=trace),
        adaptive_planner_service=AdaptivePlannerService(settings=effective, client=trace),
        catalog_snapshot=catalog, adaptive_planner_mode=case.adaptive_mode,
        adaptive_planner_schema_version=effective.adaptive_planner_schema_version,
        classification_service=None, document_summary_service=None,
    )
    state = {"agent_run_id": "routing-offline", "conversation_id": "routing-offline",
             "message_id": case.source_run_ref, "user_id": "routing-offline", "message": case.question,
             "attachments": [d.model_dump() for d in case.attachments],
             "context_documents": [d.model_dump() for d in case.context_documents],
             "planner_mode": "llm", "planning_round": 0, "observation": {}, "observed_document_ids": []}
    graph = StateGraph(AgentGraphState, context_schema=AgentRuntimeContext)
    graph.add_node("planning", planning)
    graph.add_edge(START, "planning")
    graph.add_edge("planning", END)
    with log_context(metadata_only=True, offline_silent=quiet), trace:
        result = graph.compile().invoke(state, context=context)
    observed = inspect_plan(case, result, registry)
    output = {"status": "COMPLETED", "plan": {key: result.get(key) for key in
            ("intent", "decision_type", "tool_plan", "planner_decision")},
            "diagnostics": trace.summary(result.get("intent"), result.get("user_intent_plan")), **observed}
    if capture_audit:
        output["planning_audit"] = trace.audit(case.adaptive_mode, result.get("user_intent_plan"))
    return output
