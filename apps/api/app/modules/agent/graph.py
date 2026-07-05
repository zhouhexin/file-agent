"""MVP File Agent Runtime 的 LangGraph 状态图。

当前图保持最小实现，但已经拆分 intake、planning、Tool dispatch、证据/变更处理和响应生成等边界。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime

from app.core.logging import log_context, log_event
from app.modules.agent.planner import build_plan_from_user_intent
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentGraphState, ToolInvocationRecord
from app.modules.llm.client import LLMResponseError


def build_agent_graph():
    """编译 MVP LangGraph 工作流。"""

    graph = StateGraph(AgentGraphState, context_schema=AgentRuntimeContext)
    graph.add_node("chat_intake", _logged_node("chat_intake", chat_intake))
    graph.add_node("collect_context", _logged_runtime_node("collect_context", collect_context))
    graph.add_node("planning", _logged_runtime_node("planning", planning))
    graph.add_node("tool_dispatch", _logged_runtime_node("tool_dispatch", tool_dispatch))
    graph.add_node("async_job_wait", _logged_node("async_job_wait", async_job_wait))
    graph.add_node("evidence_or_change", _logged_runtime_node("evidence_or_change", evidence_or_change))
    graph.add_node("response", _logged_runtime_node("response", response))

    graph.set_entry_point("chat_intake")
    graph.add_edge("chat_intake", "collect_context")
    graph.add_edge("collect_context", "planning")
    graph.add_edge("planning", "tool_dispatch")
    graph.add_edge("tool_dispatch", "async_job_wait")
    graph.add_edge("async_job_wait", "evidence_or_change")
    graph.add_edge("evidence_or_change", "response")
    graph.add_edge("response", END)
    return graph.compile()


def _logged_node(name: str, handler):
    """为 LangGraph 节点增加进入、退出、耗时日志。"""

    def wrapped(state: AgentGraphState):
        """执行节点并记录结构化日志。"""

        return _run_logged_node(name=name, state=state, callback=lambda: handler(state))

    return wrapped


def _logged_runtime_node(name: str, handler):
    """为需要 Runtime 注入的 LangGraph 节点增加日志，同时保留显式签名。"""

    def wrapped(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]):
        """执行带 Runtime 的节点并记录结构化日志。"""

        return _run_logged_node(name=name, state=state, callback=lambda: handler(state, runtime))

    return wrapped


def _run_logged_node(name: str, state: AgentGraphState, callback):
    """执行节点回调并记录统一的节点日志。"""

    start = time.perf_counter()
    with log_context(
        agent_run_id=state.get("agent_run_id"),
        user_id=state.get("user_id"),
        conversation_id=state.get("conversation_id"),
    ):
        log_event(
            "agent.node.entered",
            status=state.get("status"),
            message="Agent 节点开始",
            node=name,
        )
        try:
            result = callback()
        except Exception as exc:
            log_event(
                "agent.node.failed",
                level="ERROR",
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message=str(exc),
                node=name,
            )
            raise
        log_event(
            "agent.node.completed",
            status=result.get("status", state.get("status")) if isinstance(result, dict) else state.get("status"),
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="Agent 节点完成",
            node=name,
        )
        return result


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
        try:
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
        except LLMResponseError as exc:
            # LLM 意图理解失败时回退确定性 Planner，保证消息入口可用；错误原因只进入审计快照，不交给 Tool 执行。
            log_event(
                "llm.intent.fallback",
                level="WARNING",
                status="FAILED",
                error_code=exc.__class__.__name__,
                message=str(exc),
            )
            plan = runtime.context.planner.plan(
                conversation_id=state["conversation_id"],
                user_id=state["user_id"],
                message_id=state["message_id"],
                message=state["message"],
                attachments=state.get("attachments", []),
            )
            user_intent_plan = {
                "fallback_reason": "LLM_INTENT_FAILED",
                "error_code": exc.__class__.__name__,
                "message": str(exc),
            }
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

    document_results = _document_results_from_extraction_results(
        tool_results=state.get("tool_results", []),
        context_documents=state.get("context_documents", []),
        classification_service=runtime.context.classification_service,
    )
    return {
        "changeset_id": state.get("changeset_id"),
        "operation_plan_id": state.get("operation_plan_id"),
        "document_results": document_results,
    }

def _spreadsheet_analysis_from_results(
    tool_results: list[dict],
) -> dict | None:
    for result in reversed(tool_results):
        if result.get("kind") == "spreadsheet_analysis":
            return result
    return None

def response(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """生成面向用户的最终运行摘要。"""


    document_results = state.get("document_results", [])
    if document_results:
        requested_outputs = set(state.get("slots", {}).get("requested_outputs", []))
        is_summary_intent = "SUMMAR" in str(state.get("intent") or "").upper()
        is_answer_intent = "ANSWER" in str(state.get("intent") or "").upper()
        if "summary" in requested_outputs or "answer" in requested_outputs or is_summary_intent or is_answer_intent:
            llm_summary = runtime.context.document_summary_service.summarize_documents(
                document_results=document_results,
                tool_results=state.get("tool_results", []),
                user_message=state.get("message", ""),
            )
            return {
                "status": "COMPLETED",
                "final_response": llm_summary
                or _build_document_summary_response(
                    document_results=document_results,
                    tool_results=state.get("tool_results", []),
                ),
            }
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
    classification_documents = _classification_documents_from_results(state.get("tool_results", []))
    if classification_documents:
        return {
            "status": "COMPLETED",
            "final_response": _build_classification_summary_response(classification_documents),
        }

    if insight_documents:
        filenames = [
            item.get("filename") or item.get("document_id")
            for item in insight_documents
        ]
        return {
            "status": "COMPLETED",
            "final_response": f"已读取 {len(insight_documents)} 个文件的基础洞察：{', '.join(filenames)}。",
        }

    capability_catalog = _capability_catalog_from_results(state.get("tool_results", []))
    if capability_catalog:
        return {
            "status": "COMPLETED",
            "final_response": _build_capability_help_response(capability_catalog),
        }

    taxonomy_catalog = _classification_taxonomy_from_results(state.get("tool_results", []))
    if taxonomy_catalog:
        return {
            "status": "COMPLETED",
            "final_response": _build_classification_taxonomy_response(taxonomy_catalog),
        }

    intent_summary = _intent_summary_from_results(state.get("tool_results", []))
    if intent_summary:
        return {
            "status": "COMPLETED",
            "final_response": _build_general_chat_response(intent_summary),
        }

    analysis = _spreadsheet_analysis_from_results(
        state.get("tool_results", [])
    )

    if analysis is not None:
        return {
            "status": "COMPLETED",
            "final_response": format_spreadsheet_analysis_response(analysis),
        }

    return {
        "status": "COMPLETED",
        "final_response": "本次任务已执行完成，但暂未生成可展示的业务结果。请补充要读取、汇总或处理的文件范围。",
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


def _classification_documents_from_results(tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 Tool 结果中提取历史分类建议文件列表。"""

    for result in tool_results:
        result_documents = result.get("documents")
        if result.get("ok") and isinstance(result_documents, list):
            if any(isinstance(item, dict) and "categories" in item for item in result_documents):
                return [item for item in result_documents if isinstance(item, dict)]
    return []


