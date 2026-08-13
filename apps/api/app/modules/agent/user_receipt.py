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
        "trash_restore_selection",
        "file_search_clarification",
        "evidence_answer",
        "file_selection",
        "classification_clarification",
        "classification_decision",
        "filename_conflict",
    ] = "text"
    display_mode: Literal["default", "classification_cards"] = "default"
    final_response: str | None = None
    processed_count: int = 0
    document_results: list[dict[str, Any]] = Field(default_factory=list)
    managed_file_result: dict[str, Any] | None = None
    rename_plan_result: dict[str, Any] | None = None
    file_search_result: dict[str, Any] | None = None
    search_context: dict[str, Any] | None = None
    trash_restore_result: dict[str, Any] | None = None
    file_search_clarification_result: dict[str, Any] | None = None
    evidence_answer_result: dict[str, Any] | None = None
    file_selection_result: dict[str, Any] | None = None
    classification_clarification_result: dict[str, Any] | None = None
    classification_decision_result: dict[str, Any] | None = None
    filename_conflict_result: dict[str, Any] | None = None
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
    search_context = _safe_search_context(result.search_context)
    trash_restore_result = _trash_restore_result(result)
    file_search_clarification_result = _file_search_clarification_result(result)
    evidence_answer_result = _evidence_answer_result(result)
    file_selection_result = _file_selection_result(result)
    (
        classification_clarification_result,
        classification_decision_result,
    ) = _classification_decision_results(result)
    filename_conflict_result = _filename_conflict_result(result)
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
        trash_restore_result=trash_restore_result,
        file_search_clarification_result=file_search_clarification_result,
        evidence_answer_result=evidence_answer_result,
        file_selection_result=file_selection_result,
        classification_clarification_result=classification_clarification_result,
        classification_decision_result=classification_decision_result,
        filename_conflict_result=filename_conflict_result,
    )
    pending_decisions: list[dict[str, Any]] = []
    if result.operation_plan_id and not _has_executed_working_copy_result(result):
        pending_decisions.append(
            {
                "type": "operation_plan",
                "operation_plan_id": result.operation_plan_id,
                "message": "此文件操作需要确认后才会执行。",
            }
        )
    if rename_plan_result:
        pending_decisions.extend(_rename_pending_decisions(rename_plan_result))
    if trash_restore_result:
        pending_decisions.append(
            {
                "type": "trash_restore_selection",
                "message": "已找到同名已删除文件，请明确选择一个文件后再恢复。",
            }
        )
    if file_search_clarification_result:
        is_result_limit_confirmation = (
            file_search_clarification_result.get("selection_type")
            == "RESULT_LIMIT_CONFIRMATION"
        )
        pending_decisions.append(
            {
                "type": "file_search_clarification",
                "clarification_id": file_search_clarification_result.get("id"),
                "message": (
                    "查询结果较多，请确认是否全部展示。"
                    if is_result_limit_confirmation
                    else "请选择本次文件查找范围。"
                ),
            }
        )
    if file_selection_result:
        pending_decisions.append(
            {
                "type": "file_selection",
                "message": "请先选择一个具体文件后继续。",
            }
        )
    if classification_clarification_result:
        pending_decisions.append(
            {
                "type": "classification_clarification",
                "clarification_id": classification_clarification_result.get("id"),
                "message": "请选择要确认或纠正的具体文件分类。",
            }
        )
    if filename_conflict_result:
        pending_decisions.append(
            {
                "type": "filename_conflict",
                "filename": filename_conflict_result.get("filename"),
                "message": filename_conflict_result.get("message"),
                "allowed_decisions": filename_conflict_result.get(
                    "allowed_decisions", []
                ),
            }
        )
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
        display_mode=(
            "classification_cards"
            if result.intent
            in {
                "CLASSIFY_FILES",
                "CLASSIFY_MANAGED_FILES",
                "CLASSIFY_AND_SUGGEST_RENAME",
            }
            else "default"
        ),
        final_response=result.final_response,
        processed_count=len(document_results),
        document_results=document_results,
        managed_file_result=managed_file_result,
        rename_plan_result=rename_plan_result,
        file_search_result=file_search_result,
        search_context=search_context,
        trash_restore_result=trash_restore_result,
        file_search_clarification_result=file_search_clarification_result,
        evidence_answer_result=evidence_answer_result,
        file_selection_result=file_selection_result,
        classification_clarification_result=classification_clarification_result,
        classification_decision_result=classification_decision_result,
        filename_conflict_result=filename_conflict_result,
        # 检索就绪任务属于内部依赖链，前端按同一 AgentRun 状态轮询即可；
        # 普通消息接口不能暴露其任务 ID、队列或“待准备”阶段。
        pending_job_ids=(
            []
            if _has_internal_search_readiness_job(result)
            else list(result.async_job_ids)
        ),
        operation_plan_id=result.operation_plan_id,
        pending_decisions=pending_decisions,
        suggested_next_actions=_suggested_next_actions(result=result, response_type=response_type),
    )


