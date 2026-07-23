"""普通用户任务回执投影。

AgentRun、Skill、ToolInvocation 和原始 Tool 输出继续作为内部审计事实保存；本模块只把用户完成任务
所需的文件结果、文本回复、计划 ID 和安全业务结果投影到普通消息接口。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.agent.state import AgentRunResult


class UserTaskReceipt(BaseModel):
    """普通用户可以消费的稳定任务结果，不包含 Skill 或 Tool 内部载荷。"""

    task_id: str
    task_status: Literal[
        "processing",
        "waiting_confirmation",
        "completed",
        "needs_attention",
        "failed",
    ]
    response_type: Literal[
        "text",
        "file_results",
        "managed_file_list",
        "rename_plan",
        "operation_plan",
        "async_job",
        "file_search_results",
    ] = "text"
    final_response: str | None = None
    processed_count: int = 0
    document_results: list[dict[str, Any]] = Field(default_factory=list)
    managed_file_result: dict[str, Any] | None = None
    rename_plan_result: dict[str, Any] | None = None
    file_search_result: dict[str, Any] | None = None
    pending_job_ids: list[str] = Field(default_factory=list)
    operation_plan_id: str | None = None
    pending_decisions: list[dict[str, Any]] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    suggested_next_actions: list[str] = Field(default_factory=list)


def build_user_task_receipt(result: AgentRunResult) -> UserTaskReceipt:
    """从完整 AgentRun 审计结果生成普通用户投影。

    投影只读取已完成的内部结构，不让前端根据 Tool 名称解释任意 Tool 输出；新增 Tool 时如果没有
    明确的安全投影，默认只展示最终文本，不会自动泄漏内部字段。
    """

    managed_file_result = _managed_file_result(result)
    rename_plan_result = _rename_plan_result(result)
    file_search_result = _file_search_result(result)
    initial_organization_results = _initial_organization_results(result)
    document_results = _merge_document_results(
        initial_organization_results,
        [_safe_document_result(item) for item in result.document_results],
    )
    response_type = _response_type(
        result=result,
        managed_file_result=managed_file_result,
        rename_plan_result=rename_plan_result,
        file_search_result=file_search_result,
    )
    pending_decisions: list[dict[str, Any]] = []
    if result.operation_plan_id:
        pending_decisions.append(
            {
                "type": "operation_plan",
                "operation_plan_id": result.operation_plan_id,
                "message": "此文件操作需要确认后才会执行。",
            }
        )
    if rename_plan_result:
        pending_decisions.extend(_rename_pending_decisions(rename_plan_result))
    for item in document_results:
        pending = item.get("pending_decision")
        if isinstance(pending, dict) and pending not in pending_decisions:
            pending_decisions.append(pending)
    task_status = _task_status(result.status)
    if pending_decisions and task_status == "completed":
        task_status = "needs_attention"
    return UserTaskReceipt(
        task_id=result.agent_run_id,
        task_status=task_status,
        response_type=response_type,
        final_response=result.final_response,
        processed_count=len(document_results),
        document_results=document_results,
        managed_file_result=managed_file_result,
        rename_plan_result=rename_plan_result,
        file_search_result=file_search_result,
        pending_job_ids=list(result.async_job_ids),
        operation_plan_id=result.operation_plan_id,
        pending_decisions=pending_decisions,
        suggested_next_actions=_suggested_next_actions(result=result, response_type=response_type),
    )


def _task_status(status: str) -> str:
    """把内部状态机枚举转换成普通用户可理解的少量任务状态。"""

    if status == "WAITING_FOR_CONFIRMATION":
        return "waiting_confirmation"
    if status == "COMPLETED":
        return "completed"
    if status == "FAILED":
        return "failed"
    if status == "NEEDS_REVIEW":
        return "needs_attention"
    return "processing"


def _safe_document_result(value: dict[str, Any]) -> dict[str, Any]:
    """只保留逐文件回执需要的字段，移除解析器、路径、哈希和内部运行标识。"""

    allowed = {
        "document_id",
        "document_version_id",
        "working_copy_id",
        "filename",
        "organization_status",
        "search_status",
        "evidence_count",
        "extraction_status",
        "page_count",
        "char_count",
        "text_reused",
        "classification_reused",
        "categories",
        "year",
        "rename_suggestion",
        "document_type",
        "keywords",
        "entities",
        "managed_original_unchanged",
        "risk_warnings",
        "pending_decision",
        "warnings",
        "errors",
    }
    projected = {key: value.get(key) for key in allowed if key in value}
    if "categories" in projected:
        projected["categories"] = [
            _safe_category(item)
            for item in projected.get("categories") or []
            if isinstance(item, dict)
        ]
    if "errors" in projected:
        projected["errors"] = [
            {key: item.get(key) for key in ("code", "message") if key in item}
            if isinstance(item, dict)
            else str(item)
            for item in projected.get("errors") or []
        ]
    return projected


def _safe_category(value: dict[str, Any]) -> dict[str, Any]:
    """保留分类含义、置信度和可定位证据，移除分类器版本与内部候选分数。"""

    allowed = {
        "name",
        # 分类建议 ID 是用户接受、拒绝或纠正建议时需要的稳定业务标识，不属于内部 Tool 载荷。
        "suggestion_id",
        "category_id",
        "category_path",
        "confidence",
        "status",
        "evidence",
        "evidence_items",
    }
    return {key: value.get(key) for key in allowed if key in value}


def _initial_organization_results(result: AgentRunResult) -> list[dict[str, Any]]:
    """投影首次自动整理结果，绝不返回工作副本路径、原始目录或内部处理字段。"""

    projected: list[dict[str, Any]] = []
    for invocation in result.tool_invocations:
        output = invocation.output_json
        if invocation.tool_name != "working-copy-initial-organize" or output.get("working_copy_id") is None:
            continue
        projected.append(_safe_document_result(output))
    return projected


def _merge_document_results(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按工作副本或 Document ID 合并逐文件结果，避免生命周期回执重复展示。"""

    merged: list[dict[str, Any]] = []
    positions: dict[str, int] = {}
    for group in groups:
        for item in group:
            key = str(item.get("working_copy_id") or item.get("document_id") or "")
            if key and key in positions:
                index = positions[key]
                merged[index] = {**merged[index], **item}
                continue
            if key:
                positions[key] = len(merged)
            merged.append(item)
    return merged


