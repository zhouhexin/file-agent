"""MVP File Agent Runtime 的 LangGraph 状态图。

当前图保持最小实现，但已经拆分 intake、planning、Tool dispatch、证据/变更处理和响应生成等边界。
"""

from __future__ import annotations

from typing import Any, Dict, List

from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime

from app.modules.agent.planner import build_plan_from_user_intent
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentGraphState


def build_agent_graph():
    """编译 MVP LangGraph 工作流。"""

    graph = StateGraph(AgentGraphState, context_schema=AgentRuntimeContext)
    graph.add_node("chat_intake", chat_intake)
    graph.add_node("collect_context", collect_context)
    graph.add_node("planning", planning)
    graph.add_node("tool_dispatch", tool_dispatch)
    graph.add_node("async_job_wait", async_job_wait)
    graph.add_node("evidence_or_change", evidence_or_change)
    graph.add_node("response", response)

    graph.set_entry_point("chat_intake")
    graph.add_edge("chat_intake", "collect_context")
    graph.add_edge("collect_context", "planning")
    graph.add_edge("planning", "tool_dispatch")
    graph.add_edge("tool_dispatch", "async_job_wait")
    graph.add_edge("async_job_wait", "evidence_or_change")
    graph.add_edge("evidence_or_change", "response")
    graph.add_edge("response", END)
    return graph.compile()


def chat_intake(state: AgentGraphState) -> Dict[str, Any]:
    """在规划前初始化运行状态。

    此节点不执行副作用，只负责把运行状态带入受控 Planner 路径。
    """

    return {
        "status": "PLANNING",
        "errors": state.get("errors", []),
        "tool_results": state.get("tool_results", []),
        "tool_invocations": state.get("tool_invocations", []),
    }


def collect_context(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """加载 LLM 理解用户需求所需的文件上下文。"""

    return {
        "context_documents": runtime.context.context_loader.load_documents(
            user_id=state["user_id"],
            attachments=state.get("attachments", []),
        )
    }


def planning(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """调用 Planner，并且只保存通过校验的声明式计划。"""

    if state.get("planner_mode") == "llm":
        intent_plan = runtime.context.llm_intent_service.understand_user_request(
            message=state["message"],
            attachments=state.get("attachments", []),
            context_documents=state.get("context_documents", []),
        )
        plan = build_plan_from_user_intent(
            intent_plan=intent_plan,
            message=state["message"],
            attachments=state.get("attachments", []),
        )
        user_intent_plan = intent_plan.model_dump()
    else:
        plan = runtime.context.planner.plan(
            conversation_id=state["conversation_id"],
            user_id=state["user_id"],
            message_id=state["message_id"],
            message=state["message"],
            attachments=state.get("attachments", []),
        )
        user_intent_plan = {}
    return {
        "intent": plan.intent,
        "slots": plan.slots,
        "selected_skills": plan.selected_skills,
        "tool_plan": plan.model_dump(),
        "user_intent_plan": user_intent_plan,
        "status": "RUNNING_TOOL",
    }


def tool_dispatch(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """通过白名单 Registry 执行不需要确认的 Tool 步骤。"""

    registry = runtime.context.registry
    tool_results: List[Dict[str, Any]] = []
    tool_invocations: List[Dict[str, Any]] = []
    operation_plan_id = state.get("operation_plan_id")
    changeset_id = state.get("changeset_id")

    for step in state["tool_plan"]["steps"]:
        if step["requires_confirmation"]:
            operation_plan_id = operation_plan_id or "operation-plan-pending"
            continue
        invocation = registry.invoke(step["tool_name"], step["input"])
        invocation_json = invocation.model_dump()
        tool_invocations.append(invocation_json)
        tool_results.append(invocation.output_json)
        changeset_id = invocation.changeset_id or changeset_id
        operation_plan_id = invocation.operation_plan_id or operation_plan_id

    return {
        "tool_results": tool_results,
        "tool_invocations": tool_invocations,
        "changeset_id": changeset_id,
        "operation_plan_id": operation_plan_id,
        "status": "SUMMARIZING",
    }


def async_job_wait(state: AgentGraphState) -> Dict[str, Any]:
    """异步任务边界占位，后续用于接入 processing job。"""

    return {"status": "SUMMARIZING"}


def evidence_or_change(state: AgentGraphState) -> Dict[str, Any]:
    """为响应节点收集 evidence、ChangeSet 和 OperationPlan 标识。"""

    return {
        "changeset_id": state.get("changeset_id"),
        "operation_plan_id": state.get("operation_plan_id"),
    }


def response(state: AgentGraphState) -> Dict[str, Any]:
    """生成面向用户的最终运行摘要。"""

    invocation_count = len(state.get("tool_invocations", []))
    insight_documents = _insight_documents_from_results(state.get("tool_results", []))
    if insight_documents:
        filenames = [
            item.get("filename") or item.get("document_id")
            for item in insight_documents
        ]
        return {
            "status": "COMPLETED",
            "final_response": f"已读取 {len(insight_documents)} 个文件的基础洞察：{', '.join(filenames)}。",
        }
    return {
        "status": "COMPLETED",
        "final_response": f"AgentRun completed with {invocation_count} tool invocation(s).",
    }


def _insight_documents_from_results(tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 Tool 结果中提取 read-document-insights 返回的文件列表。"""

    documents: List[Dict[str, Any]] = []
    for result in tool_results:
        result_documents = result.get("documents")
        if isinstance(result_documents, list):
            documents.extend([item for item in result_documents if isinstance(item, dict)])
    return documents