def _safe_search_context(value: dict[str, Any]) -> dict[str, Any] | None:
    """投影实际查询条件和轮次摘要，不向普通用户暴露文件 ID 或内部 Tool。"""

    if not isinstance(value, dict):
        return None
    conditions = [
        {
            key: item.get(key)
            for key in (
                "label",
                "value",
                "condition_type",
                "status",
                "source",
            )
            if key in item
        }
        for item in list(value.get("effective_conditions") or [])
        if isinstance(item, dict)
    ]
    attempts = [
        {
            "query": str(item.get("query") or ""),
            "result_count": int(item.get("result_count") or 0),
            "result_status": str(item.get("result_status") or ""),
            "index_status": str(item.get("index_status") or ""),
        }
        for item in list(value.get("attempts") or [])
        if isinstance(item, dict)
    ]
    if not conditions and not attempts:
        return None
    return {
        "effective_conditions": conditions,
        "attempts": attempts,
    }


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


def _has_internal_search_readiness_job(result: AgentRunResult) -> bool:
    """判断本次等待是否仅由检索就绪协调产生。"""

    return any(
        isinstance(invocation.output_json, dict)
        and invocation.output_json.get("kind") == "filesystem_job"
        and invocation.output_json.get("source") == "search-readiness"
        for invocation in result.tool_invocations
    )


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
            "source_kind": output.get("source_kind"),
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
    trash_restore_result: dict[str, Any] | None,
    file_search_clarification_result: dict[str, Any] | None,
    evidence_answer_result: dict[str, Any] | None,
    file_selection_result: dict[str, Any] | None,
    classification_clarification_result: dict[str, Any] | None,
    classification_decision_result: dict[str, Any] | None,
    filename_conflict_result: dict[str, Any] | None,
) -> str:
    """把内部意图收敛为少量稳定的用户展示类型。"""

    if result.operation_plan_id and not _has_executed_working_copy_result(result):
        return "operation_plan"
    if classification_clarification_result:
        return "classification_clarification"
    if classification_decision_result:
        return "classification_decision"
    if filename_conflict_result:
        return "filename_conflict"
    if rename_plan_result:
        return "rename_plan"
    if trash_restore_result:
        return "trash_restore_selection"
    if file_search_clarification_result:
        return "file_search_clarification"
    if file_selection_result:
        return "file_selection"
    if evidence_answer_result:
        return "evidence_answer"
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

    # 多轮规划会产生多次 hybrid-search，用户回执只展示最新一次检索结果。
    for invocation in reversed(result.tool_invocations):
        output = invocation.output_json
        if invocation.tool_name != "hybrid-search":
            continue
        if not isinstance(output, dict):
            continue
        # 新链路会包含 total_returned 字段
        if "total_returned" not in output:
            continue
        if isinstance(output.get("trash_restore_selection"), dict):
            return None
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
                        "relevance_tier",
                    )
                    if key in item
                }
            )
        payload = {
            "query": str(output.get("query") or ""),
            "total_returned": int(output.get("total_returned") or 0),
            "partial": bool(output.get("partial", False)),
            "user_message": str(output.get("user_message") or ""),
            "show_all_results": bool(output.get("show_all_results", False)),
            "files": files,
        }
        # 只有新版分级检索显式返回数量时才写入回执，确保旧结果仍按原有
        # “相关文件”口径展示，不能把所有历史结果误显示为零个已验证文件。
        if output.get("supported_count") is not None:
            payload["supported_count"] = int(output["supported_count"])
        if output.get("possible_count") is not None:
            payload["possible_count"] = int(output["possible_count"])
        if isinstance(output.get("search_completeness"), dict):
            # 完整性字段来自后端只读评估，不含路径、任务或文件 ID，前端无需自行计算。
            payload["search_completeness"] = output["search_completeness"]
        return payload
    return None