def _build_classification_summary_response(documents: List[Dict[str, Any]]) -> str:
    """把历史分类建议汇总为用户可读文本。"""

    blocks = [f"已汇总 {len(documents)} 个文件的分类建议："]
    for index, document in enumerate(documents, start=1):
        filename = document.get("filename") or document.get("document_id") or "未知文件"
        categories = [item for item in document.get("categories", []) if isinstance(item, dict)]
        if not categories:
            blocks.append(f"{index}. {filename}\n暂无分类建议。")
            continue
        category_lines = []
        for category in categories[:5]:
            confidence = float(category.get("confidence") or 0)
            status = category.get("status") or "SUGGESTED"
            # category_lines.append(f"- {category.get('name') or '其他'}，置信度 {confidence:.2f}，状态 {status}")
            category_lines.append(f"- {category.get('name') or '其他'} ")
        blocks.append(f"{index}. {filename}\n" + "\n".join(category_lines))
    return "\n\n".join(blocks)


def _capability_catalog_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取固定能力清单。"""

    for result in tool_results:
        if result.get("ok") and isinstance(result.get("capabilities"), list):
            return result
    return {}


def _build_capability_help_response(catalog: Dict[str, Any]) -> str:
    """把固定能力清单格式化成用户可读回答。"""

    capabilities = [
        item
        for item in catalog.get("capabilities", [])
        if isinstance(item, dict)
    ]
    if not capabilities:
        return "我可以围绕文件上传、读取、总结、分类和高风险操作计划提供帮助。"
    lines = ["我可以帮你完成这些文件工作："]
    for index, capability in enumerate(capabilities, start=1):
        name = capability.get("name") or capability.get("id") or "未命名能力"
        description = capability.get("description") or ""
        lines.append(f"{index}. {name}：{description}")
    examples = [
        example
        for capability in capabilities
        for example in capability.get("examples", [])[:1]
        if isinstance(example, str)
    ][:3]
    if examples:
        lines.append("\n你可以直接这样说：")
        lines.extend(f"- {example}" for example in examples)
    return "\n".join(lines)


def _classification_taxonomy_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取系统固定分类目录。"""

    for result in tool_results:
        if result.get("ok") and isinstance(result.get("taxonomy"), dict):
            return result["taxonomy"]
    return {}