def _managed_file_result(result: AgentRunResult) -> dict[str, Any] | None:
    """投影受管文件查询结果；宿主机绝对路径和内部扫描字段不会进入响应。"""

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if invocation.tool_name != "managed-file-list" or output.get("ok") is not True:
            continue
        query = output.get("query") if isinstance(output.get("query"), dict) else {}
        files = []
        for item in output.get("files", []):
            if not isinstance(item, dict):
                continue
            files.append(
                {
                    key: item.get(key)
                    for key in (
                        "managed_file_id",
                        "root_key",
                        "relative_path",
                        "filename",
                        "extension",
                        "size_bytes",
                        "status",
                    )
                    if key in item
                }
            )
        return {
            "root_key": str(query.get("root_key") or (files[0].get("root_key") if files else "") or "受管目录"),
            "files": files,
        }
    return None


def _rename_plan_result(result: AgentRunResult) -> dict[str, Any] | None:
    """投影重命名建议，只保留用户识别文件和处理待确认项所需的字段。"""

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if invocation.tool_name != "generate-rename-suggestions" or output.get("kind") != "rename_plan":
            continue
        suggestions = []
        for item in output.get("suggestions", []):
            if not isinstance(item, dict):
                continue
            suggestions.append(
                {
                    key: item.get(key)
                    for key in (
                        "document_id",
                        "working_copy_id",
                        "filename",
                        "proposed_filename",
                        "status",
                        "warnings",
                        "errors",
                    )
                    if key in item
                }
            )
        return {
            "ok": bool(output.get("ok")),
            "status": output.get("status"),
            "matched_count": int(output.get("matched_count") or 0),
            "ready_count": int(output.get("ready_count") or 0),
            "needs_review_count": int(output.get("needs_review_count") or 0),
            "rename_batch_id": output.get("rename_batch_id"),
            "suggestions_truncated": bool(output.get("suggestions_truncated")),
            "suggestions": suggestions,
        }
    return None


def _rename_pending_decisions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把低置信度命名建议转换为用户待决策项，不暴露 Tool 或内部路径。"""

    return [
        {
            "type": "rename_review",
            "document_id": item.get("document_id"),
            "working_copy_id": item.get("working_copy_id"),
            "filename": item.get("filename"),
            "message": "文件名证据不足，请通过对话补充名称。",
        }
        for item in payload.get("suggestions", [])
        if isinstance(item, dict) and item.get("status") == "NEEDS_REVIEW"
    ]


def _response_type(
    *,
    result: AgentRunResult,
    managed_file_result: dict[str, Any] | None,
    rename_plan_result: dict[str, Any] | None,
    file_search_result: dict[str, Any] | None,
) -> str:
    """把内部意图收敛为少量稳定的用户展示类型。"""

    if result.operation_plan_id:
        return "operation_plan"
    if rename_plan_result:
        return "rename_plan"
    if file_search_result:
        return "file_search_results"
    if managed_file_result:
        return "managed_file_list"
    if result.async_job_ids or result.status == "WAITING_FOR_ASYNC_JOB":
        return "async_job"
    if result.document_results or _initial_organization_results(result):
        return "file_results"
    return "text"


def _file_search_result(result: AgentRunResult) -> dict[str, Any] | None:
    """投影两阶段文件搜索结果。

    只有当 hybrid-search tool 输出包含 total_returned 字段（表示走两阶段链路）时
    才激活此投影；旧链路保持 final_response 文本格式不变。
    """

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if invocation.tool_name != "hybrid-search":
            continue
        if not isinstance(output, dict):
            continue
        # 新链路会包含 total_returned 字段
        if "total_returned" not in output:
            continue
        files = []
        for item in output.get("results", []):
            if not isinstance(item, dict):
                continue
            files.append(
                {
                    key: item.get(key)
                    for key in (
                        "working_copy_id",
                        "document_id",
                        "document_version_id",
                        "filename",
                        "category_path",
                        "year",
                        "overview",
                        "match_reasons",
                        "match_location",
                        "evidence_preview",
                    )
                    if key in item
                }
            )
        return {
            "query": str(output.get("query") or ""),
            "total_returned": int(output.get("total_returned") or 0),
            "partial": bool(output.get("partial", False)),
            "user_message": str(output.get("user_message") or ""),
            "files": files,
        }
    return None


def _suggested_next_actions(*, result: AgentRunResult, response_type: str) -> list[str]:
    """提供用户可以直接继续输入的自然语言动作。"""

    if result.operation_plan_id:
        return ["查看计划并确认是否执行"]
    if response_type == "file_results":
        return ["继续查找相关文件", "询问文件中的具体内容"]
    if response_type == "file_search_results":
        return ["继续查找相关文件", "查看文件的详细内容"]
    if response_type == "managed_file_list":
        return ["继续按主题、年份或文件类型筛选"]
    return []
