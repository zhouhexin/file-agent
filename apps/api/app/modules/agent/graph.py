"""MVP File Agent Runtime 的 LangGraph 状态图。

当前图保持最小实现，但已经拆分 intake、planning、Tool dispatch、证据/变更处理和响应生成等边界。
"""

from __future__ import annotations

from typing import Any, Dict, List

from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime

from app.modules.agent.document_classifier import classify_document_text
from app.modules.agent.planner import build_plan_from_user_intent
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentGraphState, ToolInvocationRecord


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
        try:
            invocation = registry.invoke(step["tool_name"], step["input"])
        except Exception as exc:
            if step["tool_name"] != "extract-document-text":
                raise
            invocation = _failed_tool_invocation(step=step, error=exc)
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


def _failed_tool_invocation(*, step: Dict[str, Any], error: Exception) -> ToolInvocationRecord:
    """把单个 Tool 异常转成结构化失败记录，避免批量任务被一个文件阻断。"""

    tool_input = step.get("input", {})
    document_id = str(tool_input.get("document_id") or "")
    return ToolInvocationRecord(
        tool_name=step.get("tool_name", "unknown-tool"),
        input_json=tool_input,
        output_json={
            "ok": False,
            "document_id": document_id,
            "extraction_run_id": f"failed-{step.get('step_id', 'unknown')}",
            "status": "FAILED",
            "extractor": step.get("tool_name", "unknown-tool"),
            "pages": [],
            "error": {
                "code": "TOOL_EXECUTION_FAILED",
                "message": str(error),
            },
        },
        status="FAILED",
    )


def async_job_wait(state: AgentGraphState) -> Dict[str, Any]:
    """异步任务边界占位，后续用于接入 processing job。"""

    return {"status": "SUMMARIZING"}