def _classification_decision_results(
    result: AgentRunResult,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """投影分类决定和选择卡，隐藏正式关系、工作副本与建议内部 ID。"""

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if invocation.tool_name != "classification-decision" or not isinstance(
            output, dict
        ):
            continue
        clarification = output.get("classification_clarification")
        if output.get("kind") == "classification_clarification" and isinstance(
            clarification, dict
        ):
            options = [
                {
                    "id": str(item.get("id") or ""),
                    "filename": str(item.get("filename") or ""),
                    "category_label": str(item.get("category_label") or ""),
                }
                for item in clarification.get("options", [])
                if isinstance(item, dict) and item.get("id")
            ]
            return (
                {
                    "id": str(clarification.get("id") or ""),
                    "status": str(
                        clarification.get("status") or "WAITING_SELECTION"
                    ),
                    "prompt": str(
                        clarification.get("prompt")
                        or "请选择要确认或纠正的具体文件分类。"
                    ),
                    "action": str(clarification.get("action") or ""),
                    "options": options,
                    "expires_at": clarification.get("expires_at"),
                },
                None,
            )
        if output.get("kind") == "classification_decision" and output.get("ok"):
            return (
                None,
                {
                    "action": str(output.get("action") or ""),
                    "message": str(
                        output.get("message")
                        or "分类决定已保存，文件位置未改变。"
                    ),
                    "file_position_changed": False,
                },
            )
    return None, None


def _filename_conflict_result(
    result: AgentRunResult,
) -> dict[str, Any] | None:
    """投影共享目标同名冲突，只保留文件名和用户可选动作。"""

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if (
            isinstance(output, dict)
            and output.get("kind") == "filename_conflict"
        ):
            return {
                "filename": str(output.get("filename") or ""),
                "message": str(
                    output.get("message")
                    or "目标目录存在同名文件，请选择处理方式。"
                ),
                "allowed_decisions": [
                    str(item)
                    for item in output.get("allowed_decisions", [])
                    if str(item)
                ],
            }
    return None


def _has_executed_working_copy_result(result: AgentRunResult) -> bool:
    """识别已在本轮完成的冲突覆盖，避免把审计计划再次展示成待确认卡。"""

    return any(
        isinstance(invocation.output_json, dict)
        and invocation.output_json.get("kind") == "working_copy_operation_result"
        and invocation.output_json.get("status") == "EXECUTED"
        for invocation in result.tool_invocations
    )


def _trash_restore_result(result: AgentRunResult) -> dict[str, Any] | None:
    """投影回收站候选，不暴露工作副本、版本或内容哈希。

    除文件检索外，表格分析、正文解析和文件预览也可能明确命中一个已删除的
    document_id；这些入口必须复用同一张恢复选择卡，不能把历史正文直接显示出来。
    """

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if not isinstance(output, dict):
            continue
        selection = (
            output
            if output.get("kind") == "trash_restore_selection"
            else output.get("trash_restore_selection")
        )
        if not isinstance(selection, dict):
            continue
        candidates = []
        for position, item in enumerate(selection.get("candidates", []), start=1):
            if not isinstance(item, dict):
                continue
            candidates.append(
                {
                    "trash_entry_id": item.get("trash_entry_id"),
                    "display_index": position,
                    "filename": item.get("filename"),
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "version_number": int(item.get("version_number") or 0),
                    "deleted_at": item.get("deleted_at"),
                    "created_at": item.get("created_at"),
                }
            )
        if not candidates:
            continue
        return {
            "conversation_id": result.conversation_id,
            "query_type": "EXACT_FILENAME",
            "requires_selection": True,
            "message": str(selection.get("message") or "找到了已删除文件，请选择是否恢复。"),
            "candidates": candidates,
        }
    return None


def _evidence_answer_result(result: AgentRunResult) -> dict[str, Any] | None:
    """投影阶段五回答、文件框和已引用的受限原文依据。

    Tool 输出即使已经过服务层校验，本层仍只允许有限长度的原文片段和可读定位通过，
    防止后续 Tool 改动把 Evidence ID、Chunk ID、路径或完整正文意外暴露到聊天接口。
    """

    # Planner 在检索后可能继续读取文件，以最后一次证据回答为准。
    for invocation in reversed(result.tool_invocations):
        output = invocation.output_json
        if invocation.tool_name != "evidence-answer" or output.get("kind") != "evidence_answer":
            continue
        references = []
        seen_documents: set[str] = set()
        for item in output.get("references", []):
            if not isinstance(item, dict):
                continue
            document_id = str(item.get("document_id") or "")
            if not document_id or document_id in seen_documents:
                continue
            seen_documents.add(document_id)
            projected = {
                key: item.get(key)
                for key in (
                    "document_id",
                    "document_version_id",
                    "working_copy_id",
                    "filename",
                    "category_labels",
                    "availability",
                    "availability_message",
                    "can_open",
                    "can_restore",
                    "reference_indexes",
                )
                if key in item
            }
            projected["evidence_items"] = _safe_evidence_answer_items(
                item.get("evidence_items")
            )
            references.append(projected)
        return {
            "answer_id": output.get("answer_id"),
            "status": str(output.get("status") or ""),
            "answer": str(output.get("answer") or ""),
            "limitations": [
                str(value) for value in output.get("limitations", []) if str(value)
            ],
            "files": references,
            "cached": bool(output.get("cached", False)),
        }
    return None


def _safe_evidence_answer_items(value: Any) -> list[dict[str, Any]]:
    """过滤证据回答的原文依据，保留定位但不允许内部标识进入普通回执。"""

    items: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        quote = " ".join(str(item.get("quote") or "").split()).strip()
        if not quote:
            continue
        # 与 Evidence Answer Service 的限制保持一致，形成服务层与回执层双重保护。
        if len(quote) > 320:
            quote = f"{quote[:320].rstrip()}…"
        page_number = item.get("page_number")
        items.append(
            {
                "quote": quote,
                "page_number": page_number if isinstance(page_number, int) and page_number > 0 else None,
                "sheet_name": str(item.get("sheet_name") or "")[:255] or None,
                "cell_range": str(item.get("cell_range") or "")[:80] or None,
            }
        )
    return items


def _file_selection_result(result: AgentRunResult) -> dict[str, Any] | None:
    """投影证据回答或重命名候选选择卡，内部哈希不会进入普通接口。"""

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if (
            invocation.tool_name
            not in {"evidence-answer", "resolve-rename-reviews"}
            or output.get("kind") != "file_selection"
        ):
            continue
        return {
            "clarification_id": output.get("clarification_id"),
            "message": str(output.get("message") or "请选择一个文件。"),
            "choices": [
                {
                    key: item.get(key)
                    for key in (
                        "document_id",
                        "document_version_id",
                        "working_copy_id",
                        "filename",
                        "size_bytes",
                        "created_at",
                        "suggested_category_labels",
                        "directory_path",
                        "option_id",
                    )
                    if key in item
                }
                for item in output.get("choices", [])
                if isinstance(item, dict)
            ],
        }
    return None


def _file_search_clarification_result(
    result: AgentRunResult,
) -> dict[str, Any] | None:
    """投影检索范围选择卡，执行短语与内部模式仍只保存在后端。"""

    for invocation in result.tool_invocations:
        output = invocation.output_json
        if invocation.tool_name != "hybrid-search" or not isinstance(output, dict):
            continue
        value = output.get("search_clarification")
        if not isinstance(value, dict) or not value.get("id"):
            continue
        return {
            "id": str(value.get("id")),
            "status": str(value.get("status") or "WAITING_SELECTION"),
            "prompt": str(value.get("prompt") or "请选择本次需要查找的范围。"),
            "core_phrase": str(value.get("core_phrase") or ""),
            "options": [
                {
                    "id": str(item.get("id") or ""),
                    "label": str(item.get("label") or ""),
                    "description": str(item.get("description") or ""),
                    "examples": [
                        str(example)
                        for example in item.get("examples", [])
                        if str(example)
                    ][:8],
                    "estimated_count": item.get("estimated_count"),
                }
                for item in value.get("options", [])
                if isinstance(item, dict) and item.get("id")
            ],
            "allow_custom_phrase": bool(value.get("allow_custom_phrase", False)),
            "selection_type": str(
                value.get("selection_type") or "SEARCH_PHRASE"
            ),
            "allow_multiple": bool(value.get("allow_multiple", False)),
            "expires_at": value.get("expires_at"),
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