def _build_classification_taxonomy_response(taxonomy: Dict[str, Any]) -> str:
    """把系统固定分类目录格式化为用户可读文本。"""

    name = taxonomy.get("name") or "文件分类目录"
    version = taxonomy.get("version") or "unknown"
    lines = [f"当前系统支持的文件分类目录：{name}（版本：{version}）"]
    for category in taxonomy.get("categories", []):
        if not isinstance(category, dict):
            continue
        lines.append(f"- {category.get('name') or '未命名分类'}")
        for child in category.get("children", []) or []:
            if isinstance(child, dict):
                lines.append(f"  - {child.get('name') or '未命名子类'}")
    return "\n".join(lines)


def _intent_summary_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取普通对话意图摘要。"""

    for result in tool_results:
        if result.get("ok") and result.get("intent"):
            return result
    return {}


def _build_general_chat_response(intent_summary: Dict[str, Any]) -> str:
    """为普通对话生成自然回复，避免泄露内部 Tool 审计信息。"""

    user_goal = str(intent_summary.get("user_goal") or "").strip()
    if user_goal in {"你好", "您好", "hello", "hi", "Hello", "Hi"}:
        return "你好，我在。请告诉我你想聊什么。"
    return "我已收到。请继续说明你的需求。"


def _document_results_from_extraction_results(
    *,
    tool_results: List[Dict[str, Any]],
    context_documents: List[Dict[str, Any]],
    classification_service,
) -> List[Dict[str, Any]]:
    """把正文解析 Tool 输出聚合成逐文件业务结果。"""

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
        error = result.get("error") if isinstance(result.get("error"), dict) else None
        categories = (
            classification_service.classify(
                document_id=document_id,
                extraction_run_id=str(result.get("extraction_run_id") or ""),
                filename=str(document_context.get("filename") or ""),
                fallback_text=text_preview,
            ).get("categories", [])
            if result.get("status") == "COMPLETED"
            else []
        )
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
                "categories": categories,
                "warnings": [],
                "errors": [error] if error else [],
            }
        )
    return document_results


def _build_document_results_response(document_results: List[Dict[str, Any]]) -> str:
    """根据 document_results 生成逐文件处理回执。"""

    blocks = [f"已处理 {len(document_results)} 个文件："]
    for index, result in enumerate(document_results, start=1):
        filename = result.get("filename") or result.get("document_id") or "未知文件"
        if result.get("extraction_status") == "FAILED":
            error = (result.get("errors") or [{}])[0]
            message = error.get("message") if isinstance(error, dict) else "未知错误"
            blocks.append(
                f"{index}. {filename}\n"
                "解析结果：失败\n"
                f"失败原因：{message}"
            )
            continue

        categories = result.get("categories") or []
        blocks.append(
            f"{index}. {filename}\n"
            f"解析结果：成功，提取 {result.get('page_count', 0)} 页/Sheet，共 {result.get('char_count', 0)} 个字符\n"
            "分类建议：\n"
            f"{_format_category_receipt(categories)}"
        )
    return "\n\n".join(blocks)


def _build_document_summary_response(*, document_results: List[Dict[str, Any]], tool_results: List[Dict[str, Any]]) -> str:
    """根据解析到的正文预览生成内容总结回执。"""

    preview_by_document_id: Dict[str, str] = {}
    for result in _extraction_results_from_results(tool_results):
        document_id = str(result.get("document_id") or "")
        pages = [page for page in result.get("pages", []) if isinstance(page, dict)]
        preview_text = "\n".join(str(page.get("text_preview") or "").strip() for page in pages).strip()
        preview_by_document_id[document_id] = preview_text

    blocks = [f"已读取 {len(document_results)} 个文件，以下是内容总结："]
    for index, result in enumerate(document_results, start=1):
        filename = result.get("filename") or result.get("document_id") or "未知文件"
        if result.get("extraction_status") == "FAILED":
            error = (result.get("errors") or [{}])[0]
            message = error.get("message") if isinstance(error, dict) else "未知错误"
            blocks.append(f"{index}. {filename}\n无法总结：{message}")
            continue

        preview_text = preview_by_document_id.get(str(result.get("document_id") or ""), "")
        if not preview_text:
            blocks.append(f"{index}. {filename}\n暂未提取到可总结的正文内容。")
            continue

        clipped_preview = preview_text[:280]
        suffix = "..." if len(preview_text) > len(clipped_preview) else ""
        blocks.append(
            f"{index}. {filename}\n"
            f"内容概览：{clipped_preview}{suffix}"
        )
    return "\n\n".join(blocks)


def _format_category_receipt(categories: List[Dict[str, Any]]) -> str:
    """把多个分类建议格式化为带置信度和证据的回执片段。"""

    if not categories:
        return "- 其他（暂无明确关键词依据）"
    formatted_items: list[str] = []
    visible_categories = categories[:3]
    for category in visible_categories:
        evidence = category.get("evidence") or []
        evidence_items = [item for item in category.get("evidence_items", []) if isinstance(item, dict)]
        name = category.get("name") or "其他"
        if name == "其他" and not evidence:
            formatted_items.append("- 其他（暂无明确关键词依据）")
            continue
        evidence_text = _format_evidence_item(evidence_items[0]) if evidence_items else ""
        if not evidence_text:
            evidence_text = "、".join(str(item) for item in evidence[:3]) or "暂无明确关键词依据"
        confidence = float(category.get("confidence", 0))
        formatted_items.append(
            f"- {name}\n"
            f"  置信度：{confidence:.2f}\n"
            f"  依据：{evidence_text}"
        )
    hidden_count = len(categories) - len(visible_categories)
    if hidden_count > 0:
        formatted_items.append(f"另有 {hidden_count} 个低置信度候选未展示。")
    return "\n".join(formatted_items)


def _format_evidence_item(evidence_item: Dict[str, Any]) -> str:
    """把结构化证据格式化为用户可读的页码/Sheet + 原文片段。"""

    quote = str(evidence_item.get("quote") or "")
    if not quote:
        return ""
    page_number = evidence_item.get("page_number")
    sheet_name = evidence_item.get("sheet_name")
    if sheet_name:
        return f"Sheet {sheet_name}：“{quote}”"
    if page_number:
        return f"第 {page_number} 页：“{quote}”"
    return f"“{quote}”"