def evidence_or_change(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """为响应节点收集 evidence、ChangeSet 和 OperationPlan 标识。"""

    extraction_run_ids = [
        str(result.get("extraction_run_id"))
        for result in state.get("tool_results", [])
        if result.get("extraction_run_id")
    ]
    document_results = _document_results_from_extraction_results(
        tool_results=state.get("tool_results", []),
        context_documents=state.get("context_documents", []),
        extraction_texts=runtime.context.context_loader.load_extraction_texts(
            extraction_run_ids=extraction_run_ids,
        ),
    )
    return {
        "changeset_id": state.get("changeset_id"),
        "operation_plan_id": state.get("operation_plan_id"),
        "document_results": document_results,
    }


def response(state: AgentGraphState) -> Dict[str, Any]:
    """生成面向用户的最终运行摘要。"""

    invocation_count = len(state.get("tool_invocations", []))
    document_results = state.get("document_results", [])
    if document_results:
        return {
            "status": "COMPLETED",
            "final_response": _build_document_results_response(document_results),
        }

    extraction_results = _extraction_results_from_results(state.get("tool_results", []))
    if extraction_results:
        return {
            "status": "COMPLETED",
            "final_response": _build_extraction_response(extraction_results),
        }

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


def _extraction_results_from_results(tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 Tool 结果中提取 extract-document-text 返回的解析结果。"""

    return [
        result
        for result in tool_results
        if result.get("extraction_run_id") and result.get("status") in {"COMPLETED", "FAILED"}
    ]


def _build_extraction_response(extraction_results: List[Dict[str, Any]]) -> str:
    """生成文件解析 Tool 的用户回执。"""

    failed_messages = [
        (result.get("error") or {}).get("message") or "未知错误"
        for result in extraction_results
        if result.get("status") == "FAILED"
    ]
    completed_results = [result for result in extraction_results if result.get("status") == "COMPLETED"]
    if not completed_results and failed_messages:
        return f"文件解析失败：{failed_messages[0]}。"

    page_count = sum(len(result.get("pages", [])) for result in completed_results)
    char_count = sum(
        int(page.get("char_count", 0))
        for result in completed_results
        for page in result.get("pages", [])
    )
    response_text = f"已解析 {len(completed_results)} 个文件，提取 {page_count} 页/Sheet，共 {char_count} 个字符。"
    if failed_messages:
        response_text += f" 另有 {len(failed_messages)} 个文件解析失败：{failed_messages[0]}。"
    return response_text


def _insight_documents_from_results(tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 Tool 结果中提取 read-document-insights 返回的文件列表。"""

    documents: List[Dict[str, Any]] = []
    for result in tool_results:
        result_documents = result.get("documents")
        if isinstance(result_documents, list):
            documents.extend([item for item in result_documents if isinstance(item, dict)])
    return documents


def _document_results_from_extraction_results(
    *,
    tool_results: List[Dict[str, Any]],
    context_documents: List[Dict[str, Any]],
    extraction_texts: Dict[str, str] | None = None,
) -> List[Dict[str, Any]]:
    """把正文解析 Tool 输出聚合成逐文件业务结果。"""

    extraction_texts = extraction_texts or {}
    document_lookup = {
        str(document.get("document_id")): document
        for document in context_documents
        if document.get("document_id")
    }
    document_results: List[Dict[str, Any]] = []
    for result in _extraction_results_from_results(tool_results):
        document_id = str(result.get("document_id") or "")
        document_context = document_lookup.get(document_id, {})
        pages = [page for page in result.get("pages", []) if isinstance(page, dict)]
        char_count = sum(int(page.get("char_count", 0) or 0) for page in pages)
        text_preview = "\n".join(str(page.get("text_preview") or "") for page in pages)
        classification_text = extraction_texts.get(str(result.get("extraction_run_id") or "")) or text_preview
        error = result.get("error") if isinstance(result.get("error"), dict) else None
        document_results.append(
            {
                "document_id": document_id,
                "filename": document_context.get("filename") or document_id,
                "extraction_status": result.get("status"),
                "extractor": result.get("extractor"),
                "page_count": len(pages),
                "char_count": char_count,
                "text_reused": bool(result.get("reused")),
                "classification_reused": bool(result.get("reused")),
                "categories": (
                    classify_document_text(classification_text)
                    if result.get("status") == "COMPLETED"
                    else []
                ),
                "warnings": [],
                "errors": [error] if error else [],
            }
        )
    return document_results


def _build_document_results_response(document_results: List[Dict[str, Any]]) -> str:
    """根据 document_results 生成逐文件处理回执。"""

    lines = [f"已处理 {len(document_results)} 个文件："]
    for index, result in enumerate(document_results, start=1):
        filename = result.get("filename") or result.get("document_id") or "未知文件"
        if result.get("extraction_status") == "FAILED":
            error = (result.get("errors") or [{}])[0]
            message = error.get("message") if isinstance(error, dict) else "未知错误"
            lines.append(f"{index}. {filename}：解析失败，原因：{message}。")
            continue

        categories = result.get("categories") or []
        lines.append(
            f"{index}. {filename}：解析成功，提取 {result.get('page_count', 0)} 页/Sheet，"
            f"共 {result.get('char_count', 0)} 个字符；分类建议：{_format_category_receipt(categories)}。"
        )
    return "\n".join(lines)


def _format_category_receipt(categories: List[Dict[str, Any]]) -> str:
    """把多个分类建议格式化为带置信度和证据的回执片段。"""

    if not categories:
        return "其他（暂无明确关键词依据）"
    formatted_items: list[str] = []
    for category in categories[:5]:
        evidence = category.get("evidence") or []
        name = category.get("name") or "其他"
        if name == "其他" and not evidence:
            formatted_items.append("其他（暂无明确关键词依据）")
            continue
        evidence_text = f"依据：{'、'.join(evidence)}" if evidence else "暂无明确关键词依据"
        confidence = float(category.get("confidence", 0))
        formatted_items.append(f"{name}，置信度 {confidence:.2f}，{evidence_text}")
    return "；".join(formatted_items)
