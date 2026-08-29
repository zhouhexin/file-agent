"""统一文件任务回执编排。

本模块位于 Agent 业务事实与普通用户响应之间，只把已经验证、已经完成安全投影的结果组织成
统一展示结构。它不读取文件正文、不调用 Tool、不写数据库，也不允许 LLM 补充数量、路径或状态。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.modules.agent.state import AgentRunResult


FileTaskKind = Literal[
    "INGEST",
    "READ",
    "SUMMARIZE",
    "ANSWER",
    "CLASSIFY",
    "SEARCH",
    "LIST",
    "SPREADSHEET",
    "RENAME_SUGGESTION",
    "OPERATION_PLAN",
    "FILE_OPERATION",
    "CLARIFICATION",
    "FAILURE",
]


class FileTaskPhase(BaseModel):
    """用户可理解的文件任务阶段，不暴露 Agent、Skill、Tool 或队列名称。"""

    code: Literal[
        "RECEIVED",
        "UNDERSTANDING",
        "PROCESSING",
        "ORGANIZING",
        "WAITING_CONFIRMATION",
        "COMPLETED",
        "NEEDS_ATTENTION",
        "FAILED",
    ]
    label: str


class FileTaskCondition(BaseModel):
    """后端已经确认并允许向用户展示的一条任务条件。"""

    label: str
    value: str
    condition_type: str = ""
    status: str = "APPLIED"


class FileTaskRequestPresentation(BaseModel):
    """文件任务的对象、业务范围、动作和安全查询条件。"""

    target_label: str
    scope_label: str
    action_label: str
    conditions: list[FileTaskCondition] = Field(default_factory=list)


class FileTaskOutcomePresentation(BaseModel):
    """经过确定性计算的任务结果统计。"""

    headline: str
    total_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    needs_review_count: int = 0
    skipped_count: int = 0
    completeness: Literal["COMPLETE", "PROCESSING", "PARTIAL", "UNVERIFIABLE"]


class FileChangeImpactPresentation(BaseModel):
    """分别说明受管原件、工作副本和派生件是否变化。"""

    originals_changed: bool | None = None
    working_copies_changed: bool | None = None
    derivatives_created: int = 0
    operation_executed: bool = False
    message: str


class FileTaskNotice(BaseModel):
    """不包含内部异常细节的用户提示。"""

    level: Literal["INFO", "WARNING", "ERROR"] = "INFO"
    message: str


class FileTaskNextAction(BaseModel):
    """用户可继续选择的安全动作；普通建议不会自动发送或执行文件变更。"""

    id: str
    label: str
    action_kind: Literal[
        "FILL_PROMPT",
        "OPEN_FILE",
        "RESOLVE_CLARIFICATION",
        "CONFIRM_OPERATION",
        "LOAD_MORE",
    ]
    prompt: str | None = None
    target_ref: str | None = None
    requires_confirmation: bool = False


class FileTaskPresentation(BaseModel):
    """所有文件任务共享的稳定展示外壳，只包含经过验证的业务事实。"""

    schema_version: Literal["file-task-receipt.v1"] = "file-task-receipt.v1"
    task_kind: FileTaskKind
    title: str
    phase: FileTaskPhase
    request: FileTaskRequestPresentation
    outcome: FileTaskOutcomePresentation
    change_impact: FileChangeImpactPresentation
    notices: list[FileTaskNotice] = Field(default_factory=list)
    next_actions: list[FileTaskNextAction] = Field(default_factory=list)


_PHASES: dict[str, tuple[str, str]] = {
    "RECEIVED": ("RECEIVED", "已接收文件任务"),
    "PLANNING": ("UNDERSTANDING", "正在确认处理对象和任务条件"),
    "RUNNING_TOOL": ("PROCESSING", "正在读取或处理文件内容"),
    "WAITING_FOR_ASYNC_JOB": (
        "PROCESSING",
        "文件仍在处理中，完成后会继续整理结果",
    ),
    "SUMMARIZING": ("ORGANIZING", "正在整理逐文件结果和依据"),
    "WAITING_FOR_CONFIRMATION": (
        "WAITING_CONFIRMATION",
        "计划尚未执行，正在等待确认",
    ),
    "COMPLETED": ("COMPLETED", "处理完成"),
    "NEEDS_REVIEW": ("NEEDS_ATTENTION", "已完成可执行部分，仍有事项需要确认"),
    "FAILED": ("FAILED", "处理未完成"),
}

_TITLES: dict[FileTaskKind, str] = {
    "INGEST": "文件接收结果",
    "READ": "文件读取结果",
    "SUMMARIZE": "文件总结结果",
    "ANSWER": "文件内容回答",
    "CLASSIFY": "文件分类结果",
    "SEARCH": "文件查找结果",
    "LIST": "目录文件结果",
    "SPREADSHEET": "表格分析结果",
    "RENAME_SUGGESTION": "文件命名建议",
    "OPERATION_PLAN": "文件操作计划",
    "FILE_OPERATION": "文件操作结果",
    "CLARIFICATION": "文件处理需要确认",
    "FAILURE": "文件任务未完成",
}

_ACTION_LABELS: dict[FileTaskKind, str] = {
    "INGEST": "归档或接收文件",
    "READ": "读取文件",
    "SUMMARIZE": "总结文件内容",
    "ANSWER": "根据文件原文回答",
    "CLASSIFY": "分类文件",
    "SEARCH": "查找相关文件",
    "LIST": "列出目录文件",
    "SPREADSHEET": "分析表格",
    "RENAME_SUGGESTION": "生成文件命名建议",
    "FILE_OPERATION": "执行已确认的文件操作",
    "CLARIFICATION": "确认文件处理方式",
}


def compose_file_task_presentation(
    result: AgentRunResult,
    *,
    task_status: str,
    response_type: str,
    document_results: list[dict[str, Any]],
    managed_file_result: dict[str, Any] | None,
    file_search_result: dict[str, Any] | None,
    search_context: dict[str, Any] | None,
    evidence_answer_result: dict[str, Any] | None,
) -> FileTaskPresentation | None:
    """从已验证的 AgentRun 和安全投影构造公共文件回执。

    阶段一和阶段二只覆盖搜索、目录列举、读取、摘要、证据回答和表格分析。其他文件任务继续使用
    现有专用回执，直到后续阶段显式接入，避免提前改变分类或文件操作语义。
    """

    task_kind = _resolve_stage_two_task_kind(
        result=result,
        response_type=response_type,
        document_results=document_results,
        managed_file_result=managed_file_result,
        file_search_result=file_search_result,
        evidence_answer_result=evidence_answer_result,
    )
    if task_kind is None:
        return None

    phase = _build_phase(result.status, task_status=task_status)
    request = _build_request(
        result=result,
        task_kind=task_kind,
        document_results=document_results,
        managed_file_result=managed_file_result,
        file_search_result=file_search_result,
        search_context=search_context,
        evidence_answer_result=evidence_answer_result,
    )
    outcome = _build_outcome(
        result=result,
        task_kind=task_kind,
        document_results=document_results,
        managed_file_result=managed_file_result,
        file_search_result=file_search_result,
        evidence_answer_result=evidence_answer_result,
    )
    return FileTaskPresentation(
        task_kind=task_kind,
        title=_presentation_title(result=result, task_kind=task_kind),
        phase=phase,
        request=request,
        outcome=outcome,
        change_impact=_build_change_impact(result=result, task_kind=task_kind),
        notices=_build_notices(
            file_search_result=file_search_result,
            outcome=outcome,
        ),
        # 任务尚在处理时不提供可点击建议，避免用户基于未完成结果继续操作。
        next_actions=(
            _build_next_actions(task_kind)
            if phase.code in {"COMPLETED", "NEEDS_ATTENTION"}
            else []
        ),
    )


def _resolve_stage_two_task_kind(
    *,
    result: AgentRunResult,
    response_type: str,
    document_results: list[dict[str, Any]],
    managed_file_result: dict[str, Any] | None,
    file_search_result: dict[str, Any] | None,
    evidence_answer_result: dict[str, Any] | None,
) -> FileTaskKind | None:
    """识别明确任务类型；逐文件结果本身不能被解释成“读取”意图。"""

    intent = str(result.intent or "").upper()
    if intent == "SYSTEM_FILE_LIFECYCLE":
        # 生命周期消息由后端审计事件和结构化结果确定类型。这里兼容既有历史记录，
        # 不读取消息文案，也不让前端从 Tool 名称猜测业务含义。
        return _system_lifecycle_task_kind(
            result=result,
            document_results=document_results,
        )
    # 分类、命名和文件变更将在阶段三、四接入；即使这些任务也产生 document_results，
    # 当前也必须保持旧回执，避免被误标为只读文件读取。
    if any(
        marker in intent
        for marker in ("CLASSIF", "RENAME", "OPERATION", "TRASH", "RESTORE")
    ):
        return None
    if _has_spreadsheet_result(result) or "SPREADSHEET" in intent:
        return "SPREADSHEET"
    if file_search_result is not None or response_type == "file_search_results":
        return "SEARCH"
    if managed_file_result is not None or response_type == "managed_file_list":
        return "LIST"
    if evidence_answer_result is not None or (
        "ANSWER" in intent
        and response_type
        not in {"file_selection", "file_search_clarification", "classification_clarification"}
    ):
        return "ANSWER"
    if "SUMMAR" in intent:
        return "SUMMARIZE"
    if any(
        marker in intent
        for marker in ("READ_", "READ_DOCUMENT", "EXTRACT_DOCUMENT")
    ):
        return "READ"
    return None


def _system_lifecycle_task_kind(
    *,
    result: AgentRunResult,
    document_results: list[dict[str, Any]],
) -> FileTaskKind | None:
    """把系统生命周期审计事件映射为用户业务类型，并兼容历史 AgentRun。"""

    tool_names = _result_tool_names(result)

    if "document-background-analysis" in tool_names:
        # 后台分析可能同时给出分类和命名建议；存在明确命名建议时，用户当前需要
        # 处理的是命名选择，因此优先展示为命名建议，而不是泛化的分类结果。
        if any(_has_rename_suggestion(item) for item in document_results):
            return "RENAME_SUGGESTION"
        if not document_results and any(
            str(invocation.output_json.get("organization_decision") or "").upper()
            == "NEEDS_REVIEW"
            for invocation in result.tool_invocations
            if invocation.tool_name == "document-background-analysis"
            and isinstance(invocation.output_json, dict)
        ):
            return "CLARIFICATION"
        return "CLASSIFY"
    if "managed-source-auto-classification" in tool_names:
        return "CLASSIFY"
    if tool_names.intersection(
        {
            "upload-archive",
            "working-copy-fast-import",
        }
    ):
        return "INGEST"
    if "basic-file-risk-check" in tool_names:
        return "CLARIFICATION"
    if "confirmed-file-action" in tool_names:
        return "FILE_OPERATION"
    # 未声明映射的后台事件保持原有文本/专用回执，绝不能仅因携带逐文件结果
    # 就降级成“文件读取结果”。
    return None


def _result_tool_names(result: AgentRunResult) -> set[str]:
    """读取后端已经审计的生命周期工具名，不向普通用户直接暴露该字段。"""

    tool_names = {
        invocation.tool_name
        for invocation in result.tool_invocations
        if invocation.tool_name
    }
    planned_tool = (
        str(result.tool_plan.get("tool_name") or "")
        if isinstance(result.tool_plan, dict)
        else ""
    )
    if planned_tool:
        tool_names.add(planned_tool)
    return tool_names


def _has_rename_suggestion(item: dict[str, Any]) -> bool:
    """只根据生命周期结构化结果识别命名建议，不解析自然语言消息。"""

    if isinstance(item.get("rename_suggestion"), dict):
        return True
    pending = item.get("pending_decision")
    return isinstance(pending, dict) and str(pending.get("type") or "") == "rename_suggestion"


def _presentation_title(*, result: AgentRunResult, task_kind: FileTaskKind) -> str:
    """为归档生命周期给出准确标题，其他任务沿用稳定公共标题。"""

    if task_kind == "INGEST" and "upload-archive" in _result_tool_names(result):
        return "文件归档结果"
    return _TITLES[task_kind]


def _build_phase(status: str, *, task_status: str) -> FileTaskPhase:
    """将最终任务状态和 Agent 细粒度状态映射为一致的业务阶段。"""

    # UserTaskReceipt 可能因待决策项把已完成 AgentRun 调整为“需要处理”；公共外壳必须
    # 使用同一最终状态，不能一边显示 completed，一边又要求用户确认。
    task_status_phase = {
        "waiting_confirmation": "WAITING_FOR_CONFIRMATION",
        "needs_attention": "NEEDS_REVIEW",
        "failed": "FAILED",
    }.get(task_status)
    effective_status = task_status_phase or str(status or "")

    code, label = _PHASES.get(
        effective_status,
        ("PROCESSING", "正在处理文件任务"),
    )
    return FileTaskPhase(code=code, label=label)


def _build_request(
    *,
    result: AgentRunResult,
    task_kind: FileTaskKind,
    document_results: list[dict[str, Any]],
    managed_file_result: dict[str, Any] | None,
    file_search_result: dict[str, Any] | None,
    search_context: dict[str, Any] | None,
    evidence_answer_result: dict[str, Any] | None,
) -> FileTaskRequestPresentation:
    """构造用户可核对的任务理解，不把内部 workspace 当作业务范围展示。"""

    conditions = _safe_conditions(search_context)
    scope_label = _scope_from_conditions(conditions)
    if not scope_label and file_search_result:
        completeness = file_search_result.get("search_completeness")
        if isinstance(completeness, dict):
            scope_label = _business_scope_label(completeness.get("scope_label"))
    if not scope_label and managed_file_result:
        scope_label = _managed_root_label(managed_file_result)
    if not scope_label:
        scope_label = "本次指定文件"

    target_count = _target_count(
        result=result,
        document_results=document_results,
        managed_file_result=managed_file_result,
        file_search_result=file_search_result,
        evidence_answer_result=evidence_answer_result,
    )
    target_label = (
        "相关文件"
        if task_kind == "SEARCH"
        else _managed_root_label(managed_file_result)
        if task_kind == "LIST" and managed_file_result
        else f"{target_count} 个文件"
        if target_count > 0
        else "指定文件"
    )
    return FileTaskRequestPresentation(
        target_label=target_label,
        scope_label=scope_label,
        action_label=_ACTION_LABELS[task_kind],
        conditions=conditions,
    )


def _build_outcome(
    *,
    result: AgentRunResult,
    task_kind: FileTaskKind,
    document_results: list[dict[str, Any]],
    managed_file_result: dict[str, Any] | None,
    file_search_result: dict[str, Any] | None,
    evidence_answer_result: dict[str, Any] | None,
) -> FileTaskOutcomePresentation:
    """按专用安全 payload 确定性计算结果数量和完整性。"""

    if task_kind == "SEARCH":
        payload = file_search_result or {}
        total = len(payload.get("files") or [])
        supported = int(payload.get("supported_count") or 0)
        possible = int(payload.get("possible_count") or 0)
        if payload.get("supported_count") is None and payload.get("possible_count") is None:
            supported = total
        completeness = _search_completeness(payload)
        headline = (
            f"找到 {supported} 个明确相关文件"
            + (f"，另有 {possible} 个可能相关文件" if possible else "")
            if total
            else "没有找到符合当前条件的文件"
        )
        return FileTaskOutcomePresentation(
            headline=headline,
            total_count=total,
            completed_count=supported,
            needs_review_count=possible,
            completeness=completeness,
        )

    if task_kind == "LIST":
        total = len((managed_file_result or {}).get("files") or [])
        return FileTaskOutcomePresentation(
            headline=f"目录中共有 {total} 个文件",
            total_count=total,
            completed_count=total,
            completeness=_status_completeness(result.status),
        )

    if task_kind == "ANSWER":
        payload = evidence_answer_result or {}
        total = len(payload.get("files") or [])
        answer_status = str(payload.get("status") or "").upper()
        has_supported_answer = answer_status in {"ANSWERED", "PARTIAL"} and total > 0
        return FileTaskOutcomePresentation(
            headline=(
                f"已根据 {total} 个文件中的可定位依据生成回答"
                if has_supported_answer
                else "当前没有找到足够的文件依据"
            ),
            total_count=total,
            # 引用文件不是逐文件处理队列，不能因回答状态为 PARTIAL 就凭空减去一个完成项
            # 或制造一个待复核文件；回答完整性由独立 completeness 字段表达。
            completed_count=total,
            needs_review_count=0,
            completeness=_evidence_answer_completeness(
                answer_status=answer_status,
                agent_status=result.status,
            ),
        )

    if task_kind == "SPREADSHEET":
        completed, failed = _spreadsheet_result_counts(result)
        total = completed + failed or _document_scope_count(result)
        if completed == 0 and failed == 0 and result.status != "COMPLETED":
            headline = f"正在分析 {total or 1} 个表格文件"
        elif failed == 0:
            headline = f"已完成 {completed} 个表格分析结果"
        else:
            headline = f"已完成 {completed} 个表格分析结果，{failed} 个未完成"
        return FileTaskOutcomePresentation(
            headline=headline,
            total_count=total,
            completed_count=completed,
            failed_count=failed,
            completeness=("PARTIAL" if failed and completed else _status_completeness(result.status)),
        )

    if task_kind in {
        "INGEST",
        "CLASSIFY",
        "RENAME_SUGGESTION",
        "FILE_OPERATION",
        "CLARIFICATION",
    }:
        return _build_lifecycle_outcome(
            result=result,
            task_kind=task_kind,
            document_results=document_results,
        )

    total = len(document_results) or _document_scope_count(result)
    failed = sum(
        1
        for item in document_results
        if str(item.get("extraction_status") or "").upper() == "FAILED"
    )
    needs_review = sum(
        1
        for item in document_results
        if str(item.get("organization_status") or "").upper() == "NEEDS_REVIEW"
        and str(item.get("extraction_status") or "").upper() != "FAILED"
    )
    completed = (
        max(0, len(document_results) - failed)
        if document_results
        else total
        if result.status == "COMPLETED"
        else 0
    )
    action = "总结" if task_kind == "SUMMARIZE" else "读取"
    if result.status == "FAILED" and not document_results:
        # AgentRun 失败但尚未形成逐文件事实时，只说明任务未完成；不能继续显示“正在处理”，
        # 也不能把任务级失败擅自换算成某个文件失败。
        headline = f"文件{action}任务未完成"
    elif result.status != "COMPLETED" and not document_results:
        headline = f"正在{action} {total or 1} 个文件"
    else:
        headline = (
            f"已{action} {completed} 个文件"
            + (f"，{failed} 个未完成" if failed else "")
            + (f"，{needs_review} 个需要留意" if needs_review else "")
        )
    return FileTaskOutcomePresentation(
        headline=headline,
        total_count=total,
        completed_count=completed,
        failed_count=failed,
        needs_review_count=needs_review,
        completeness=(
            "PARTIAL"
            if failed or needs_review
            else _status_completeness(result.status)
        ),
    )


def _build_lifecycle_outcome(
    *,
    result: AgentRunResult,
    task_kind: FileTaskKind,
    document_results: list[dict[str, Any]],
) -> FileTaskOutcomePresentation:
    """为归档、分类、命名建议和确认事项生成确定性结果统计。"""

    total = (
        len(document_results)
        or _document_scope_count(result)
        # 每条系统生命周期审计只对应一个受控目标；历史记录通常没有 Planner slots，
        # 但不能因此把已知的单文件归档或确认事项显示成 0 个文件。
        or (1 if str(result.intent or "").upper() == "SYSTEM_FILE_LIFECYCLE" else 0)
    )
    failed = sum(
        1
        for item in document_results
        if str(item.get("extraction_status") or "").upper() == "FAILED"
    )
    needs_review = sum(
        1
        for item in document_results
        if (
            str(item.get("organization_status") or "").upper() == "NEEDS_REVIEW"
            or isinstance(item.get("pending_decision"), dict)
        )
        and str(item.get("extraction_status") or "").upper() != "FAILED"
    )
    completed = (
        max(0, len(document_results) - failed)
        if document_results
        else total
        if result.status == "COMPLETED"
        else 0
    )

    if task_kind == "CLARIFICATION":
        headline = "需要确认文件处理方式"
        needs_review = max(needs_review, total or 1)
    elif task_kind == "RENAME_SUGGESTION":
        headline = (
            f"已生成 {completed} 个文件的命名建议"
            if completed
            else "文件命名建议尚未生成"
        )
        if needs_review:
            headline += f"，{needs_review} 个需要确认"
    elif task_kind == "CLASSIFY":
        headline = (
            f"已生成 {completed} 个文件的分类结果"
            if completed
            else "文件分类尚未完成"
        )
        if failed:
            headline += f"，{failed} 个未完成"
        if needs_review:
            headline += f"，{needs_review} 个需要确认"
    elif task_kind == "FILE_OPERATION":
        headline = (
            f"已完成 {completed} 个文件操作"
            if completed or result.status == "COMPLETED"
            else "文件操作尚未完成"
        )
    else:
        headline = (
            f"已完成 {completed} 个文件的归档或接收"
            if completed
            else "文件归档或接收已完成"
            if result.status == "COMPLETED"
            else "文件归档或接收尚未完成"
        )

    return FileTaskOutcomePresentation(
        headline=headline,
        total_count=total,
        completed_count=completed,
        failed_count=failed,
        needs_review_count=needs_review,
        completeness=(
            "PARTIAL"
            if failed or needs_review
            else _status_completeness(result.status)
        ),
    )


def _safe_conditions(search_context: dict[str, Any] | None) -> list[FileTaskCondition]:
    """复用安全查询投影，进一步移除内部来源字段。"""

    if not isinstance(search_context, dict):
        return []
    conditions: list[FileTaskCondition] = []
    for item in search_context.get("effective_conditions") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        value = str(item.get("value") or "").strip()
        if not label or not value:
            continue
        condition_type = str(item.get("condition_type") or "")
        # workspace 术语清洗只适用于范围条件。主题、实体和文种可能合法包含“工作区”，
        # 对它们做全局替换会改变用户原始检索语义。
        public_value = (
            _business_scope_label(value)
            if _is_scope_condition(label=label, condition_type=condition_type)
            else value
        )
        conditions.append(
            FileTaskCondition(
                label=label,
                value=public_value,
                condition_type=condition_type,
                status=str(item.get("status") or "APPLIED"),
            )
        )
    return conditions


def _scope_from_conditions(conditions: list[FileTaskCondition]) -> str:
    """优先使用用户明确表达的范围词，例如学校、学院或指定目录。"""

    for condition in conditions:
        if _is_scope_condition(
            label=condition.label,
            condition_type=condition.condition_type,
        ):
            return condition.value
    return ""


def _business_scope_label(value: Any) -> str:
    """把内部 workspace 术语转换为普通用户可理解的文件范围。"""

    label = str(value or "").strip()
    if not label:
        return ""
    for internal in ("当前共享工作区全部活动文件", "当前 workspace", "当前工作区"):
        label = label.replace(internal, "当前可用文件范围")
    return label.replace("workspace", "文件范围").replace("工作区", "文件范围")


def _managed_root_label(managed_file_result: dict[str, Any]) -> str:
    """优先返回受管目录业务名称，root_key 仅作为兼容回退。"""

    return str(
        managed_file_result.get("root_display_name")
        or managed_file_result.get("root_key")
        or "受管目录"
    )


def _is_scope_condition(*, label: str, condition_type: str) -> bool:
    """只识别明确的范围条件，避免把普通主题词误当作内部 workspace 文案。"""

    return "scope" in condition_type.lower() or "范围" in label


def _target_count(
    *,
    result: AgentRunResult,
    document_results: list[dict[str, Any]],
    managed_file_result: dict[str, Any] | None,
    file_search_result: dict[str, Any] | None,
    evidence_answer_result: dict[str, Any] | None,
) -> int:
    """从安全结果或声明式计划中计算处理对象数量。"""

    if file_search_result is not None:
        return len(file_search_result.get("files") or [])
    if managed_file_result is not None:
        return len(managed_file_result.get("files") or [])
    if evidence_answer_result is not None:
        return len(evidence_answer_result.get("files") or [])
    return len(document_results) or _document_scope_count(result)


def _document_scope_count(result: AgentRunResult) -> int:
    """只读取 Planner 已固化的 document_ids 数量，不返回具体内部 ID。"""

    slots = result.tool_plan.get("slots") if isinstance(result.tool_plan, dict) else {}
    if not isinstance(slots, dict):
        return 0
    document_ids = slots.get("document_ids")
    return len(document_ids) if isinstance(document_ids, list) else 0


def _has_spreadsheet_result(result: AgentRunResult) -> bool:
    """根据白名单表格 Tool 的结构化结果识别只读表格任务。"""

    return any(
        invocation.tool_name
        in {"analyze-spreadsheet", "profile-spreadsheet", "validate-spreadsheet"}
        and isinstance(invocation.output_json, dict)
        for invocation in result.tool_invocations
    )


def _spreadsheet_result_counts(result: AgentRunResult) -> tuple[int, int]:
    """只根据 ToolInvocation 的业务状态统计表格分析成功和失败数量。"""

    completed = 0
    failed = 0
    for invocation in result.tool_invocations:
        if invocation.tool_name not in {
            "analyze-spreadsheet",
            "profile-spreadsheet",
            "validate-spreadsheet",
        }:
            continue
        output = invocation.output_json if isinstance(invocation.output_json, dict) else {}
        if (
            invocation.status == "FAILED"
            or output.get("ok") is False
            or str(output.get("status") or "").upper() == "FAILED"
        ):
            failed += 1
        else:
            completed += 1
    return completed, failed


def _search_completeness(payload: dict[str, Any]) -> Literal[
    "COMPLETE", "PROCESSING", "PARTIAL", "UNVERIFIABLE"
]:
    """沿用后端检索覆盖结论，不能由前端根据展示数量猜测。"""

    completeness = payload.get("search_completeness")
    if isinstance(completeness, dict):
        status = str(completeness.get("status") or "UNVERIFIABLE").upper()
        if status in {"COMPLETE", "PROCESSING", "PARTIAL", "UNVERIFIABLE"}:
            return status  # type: ignore[return-value]
    return "PARTIAL" if payload.get("partial") else "UNVERIFIABLE"


def _status_completeness(status: str) -> Literal[
    "COMPLETE", "PROCESSING", "PARTIAL", "UNVERIFIABLE"
]:
    """将 Agent 状态转换为结果完整性，失败状态不伪装成完整结果。"""

    if status == "COMPLETED":
        return "COMPLETE"
    if status in {"RECEIVED", "PLANNING", "RUNNING_TOOL", "WAITING_FOR_ASYNC_JOB", "SUMMARIZING"}:
        return "PROCESSING"
    if status == "NEEDS_REVIEW":
        return "PARTIAL"
    return "UNVERIFIABLE"


def _evidence_answer_completeness(
    *,
    answer_status: str,
    agent_status: str,
) -> Literal["COMPLETE", "PROCESSING", "PARTIAL", "UNVERIFIABLE"]:
    """用回答级状态表达证据充分性，不伪造逐文件统计。"""

    if answer_status == "ANSWERED":
        return _status_completeness(agent_status)
    if answer_status == "PARTIAL":
        return "PARTIAL"
    if answer_status in {
        "NO_EVIDENCE",
        "NEEDS_CLARIFICATION",
        "NEEDS_CONFIRMATION",
    }:
        return "UNVERIFIABLE"
    return _status_completeness(agent_status)


def _build_notices(
    *,
    file_search_result: dict[str, Any] | None,
    outcome: FileTaskOutcomePresentation,
) -> list[FileTaskNotice]:
    """展示覆盖限制和部分失败，但不泄漏内部故障细节。"""

    notices: list[FileTaskNotice] = []
    if file_search_result:
        completeness = file_search_result.get("search_completeness")
        message = str(completeness.get("message") or "") if isinstance(completeness, dict) else ""
        if message:
            notices.append(
                FileTaskNotice(
                    level="INFO" if outcome.completeness == "COMPLETE" else "WARNING",
                    message=message,
                )
            )
    if outcome.failed_count:
        notices.append(
            FileTaskNotice(
                level="WARNING",
                message=f"有 {outcome.failed_count} 个结果未完成，请查看逐文件状态。",
            )
        )
    return notices


def _build_change_impact(
    *,
    result: AgentRunResult,
    task_kind: FileTaskKind,
) -> FileChangeImpactPresentation:
    """按业务任务说明文件变化，不能把分析或建议伪装成已执行文件操作。"""

    if task_kind == "CLASSIFY":
        return FileChangeImpactPresentation(
            originals_changed=False,
            working_copies_changed=None,
            operation_executed=False,
            message="本次生成了分类结果；受管原件未改变，工作副本状态以逐文件结果为准。",
        )
    if task_kind == "RENAME_SUGGESTION":
        return FileChangeImpactPresentation(
            originals_changed=False,
            working_copies_changed=False,
            operation_executed=False,
            message="本次只生成命名建议，当前尚未改名；原件和工作副本均未改变。",
        )
    if task_kind == "CLARIFICATION":
        return FileChangeImpactPresentation(
            originals_changed=False,
            working_copies_changed=False,
            operation_executed=False,
            message="确认前不会执行文件变更；原件和工作副本均未改变。",
        )
    if task_kind == "INGEST":
        return FileChangeImpactPresentation(
            originals_changed=False,
            working_copies_changed=None,
            operation_executed=False,
            message="上传原件已按文件生命周期规则受到保护，不会因归档、分类或命名被覆盖。",
        )
    if task_kind == "FILE_OPERATION":
        executed = result.status == "COMPLETED"
        return FileChangeImpactPresentation(
            originals_changed=False,
            working_copies_changed=executed,
            operation_executed=executed,
            message=(
                "已执行确认后的工作副本操作；受管原件未改变。"
                if executed
                else "文件操作未完成；受管原件未改变。"
            ),
        )
    return FileChangeImpactPresentation(
        originals_changed=False,
        working_copies_changed=False,
        derivatives_created=0,
        operation_executed=False,
        message=_read_only_change_message(task_kind),
    )


def _read_only_change_message(task_kind: FileTaskKind) -> str:
    """为阶段二只读任务明确原件保护状态。"""

    if task_kind == "SEARCH":
        return "本次只进行了文件查找，原文件和工作副本均未改变。"
    if task_kind == "LIST":
        return "本次只读取了目录索引，原文件和工作副本均未改变。"
    if task_kind == "ANSWER":
        return "本次只读取了文件证据并生成回答，原文件未改变。"
    if task_kind == "SPREADSHEET":
        return "本次只进行了表格读取和确定性分析，原文件未改变。"
    if task_kind == "SUMMARIZE":
        return "本次只读取并总结了文件内容，原文件未改变。"
    return "本次只读取了文件内容，原文件未改变。"


def _build_next_actions(task_kind: FileTaskKind) -> list[FileTaskNextAction]:
    """生成只填入输入框的安全建议，不在普通按钮中执行文件操作。"""

    actions: dict[FileTaskKind, list[tuple[str, str, str]]] = {
        "SEARCH": [
            ("refine-search", "继续筛选", "请按年份、单位或文件类型继续筛选这些结果"),
            ("summarize-search-results", "总结相关文件", "请总结刚才找到的明确相关文件"),
        ],
        "LIST": [
            ("filter-directory", "继续筛选", "请按主题、年份或文件类型筛选这个目录中的文件"),
        ],
        "ANSWER": [
            ("continue-evidence-question", "继续追问", "请继续根据这些文件的原文回答："),
        ],
        "READ": [
            ("summarize-read-files", "总结文件", "请总结刚才读取的文件"),
            ("ask-read-files", "询问具体内容", "请根据刚才读取的文件回答："),
        ],
        "SUMMARIZE": [
            ("ask-summary-files", "继续追问", "请根据刚才总结的文件回答："),
        ],
        "SPREADSHEET": [
            ("continue-spreadsheet-analysis", "继续分析表格", "请继续分析这份表格："),
        ],
        "CLASSIFY": [
            ("review-classification", "核对分类", "请说明需要确认或纠正分类的文件"),
        ],
        "RENAME_SUGGESTION": [
            ("request-rename-plan", "生成改名计划", "请为刚才的命名建议生成待确认的改名计划"),
        ],
        "CLARIFICATION": [
            ("clarify-file-task", "补充处理方式", "我确认的处理方式是："),
        ],
    }
    return [
        FileTaskNextAction(
            id=action_id,
            label=label,
            action_kind="FILL_PROMPT",
            prompt=prompt,
        )
        for action_id, label, prompt in actions.get(task_kind, [])
    ]
