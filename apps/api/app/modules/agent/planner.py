"""MVP Agent Runtime 使用的确定性 Planner 和计划 schema。

后续真实 Planner 可以调用 LLM，但仍必须返回这里定义的声明式结构。
Shell 命令、SQL 写入和文件系统路径会在 Tool dispatch 前被拒绝。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.agent.capability_router import route_user_intent
from app.modules.llm.schemas import UserIntentPlan


FORBIDDEN_INPUT_KEYS = {
    "shell",
    "shell_command",
    "sql",
    "sql_write",
    "path",
    "file_path",
    "absolute_path",
}

TEXT_EXTRACTION_HINTS = {
    "extract_document_text",
    "extract-document-text",
    "read_file_content",
    "read-file-content",
    "parse_document",
    "parse-document",
    "ocr_image",
    "ocr-image",
}
DOCUMENT_INSIGHT_HINTS = {"read_document_insights", "read-document-insights"}
DOCUMENT_CLASSIFICATION_HINTS = {
    "read_document_classifications",
    "read-document-classifications",
}
AGENT_CAPABILITY_HINTS = {
    "read_agent_capabilities",
    "read-agent-capabilities",
    "capability_help",
}
CLASSIFICATION_TAXONOMY_HINTS = {
    "read_classification_taxonomy",
    "read-classification-taxonomy",
}
SPREADSHEET_ANALYSIS_HINTS = {"analyze_spreadsheet", "analyze-spreadsheet"}
SPREADSHEET_PROFILE_HINTS = {"profile_spreadsheet", "profile-spreadsheet"}
SPREADSHEET_VALIDATE_HINTS = {"validate_spreadsheet", "validate-spreadsheet"}
MANAGED_FILE_LIST_HINTS = {"managed_file_list", "managed-file-list"}
MANAGED_FILE_READ_HINTS = {"managed_file_read", "managed-file-read-document", "read_managed_file"}
MANAGED_FILE_RENAME_HINTS = {"suggest_rename", "generate-rename-suggestions", "file_rename"}
MCP_FILESYSTEM_HINTS = {"mcp_filesystem_read", "mcp-filesystem-list", "mcp-filesystem-search", "mcp-filesystem-info"}
SPREADSHEET_SUFFIXES = {".xls", ".xlsx", ".xlsm", ".csv", ".tsv"}
MANAGED_EXTENSION_ALIASES = {
    "pdf": "pdf",
    ".pdf": "pdf",
    "doc": "doc",
    ".doc": "doc",
    "docx": "docx",
    ".docx": "docx",
    "word": "docx",
    "xls": "xls",
    ".xls": "xls",
    "xlsx": "xlsx",
    ".xlsx": "xlsx",
    "xlsm": "xlsm",
    ".xlsm": "xlsm",
    "excel": "xlsx",
    "csv": "csv",
    ".csv": "csv",
    "tsv": "tsv",
    ".tsv": "tsv",
    "txt": "txt",
    ".txt": "txt",
    "md": "md",
    ".md": "md",
    "png": "png",
    ".png": "png",
    "jpg": "jpg",
    ".jpg": "jpg",
    "jpeg": "jpeg",
    ".jpeg": "jpeg",
}


class PlannerStep(BaseModel):
    """Planner 生成的一步声明式 Tool 调用。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    skill: str
    tool_name: str
    input: Dict[str, Any]
    requires_confirmation: bool = False
    risk_level: str = "low"
    expected_outputs: List[str] = Field(default_factory=list)
    writes: List[str] = Field(default_factory=list)

    @field_validator("input")
    @classmethod
    def reject_direct_actions(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """拒绝 Planner 通过输入参数夹带直接执行指令。"""
        forbidden = FORBIDDEN_INPUT_KEYS.intersection(value.keys())
        if forbidden:
            raise ValueError(
                f"Planner step contains forbidden direct action keys: {sorted(forbidden)}"
            )
        return value

    @field_validator("writes")
    @classmethod
    def reject_direct_writes(cls, value: List[str]) -> List[str]:
        """拒绝直接指向 shell、SQL 或文件系统的写入声明。"""
        bad_writes = [
            item for item in value if item.startswith(("filesystem:", "shell:", "sql:"))
        ]
        if bad_writes:
            raise ValueError(f"Planner step contains forbidden direct writes: {bad_writes}")
        return value


class PlannerOutput(BaseModel):
    """供 LangGraph tool-dispatch 节点消费的声明式计划。"""

    model_config = ConfigDict(extra="forbid")

    intent: str
    user_goal: str
    slots: Dict[str, Any] = Field(default_factory=dict)
    selected_skills: List[str]
    steps: List[PlannerStep]
    evidence_policy: Dict[str, Any]
    confirmation_policy: Dict[str, Any]

    @model_validator(mode="after")
    def reject_empty_steps(self) -> "PlannerOutput":
        """要求每次运行至少包含一个 Tool 步骤，确保意图可审计。"""
        if not self.steps:
            raise ValueError("Planner output must contain at least one step")
        return self


class DeterministicPlanner:
    """用于测试和早期框架开发的确定性 Planner。

    它不调用外部 LLM，保证 Agent Runtime 测试输出稳定。
    """

    def __init__(self, force_unsafe_step: bool = False) -> None:
        self.force_unsafe_step = force_unsafe_step

    def plan(
        self,
        conversation_id: str,
        user_id: str,
        message_id: str,
        message: str,
        attachments: List[Dict[str, Any]],
    ) -> PlannerOutput:
        """根据用户消息和附件上下文生成声明式计划。"""
        lowered = message.lower()

        if _has_capability_help_intent(message=message, lowered=lowered):
            return _capability_help_plan(user_goal=message)

        if _has_classification_taxonomy_intent(message=message, lowered=lowered):
            return _classification_taxonomy_plan(user_goal=message)

        if _has_mcp_filesystem_list_intent(message=message, lowered=lowered):
            return _mcp_filesystem_list_plan(
                user_goal=message,
                path_prefix=_mcp_filesystem_path_prefix_from_list_request(message),
            )

        managed_rename_filters = _managed_file_rename_filters_from_request(
            message=message,
            lowered=lowered,
        )
        if managed_rename_filters and not attachments:
            return _managed_file_rename_plan(
                user_goal=message,
                root_key=managed_rename_filters.get("root_key"),
                path_prefix=managed_rename_filters.get("path_prefix"),
                extension=managed_rename_filters.get("extension"),
                filename_contains=managed_rename_filters.get("filename_contains"),
                route_source="deterministic_planner",
            )

        managed_extension = _managed_extension_from_list_request(message)
        managed_filename_contains = _managed_filename_contains_from_list_request(message)
        managed_root_key = _managed_root_key_from_list_request(message)
        managed_path_prefix = _managed_path_prefix_from_list_request(
            message=message,
            root_key=managed_root_key,
        )
        if managed_root_key:
            return _managed_file_list_plan(
                user_goal=message,
                root_key=managed_root_key,
                path_prefix=managed_path_prefix,
                extension=managed_extension,
                filename_contains=managed_filename_contains,
            )
        if _has_managed_file_list_filter_intent(
            message=message,
            path_prefix=managed_path_prefix,
            extension=managed_extension,
            filename_contains=managed_filename_contains,
        ) and not attachments:
            return _managed_file_list_plan(
                user_goal=message,
                root_key=None,
                path_prefix=managed_path_prefix,
                extension=managed_extension,
                filename_contains=managed_filename_contains,
            )

        managed_read_filters = _managed_file_read_filters_from_request(message=message, lowered=lowered)
        if managed_read_filters and not attachments:
            return _managed_file_read_document_plan(
                user_goal=message,
                root_key=managed_read_filters.get("root_key"),
                path_prefix=managed_read_filters.get("path_prefix"),
                extension=managed_read_filters.get("extension"),
                filename_contains=managed_read_filters.get("filename_contains"),
                requested_outputs=_requested_outputs_for_message(message=message, lowered=lowered),
                route_source="deterministic_planner",
            )

        needs_file_scope = (
            _should_extract_text(message=message, lowered=lowered)
            or _has_classification_intent(message=message, lowered=lowered)
            or _has_answer_intent(message=message, lowered=lowered)
            or _has_summary_intent(message=message, lowered=lowered)
            or _has_spreadsheet_profile_intent(message=message, lowered=lowered)
            or _has_spreadsheet_validation_intent(message=message, lowered=lowered)
            or _has_spreadsheet_analysis_intent(
                message=message,
                lowered=lowered,
                attachments=attachments,
            )
        )

        if not attachments and not needs_file_scope:
            return _general_chat_plan(intent="GENERAL_CHAT", user_goal=message)

        document_ids = _document_ids(attachments)

        if needs_file_scope and not document_ids:
            return _missing_file_scope_plan(user_goal=message)

        document_id = document_ids[0] if document_ids else ""

        if self.force_unsafe_step:
            return PlannerOutput(
                intent="UNSAFE_DIRECT_WRITE",
                user_goal=message,
                slots={"document_ids": [document_id]},
                selected_skills=["file-ingest"],
                steps=[
                    {
                        "step_id": "step-unsafe",
                        "skill": "file-ingest",
                        "tool_name": "document-convert",
                        "input": {"document_id": document_id, "path": "/tmp/unsafe"},
                        "requires_confirmation": False,
                        "risk_level": "high",
                        "expected_outputs": ["file"],
                        "writes": ["filesystem:/tmp/unsafe"],
                    }
                ],
                evidence_policy={"require_page_or_cell": True, "allow_no_evidence_answer": False},
                confirmation_policy={"operation_plan_required": True},
            )

        if _has_spreadsheet_validation_intent(message=message, lowered=lowered):
            return _spreadsheet_workbench_plan(
                intent="VALIDATE_SPREADSHEET",
                user_goal=message,
                document_ids=document_ids,
                tool_name="validate-spreadsheet",
                expected_outputs=["spreadsheet_validation"],
                selected_skills=["chat-intake", "spreadsheet-workbench"],
            )

        if _has_spreadsheet_profile_intent(message=message, lowered=lowered):
            return _spreadsheet_workbench_plan(
                intent="PROFILE_SPREADSHEET",
                user_goal=message,
                document_ids=document_ids,
                tool_name="profile-spreadsheet",
                expected_outputs=["spreadsheet_profile"],
                selected_skills=["chat-intake", "spreadsheet-workbench"],
            )

        if _has_spreadsheet_analysis_intent(
            message=message,
            lowered=lowered,
            attachments=attachments,
        ):
            return _spreadsheet_analysis_plan(
                user_goal=message,
                document_ids=document_ids,
                question=message,
                selected_skills=["chat-intake", "spreadsheet-analysis"],
            )

        if _has_plain_document_summary_intent(message=message, lowered=lowered):
            return PlannerOutput(
                intent="SUMMARIZE_DOCUMENTS",
                user_goal=message,
                slots={
                    "document_ids": document_ids,
                    "requested_outputs": ["text", "summary", "receipt"],
                },
                selected_skills=["chat-intake", "document-text-extract", "document-reading"],
                steps=[
                    _extract_document_text_step(
                        document_id=item,
                        index=index,
                        force_reprocess=_should_force_reprocess(
                            message=message,
                            lowered=lowered,
                        ),
                    )
                    for index, item in enumerate(document_ids, start=1)
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )

        if _has_classification_summary_intent(message=message):
            return PlannerOutput(
                intent="SUMMARIZE_CLASSIFICATIONS",
                user_goal=message,
                slots={
                    "document_ids": document_ids,
                    "requested_outputs": ["classification_summary"],
                },
                selected_skills=["chat-intake", "document-classification-read"],
                steps=[
                    {
                        "step_id": "step-1",
                        "skill": "document-classification-read",
                        "tool_name": "read-document-classifications",
                        "input": {"document_ids": document_ids},
                        "requires_confirmation": False,
                        "risk_level": "low",
                        "expected_outputs": ["document_category_suggestions"],
                        "writes": [],
                    }
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )

        if _has_answer_intent(message=message, lowered=lowered):
            return PlannerOutput(
                intent="ANSWER_DOCUMENTS",
                user_goal=message,
                slots={
                    "document_ids": document_ids,
                    "question": message,
                    "requested_outputs": ["text", "answer", "receipt"],
                },
                selected_skills=["chat-intake", "document-text-extract", "document-reading"],
                steps=[
                    _extract_document_text_step(
                        document_id=item,
                        index=index,
                        force_reprocess=_should_force_reprocess(
                            message=message,
                            lowered=lowered,
                        ),
                    )
                    for index, item in enumerate(document_ids, start=1)
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )

        if _should_extract_text(message=message, lowered=lowered):
            requested_outputs = _requested_outputs_for_message(
                message=message,
                lowered=lowered,
            )
            return PlannerOutput(
                intent="SUMMARIZE_DOCUMENTS" if "summary" in requested_outputs else "EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                slots={
                    "document_ids": document_ids,
                    "requested_outputs": requested_outputs,
                },
                selected_skills=[
                    "chat-intake",
                    "document-text-extract",
                    "document-classification",
                    "change-report",
                ],
                steps=[
                    _extract_document_text_step(
                        document_id=item,
                        index=index,
                        force_reprocess=_should_force_reprocess(
                            message=message,
                            lowered=lowered,
                        ),
                    )
                    for index, item in enumerate(document_ids, start=1)
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )

        if _has_classification_intent(message=message, lowered=lowered):
            return _classify_files_plan(
                user_goal=message,
                document_ids=document_ids,
                selected_skills=[
                    "chat-intake",
                    "document-text-extract",
                    "document-classification",
                    "change-report",
                ],
            )

        return _classify_files_plan(
            user_goal=message,
            document_ids=document_ids,
            selected_skills=[
                "chat-intake",
                "document-text-extract",
                "document-classification",
                "change-report",
            ],
        )


def build_plan_from_user_intent(
    *,
    intent_plan: UserIntentPlan,
    message: str,
    attachments: List[Dict[str, Any]],
) -> PlannerOutput:
    """把 LLM 结构化意图转换为受控 PlannerOutput。"""
    lowered = message.lower()
    document_ids = intent_plan.referenced_document_ids or _document_ids(attachments)
    requested_capabilities = set(intent_plan.required_capabilities).union(
        intent_plan.tool_plan_hint
    )
    capability_route = route_user_intent(
        intent=intent_plan.intent,
        required_capabilities=intent_plan.required_capabilities,
        tool_plan_hint=intent_plan.tool_plan_hint,
        target_scope=intent_plan.target_scope,
        attachments=attachments,
    )
    resolved_scope = _resolved_scope_from_attachments(attachments)

    if (
        _has_capability_help_intent(message=message, lowered=lowered)
        or intent_plan.intent == "CAPABILITY_HELP"
        or requested_capabilities.intersection(AGENT_CAPABILITY_HINTS)
    ):
        return _capability_help_plan(user_goal=intent_plan.user_goal or message)

    if (
        _has_classification_taxonomy_intent(message=message, lowered=lowered)
        or intent_plan.intent == "LIST_CLASSIFICATION_TAXONOMY"
        or requested_capabilities.intersection(CLASSIFICATION_TAXONOMY_HINTS)
    ):
        return _classification_taxonomy_plan(user_goal=intent_plan.user_goal or message)

    if (
        _has_mcp_filesystem_list_intent(message=message, lowered=lowered)
        or intent_plan.intent == "LIST_MCP_FILESYSTEM"
        or requested_capabilities.intersection(MCP_FILESYSTEM_HINTS)
        or (capability_route is not None and capability_route.tool_name == "mcp-filesystem-list")
    ):
        return _mcp_filesystem_list_plan(
            user_goal=intent_plan.user_goal or message,
            path_prefix=intent_plan.managed_path_prefix or _mcp_filesystem_path_prefix_from_list_request(message),
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
        )

    managed_rename_filters = _managed_file_rename_filters_from_request(
        message=message,
        lowered=lowered,
    )
    if (
        intent_plan.intent == "SUGGEST_RENAME"
        or requested_capabilities.intersection(MANAGED_FILE_RENAME_HINTS)
        or "generate-rename-suggestions" in intent_plan.tool_plan_hint
        or (managed_rename_filters and not document_ids)
    ):
        return _managed_file_rename_plan(
            user_goal=intent_plan.user_goal or message,
            root_key=intent_plan.managed_root_key or managed_rename_filters.get("root_key"),
            path_prefix=intent_plan.managed_path_prefix or managed_rename_filters.get("path_prefix"),
            extension=intent_plan.managed_extension or managed_rename_filters.get("extension"),
            filename_contains=(
                intent_plan.managed_filename_contains
                or managed_rename_filters.get("filename_contains")
            ),
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="capability_router" if capability_route else "llm_planner",
        )

    managed_read_filters = _managed_file_read_filters_from_request(message=message, lowered=lowered)
    if (
        intent_plan.intent in {"READ_MANAGED_FILE", "SUMMARIZE_MANAGED_FILE", "ANSWER_MANAGED_FILE"}
        or requested_capabilities.intersection(MANAGED_FILE_READ_HINTS)
        or "managed-file-read-document" in intent_plan.tool_plan_hint
        or (managed_read_filters and not document_ids)
    ):
        requested_outputs = _requested_outputs_for_intent(intent=intent_plan.intent, message=message)
        return _managed_file_read_document_plan(
            user_goal=intent_plan.user_goal or message,
            root_key=intent_plan.managed_root_key or managed_read_filters.get("root_key"),
            path_prefix=intent_plan.managed_path_prefix or managed_read_filters.get("path_prefix"),
            extension=intent_plan.managed_extension or managed_read_filters.get("extension"),
            filename_contains=intent_plan.managed_filename_contains or managed_read_filters.get("filename_contains"),
            requested_outputs=requested_outputs,
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="capability_router" if capability_route else "llm_planner",
        )

    managed_root_key = intent_plan.managed_root_key or _managed_root_key_from_list_request(message)
    managed_path_prefix = intent_plan.managed_path_prefix or _managed_path_prefix_from_list_request(
        message=message,
        root_key=managed_root_key,
    )
    managed_extension = intent_plan.managed_extension or _managed_extension_from_list_request(message)
    managed_filename_contains = (
        intent_plan.managed_filename_contains
        or _managed_filename_contains_from_list_request(message)
    )
    if (
        managed_root_key
        or intent_plan.intent in {"LIST_MANAGED_FILES", "SEARCH_MANAGED_FILES"}
        or requested_capabilities.intersection(MANAGED_FILE_LIST_HINTS)
        or (capability_route is not None and capability_route.tool_name == "managed-file-list")
    ):
        return _managed_file_list_plan(
            user_goal=intent_plan.user_goal or message,
            root_key=managed_root_key,
            path_prefix=managed_path_prefix,
            extension=managed_extension,
            filename_contains=managed_filename_contains,
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="capability_router" if capability_route else "legacy_planner",
        )

    if (
        requested_capabilities.intersection(SPREADSHEET_VALIDATE_HINTS)
        or intent_plan.intent == "VALIDATE_SPREADSHEET"
        or (capability_route is not None and capability_route.tool_name == "validate-spreadsheet")
        or _has_spreadsheet_validation_intent(message=message, lowered=lowered)
    ):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return _spreadsheet_workbench_plan(
            intent="VALIDATE_SPREADSHEET",
            user_goal=intent_plan.user_goal or message,
            document_ids=document_ids,
            tool_name="validate-spreadsheet",
            expected_outputs=["spreadsheet_validation"],
            selected_skills=["llm-understanding", "spreadsheet-workbench"],
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="capability_router" if capability_route else "legacy_planner",
            target_scope=intent_plan.target_scope,
            resolved_scope=resolved_scope,
        )

    if (
        requested_capabilities.intersection(SPREADSHEET_PROFILE_HINTS)
        or intent_plan.intent == "PROFILE_SPREADSHEET"
        or (capability_route is not None and capability_route.tool_name == "profile-spreadsheet")
        or _has_spreadsheet_profile_intent(message=message, lowered=lowered)
    ):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return _spreadsheet_workbench_plan(
            intent="PROFILE_SPREADSHEET",
            user_goal=intent_plan.user_goal or message,
            document_ids=document_ids,
            tool_name="profile-spreadsheet",
            expected_outputs=["spreadsheet_profile"],
            selected_skills=["llm-understanding", "spreadsheet-workbench"],
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="capability_router" if capability_route else "legacy_planner",
            target_scope=intent_plan.target_scope,
            resolved_scope=resolved_scope,
        )

    if (
        requested_capabilities.intersection(SPREADSHEET_ANALYSIS_HINTS)
        or _has_spreadsheet_analysis_intent(
            message=message,
            lowered=lowered,
            attachments=attachments,
        )
    ):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return _spreadsheet_analysis_plan(
            user_goal=intent_plan.user_goal or message,
            document_ids=document_ids,
            question=message,
            selected_skills=["llm-understanding", "spreadsheet-analysis"],
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
        )

    # 关键修复：LLM Planner 中用户明确要求“分类/归类/整理”时，必须生成
    # extract-document-text 步骤，不能降级成 intent-summary。
    # “查看/列出/汇总 分类结果”仍由后面的 SUMMARIZE_CLASSIFICATIONS 分支处理。
    if _has_classification_intent(message=message, lowered=lowered) and not _has_classification_summary_intent(
        message=message
    ):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return _classify_files_plan(
            user_goal=intent_plan.user_goal or message,
            document_ids=document_ids,
            selected_skills=[
                "llm-understanding",
                "document-text-extract",
                "document-classification",
                "change-report",
            ],
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="legacy_planner",
            target_scope=intent_plan.target_scope,
            resolved_scope=resolved_scope,
        )

    if _has_plain_document_summary_intent(message=message, lowered=lowered):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return PlannerOutput(
            intent="SUMMARIZE_DOCUMENTS",
            user_goal=intent_plan.user_goal or message,
            slots={
                "document_ids": document_ids,
                "requested_outputs": ["text", "summary", "receipt"],
                "response_style": intent_plan.response_style,
                "clarification_question": intent_plan.clarification_question,
                "llm_intent_plan": intent_plan.model_dump(),
            },
            selected_skills=["llm-understanding", "document-text-extract", "document-reading"],
            steps=[
                _extract_document_text_step(
                    document_id=document_id,
                    index=index,
                    force_reprocess=_should_force_reprocess(
                        message=message,
                        lowered=lowered,
                    ),
                )
                for index, document_id in enumerate(document_ids, start=1)
            ],
            evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
            confirmation_policy={"operation_plan_required": False},
        )

    if (
        _has_classification_summary_intent(message=message)
        or requested_capabilities.intersection(DOCUMENT_CLASSIFICATION_HINTS)
        or (capability_route is not None and capability_route.tool_name == "read-document-classifications")
    ):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return PlannerOutput(
            intent="SUMMARIZE_CLASSIFICATIONS",
            user_goal=intent_plan.user_goal or message,
            slots={
                "document_ids": document_ids,
                "target_scope": intent_plan.target_scope,
                "resolved_scope": resolved_scope,
                "route_source": "capability_router" if capability_route else "legacy_planner",
                "response_style": intent_plan.response_style,
                "clarification_question": intent_plan.clarification_question,
                "llm_intent_plan": intent_plan.model_dump(),
            },
            selected_skills=["llm-understanding", "document-classification-read"],
            steps=[
                {
                    "step_id": "step-1",
                    "skill": "document-classification-read",
                    "tool_name": "read-document-classifications",
                    "input": {"document_ids": document_ids},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["document_category_suggestions"],
                    "writes": [],
                }
            ],
            evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
            confirmation_policy={"operation_plan_required": False},
        )

    if (
        capability_route is not None
        and capability_route.capability_id == "document_classification"
        and capability_route.tool_name == "extract-document-text"
    ):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return _classify_files_plan(
            user_goal=intent_plan.user_goal or message,
            document_ids=document_ids,
            selected_skills=[
                "llm-understanding",
                "document-text-extract",
                "document-classification",
                "change-report",
            ],
            response_style=intent_plan.response_style,
            clarification_question=intent_plan.clarification_question,
            llm_intent_plan=intent_plan.model_dump(),
            route_source="capability_router",
            target_scope=intent_plan.target_scope,
            resolved_scope=resolved_scope,
        )

    if requested_capabilities.intersection(TEXT_EXTRACTION_HINTS):
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        requested_outputs = _requested_outputs_for_intent(
            intent=intent_plan.intent,
            message=message,
        )
        return PlannerOutput(
            intent=intent_plan.intent,
            user_goal=intent_plan.user_goal or message,
            slots={
                "document_ids": document_ids,
                "requested_outputs": requested_outputs,
                "response_style": intent_plan.response_style,
                "clarification_question": intent_plan.clarification_question,
                "llm_intent_plan": intent_plan.model_dump(),
            },
            selected_skills=["llm-understanding", "document-text-extract"],
            steps=[
                _extract_document_text_step(
                    document_id=document_id,
                    index=index,
                    force_reprocess=_should_force_reprocess(
                        message=message,
                        lowered=lowered,
                    ),
                )
                for index, document_id in enumerate(document_ids, start=1)
            ],
            evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
            confirmation_policy={"operation_plan_required": False},
        )

    uses_document_insights = intent_plan.needs_file_context or bool(
        requested_capabilities.intersection(DOCUMENT_INSIGHT_HINTS)
    )
    if uses_document_insights:
        if not document_ids:
            return _missing_file_scope_plan(user_goal=intent_plan.user_goal or message)
        return PlannerOutput(
            intent=intent_plan.intent,
            user_goal=intent_plan.user_goal or message,
            slots={
                "document_ids": document_ids,
                "skip_completed_ingest": intent_plan.skip_completed_ingest,
                "response_style": intent_plan.response_style,
                "clarification_question": intent_plan.clarification_question,
                "llm_intent_plan": intent_plan.model_dump(),
            },
            selected_skills=["llm-understanding", "document-insight-read"],
            steps=[
                {
                    "step_id": "step-1",
                    "skill": "document-insight-read",
                    "tool_name": "read-document-insights",
                    "input": {"document_ids": document_ids},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["document_insights"],
                    "writes": [],
                }
            ],
            evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
            confirmation_policy={"operation_plan_required": False},
        )

    return PlannerOutput(
        intent=intent_plan.intent,
        user_goal=intent_plan.user_goal or message,
        slots={
            "document_ids": document_ids,
            "response_style": intent_plan.response_style,
            "clarification_question": intent_plan.clarification_question,
            "llm_intent_plan": intent_plan.model_dump(),
        },
        selected_skills=["llm-understanding"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "llm-understanding",
                "tool_name": "intent-summary",
                "input": {"intent": intent_plan.intent, "user_goal": intent_plan.user_goal or message},
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["intent"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _general_chat_plan(*, intent: str, user_goal: str) -> PlannerOutput:
    """生成普通对话计划，不触发任何文件处理工具。"""
    return PlannerOutput(
        intent=intent,
        user_goal=user_goal,
        slots={"document_ids": [], "response_style": "concise"},
        selected_skills=["llm-understanding"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "llm-understanding",
                "tool_name": "intent-summary",
                "input": {"intent": intent, "user_goal": user_goal},
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["intent"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _missing_file_scope_plan(*, user_goal: str) -> PlannerOutput:
    """用户请求文件任务但未解析到真实 document_id 时，返回明确提示。"""
    return PlannerOutput(
        intent="MISSING_FILE_SCOPE",
        user_goal=user_goal,
        slots={
            "document_ids": [],
            "requested_outputs": ["missing_file_scope"],
            "response_style": "concise",
        },
        selected_skills=["file-context"],
        steps=[
            {
                "step_id": "step-missing-file-scope",
                "skill": "file-context",
                "tool_name": "intent-summary",
                "input": {"intent": "MISSING_FILE_SCOPE", "user_goal": user_goal},
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["intent"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _capability_help_plan(*, user_goal: str) -> PlannerOutput:
    """生成读取固定能力清单的声明式计划。"""
    return PlannerOutput(
        intent="CAPABILITY_HELP",
        user_goal=user_goal,
        slots={"document_ids": [], "response_style": "concise"},
        selected_skills=["capability-help"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "capability-help",
                "tool_name": "read-agent-capabilities",
                "input": {"detail_level": "brief"},
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["agent_capabilities"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _classification_taxonomy_plan(*, user_goal: str) -> PlannerOutput:
    """生成读取系统固定分类目录的声明式计划。"""
    return PlannerOutput(
        intent="LIST_CLASSIFICATION_TAXONOMY",
        user_goal=user_goal,
        slots={"document_ids": [], "response_style": "concise"},
        selected_skills=["classification-taxonomy-read"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "classification-taxonomy-read",
                "tool_name": "read-classification-taxonomy",
                "input": {"detail_level": "brief", "max_depth": 2},
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["classification_taxonomy"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _managed_file_list_plan(
    *,
    user_goal: str,
    root_key: str | None,
    path_prefix: str | None = None,
    extension: str | None = None,
    filename_contains: str | None = None,
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
    route_source: str = "legacy_planner",
) -> PlannerOutput:
    """生成受管目录文件列表查询计划。"""

    input_json: Dict[str, Any] = {"status": "ACTIVE"}
    if root_key:
        input_json["root_key"] = root_key
    if path_prefix:
        input_json["path_prefix"] = path_prefix
    if extension:
        input_json["extension"] = extension
    if filename_contains:
        input_json["filename_contains"] = filename_contains
    return PlannerOutput(
        intent="LIST_MANAGED_FILES",
        user_goal=user_goal,
        slots={
            "document_ids": [],
            "root_key": root_key,
            "path_prefix": path_prefix,
            "extension": extension,
            "filename_contains": filename_contains,
            "requested_outputs": ["managed_files"],
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
            "route_source": route_source,
        },
        selected_skills=["managed-file-query"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "managed-file-query",
                "tool_name": "managed-file-list",
                "input": input_json,
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["managed_files"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _managed_file_rename_plan(
    *,
    user_goal: str,
    root_key: str | None,
    path_prefix: str | None = None,
    extension: str | None = None,
    filename_contains: str | None = None,
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
    route_source: str = "legacy_planner",
) -> PlannerOutput:
    """生成受管文件重命名建议计划；此阶段不直接修改文件。"""

    input_json: Dict[str, Any] = {}
    if root_key:
        input_json["root_key"] = root_key
    if path_prefix:
        input_json["path_prefix"] = path_prefix
    if extension:
        input_json["extension"] = extension
    if filename_contains:
        input_json["filename_contains"] = filename_contains
    return PlannerOutput(
        intent="SUGGEST_RENAME",
        user_goal=user_goal,
        slots={
            "document_ids": [],
            "root_key": root_key,
            "path_prefix": path_prefix,
            "extension": extension,
            "filename_contains": filename_contains,
            "requested_outputs": ["rename_suggestions", "operation_plan"],
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
            "route_source": route_source,
        },
        selected_skills=["file-rename", "operation-plan"],
        steps=[
            {
                "step_id": "step-rename-suggestions",
                "skill": "file-rename",
                "tool_name": "generate-rename-suggestions",
                "input": input_json,
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["rename_suggestions", "operation_plan"],
                "writes": ["document_pages", "operation_plans"],
            }
        ],
        evidence_policy={"require_page_or_cell": True, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": True},
    )


def _mcp_filesystem_list_plan(
    *,
    user_goal: str,
    path_prefix: str | None = None,
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
) -> PlannerOutput:
    """生成实时 Filesystem MCP 目录列举计划。"""

    input_json: Dict[str, Any] = {"sort_by": "name"}
    if path_prefix:
        input_json["path_prefix"] = path_prefix
    return PlannerOutput(
        intent="LIST_MCP_FILESYSTEM",
        user_goal=user_goal,
        slots={
            "document_ids": [],
            "path_prefix": path_prefix,
            "requested_outputs": ["mcp_filesystem"],
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
            "route_source": "mcp_filesystem",
        },
        selected_skills=["managed-file-query"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "managed-file-query",
                "tool_name": "mcp-filesystem-list",
                "input": input_json,
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["mcp_filesystem"],
                "writes": [],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _managed_file_read_document_plan(
    *,
    user_goal: str,
    root_key: str | None,
    path_prefix: str | None = None,
    extension: str | None = None,
    filename_contains: str | None = None,
    requested_outputs: List[str] | None = None,
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
    route_source: str = "legacy_planner",
) -> PlannerOutput:
    """生成读取并解析唯一受管文件的计划。"""

    outputs = requested_outputs or ["text", "receipt"]
    input_json: Dict[str, Any] = {}
    if root_key:
        input_json["root_key"] = root_key
    if path_prefix:
        input_json["path_prefix"] = path_prefix
    if extension:
        input_json["extension"] = extension
    if filename_contains:
        input_json["filename_contains"] = filename_contains
    return PlannerOutput(
        intent="SUMMARIZE_MANAGED_FILE" if "summary" in outputs else "READ_MANAGED_FILE",
        user_goal=user_goal,
        slots={
            "document_ids": [],
            "root_key": root_key,
            "path_prefix": path_prefix,
            "extension": extension,
            "filename_contains": filename_contains,
            "requested_outputs": outputs,
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
            "route_source": route_source,
        },
        selected_skills=["managed-file-query", "document-text-extract", "document-reading"],
        steps=[
            {
                "step_id": "step-1",
                "skill": "managed-file-query",
                "tool_name": "managed-file-read-document",
                "input": input_json,
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": ["document_pages", "extraction_run"],
                "writes": ["documents", "file_objects", "document_extraction_runs", "document_pages"],
            }
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _classify_files_plan(
    *,
    user_goal: str,
    document_ids: List[str],
    selected_skills: List[str],
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
    route_source: str = "legacy_planner",
    target_scope: str = "unspecified",
    resolved_scope: str = "unspecified",
) -> PlannerOutput:
    """构造真实文件分类计划：分类必须先解析正文，再由 Graph 分类服务生成结果。"""
    return PlannerOutput(
        intent="CLASSIFY_FILES",
        user_goal=user_goal,
        slots={
            "document_ids": document_ids,
            "requested_outputs": ["classification", "receipt"],
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
            "route_source": route_source,
            "target_scope": target_scope,
            "resolved_scope": resolved_scope,
        },
        selected_skills=selected_skills,
        steps=[
            _extract_document_text_step(
                document_id=document_id,
                index=index,
                force_reprocess=False,
            )
            for index, document_id in enumerate(document_ids, start=1)
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _extract_document_text_step(
    *,
    document_id: str,
    index: int,
    force_reprocess: bool = False,
) -> Dict[str, Any]:
    """为一个文件生成正文解析 Tool 步骤，支持多附件批量计划。"""
    return {
        "step_id": f"step-extract-{index}",
        "skill": "document-text-extract",
        "tool_name": "extract-document-text",
        "input": {"document_id": document_id, "force_reprocess": force_reprocess},
        "requires_confirmation": False,
        "risk_level": "low",
        "expected_outputs": ["document_pages", "extraction_run"],
        "writes": ["document_extraction_runs", "document_pages"],
    }


def _analyze_spreadsheet_step(*, document_id: str, question: str, index: int) -> Dict[str, Any]:
    """为一个已上传电子表格生成只读分析 Tool 步骤。"""
    return {
        "step_id": f"step-spreadsheet-{index}",
        "skill": "spreadsheet-analysis",
        "tool_name": "analyze-spreadsheet",
        "input": {"document_id": document_id, "question": question},
        "requires_confirmation": False,
        "risk_level": "low",
        "expected_outputs": ["spreadsheet_analysis"],
        "writes": [],
    }


def _spreadsheet_analysis_plan(
    *,
    user_goal: str,
    document_ids: List[str],
    question: str,
    selected_skills: List[str],
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
) -> PlannerOutput:
    """构造通用电子表格分析计划；业务字段完全由运行时 Profile 决定。"""
    return PlannerOutput(
        intent="ANALYZE_SPREADSHEET",
        user_goal=user_goal,
        slots={
            "document_ids": document_ids,
            "question": question,
            "requested_outputs": ["spreadsheet_analysis"],
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
        },
        selected_skills=selected_skills,
        steps=[
            _analyze_spreadsheet_step(
                document_id=document_id,
                question=question,
                index=index,
            )
            for index, document_id in enumerate(document_ids, start=1)
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": False},
        confirmation_policy={"operation_plan_required": False},
    )


def _spreadsheet_workbench_plan(
    *,
    intent: str,
    user_goal: str,
    document_ids: List[str],
    tool_name: str,
    expected_outputs: List[str],
    selected_skills: List[str],
    response_style: str = "concise",
    clarification_question: str | None = None,
    llm_intent_plan: Dict[str, Any] | None = None,
    route_source: str = "legacy_planner",
    target_scope: str = "unspecified",
    resolved_scope: str = "unspecified",
) -> PlannerOutput:
    """构造表格工作台只读计划；Profile/校验不得修改原件。"""

    return PlannerOutput(
        intent=intent,
        user_goal=user_goal,
        slots={
            "document_ids": document_ids,
            "requested_outputs": expected_outputs,
            "response_style": response_style,
            "clarification_question": clarification_question,
            "llm_intent_plan": llm_intent_plan or {},
            "route_source": route_source,
            "target_scope": target_scope,
            "resolved_scope": resolved_scope,
        },
        selected_skills=selected_skills,
        steps=[
            {
                "step_id": f"step-spreadsheet-workbench-{index}",
                "skill": "spreadsheet-workbench",
                "tool_name": tool_name,
                "input": {"document_id": document_id},
                "requires_confirmation": False,
                "risk_level": "low",
                "expected_outputs": expected_outputs,
                "writes": [],
            }
            for index, document_id in enumerate(document_ids, start=1)
        ],
        evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
        confirmation_policy={"operation_plan_required": False},
    )


def _should_extract_text(*, message: str, lowered: str) -> bool:
    """判断确定性模式下用户是否明确要求读取正文；读取优先于分类组合词。"""
    extraction_keywords = ["读取", "解析", "正文", "内容", "OCR"]
    english_keywords = ["read", "extract", "parse", "ocr"]
    return any(keyword in message for keyword in extraction_keywords) or any(
        keyword in lowered for keyword in english_keywords
    )


def _should_force_reprocess(*, message: str, lowered: str) -> bool:
    """判断用户是否明确要求跳过缓存重新处理。"""
    chinese_keywords = ["重新解析", "重新读取", "重新处理", "重跑", "强制重新"]
    english_keywords = ["reprocess", "rerun", "force reprocess", "parse again"]
    return any(keyword in message for keyword in chinese_keywords) or any(
        keyword in lowered for keyword in english_keywords
    )


def _requested_outputs_for_message(*, message: str, lowered: str) -> List[str]:
    """根据确定性关键词记录用户期望输出，供审计和后续回执策略使用。"""
    outputs = ["text", "receipt"]
    if _has_summary_intent(message=message, lowered=lowered):
        outputs.insert(1, "summary")
    if _has_answer_intent(message=message, lowered=lowered):
        outputs.insert(1, "answer")
    if _has_classification_intent(message=message, lowered=lowered):
        outputs.insert(1, "classification")
    return outputs


def _requested_outputs_for_intent(*, intent: str, message: str) -> List[str]:
    """根据 LLM 意图和原始消息记录用户期望输出。"""
    lowered = message.lower()
    outputs = _requested_outputs_for_message(message=message, lowered=lowered)
    if "SUMMAR" in intent.upper() and "summary" not in outputs:
        outputs.insert(1, "summary")
    if ("ANSWER" in intent.upper() or "QUESTION" in intent.upper()) and "answer" not in outputs:
        outputs.insert(1, "answer")
    return outputs


def _has_classification_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否明确要求分类、归类或整理。"""
    classification_keywords = ["分类", "归类", "整理"]
    english_keywords = ["classify", "categorize"]
    return any(keyword in message for keyword in classification_keywords) or any(
        keyword in lowered for keyword in english_keywords
    )


def _has_capability_help_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否在询问 File Agent 当前可用能力。"""
    chinese_patterns = [
        "你可以做什么",
        "你能做什么",
        "你有什么功能",
        "系统有什么功能",
        "可以帮我做什么",
        "你可以实现什么功能",
        "系统有哪些功能",
    ]
    english_patterns = ["what can you do", "capabilities", "what are your features"]
    return any(pattern in message for pattern in chinese_patterns) or any(
        pattern in lowered for pattern in english_patterns
    )


def _has_classification_taxonomy_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否在询问系统固定分类目录，而不是文件已生成的分类建议。"""
    taxonomy_keywords = [
        "分类目录",
        "分类体系",
        "归类表",
        "文件归类表",
        "支持的文件分类",
        "支持哪些分类",
        "有哪些分类",
    ]
    english_keywords = ["taxonomy", "classification catalog", "category catalog"]
    return any(keyword in message for keyword in taxonomy_keywords) or any(
        keyword in lowered for keyword in english_keywords
    )


def _has_classification_summary_intent(*, message: str) -> bool:
    """判断用户是否想汇总或查看已有分类结果，而不是重新解析正文。"""
    summary_keywords = ["总结", "汇总", "查看", "列出", "统计"]
    classification_keywords = ["分类", "归类", "类别"]
    return any(keyword in message for keyword in summary_keywords) and any(
        keyword in message for keyword in classification_keywords
    )


def _managed_root_key_from_list_request(message: str) -> str | None:
    """从“列出 root_key 下的文件”这类表达中提取受管目录 root_key。"""

    if not any(keyword in message for keyword in ["列出", "查看", "显示"]):
        return None
    if not any(keyword in message for keyword in ["文件", "目录"]):
        return None
    patterns = [
        r"(?:列出|查看|显示)\s*([A-Za-z0-9_-]+)\s*(?:下|目录下|中的|里的|里面的)",
        r"([A-Za-z0-9_-]+)\s*(?:下|目录下|中的|里的|里面的)\s*(?:所有)?\s*文件",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if match:
            return match.group(1)
    return None


def _has_mcp_filesystem_list_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否明确要求实时读取服务器工作目录。"""

    if not any(keyword in message for keyword in ["列出", "查看", "显示"]):
        return False
    if "文件" not in message and "目录" not in message:
        return False
    return (
        "实时" in message
        or "服务器工作目录" in message
        or "服务器当前目录" in message
        or "当前服务器目录" in message
        or "filesystem mcp" in lowered
    )


def _mcp_filesystem_path_prefix_from_list_request(message: str) -> str | None:
    """从实时服务器目录请求中提取 MCP 受管根内相对路径。"""

    tail = re.sub(r"^(?:实时)?(?:列出|查看|显示)\s*", "", message).strip()
    tail = re.sub(r"^(?:服务器工作目录|服务器当前目录|当前服务器目录|工作目录|当前目录)", "", tail).strip()
    tail = re.sub(r"^(?:目录下|下|中的|里的|里面的|内|目录中|目录里)\s*", "", tail)
    tail = _strip_managed_file_filter_words(tail)
    tail = _strip_managed_year_filter_words(tail)
    subject_prefix = _managed_directory_prefix_from_subject_tail(tail)
    if subject_prefix:
        return _normalize_managed_path_prefix(subject_prefix)
    tail = _strip_managed_subject_filter_words(tail)
    tail = re.sub(r"^(?:的)?(?:所有|全部)?\s*", "", tail)
    tail = re.sub(
        r"\s*(?:目录|文件夹)?(?:中|里|下|里面)?(?:的)?(?:所有|全部)?(?:文件|目录)\s*$",
        "",
        tail,
    )
    tail = re.sub(r"(?:目录|文件夹)$", "", tail).strip()
    return _normalize_managed_path_prefix(tail)


def _managed_extension_from_list_request(message: str) -> str | None:
    """从受管目录列表请求中提取文件扩展名过滤条件。"""

    lowered = message.lower()
    for alias in sorted(MANAGED_EXTENSION_ALIASES, key=len, reverse=True):
        normalized_alias = re.escape(alias.lower())
        if re.search(rf"(?<![A-Za-z0-9_]){normalized_alias}(?:\s*(?:文件|文档|表格|图片))?", lowered):
            return MANAGED_EXTENSION_ALIASES[alias]
    return None


def _managed_filename_contains_from_list_request(message: str) -> str | None:
    """从“文件名包含 xxx”这类表达中提取文件名关键字。"""

    year = _managed_year_from_list_request(message)
    if year:
        return year

    patterns = [
        r"(?:关于|有关|相关|主题为|关键词为)\s*[“\"']?(?P<keyword>[^”\"'，。！？]+?)[”\"']?(?:的)?(?:[A-Za-z0-9.]+)?(?:文件|文档|材料)?(?:$|[，。！？\s])",
        r"(?:文件名|名称|名字)\s*(?:包含|含有|带有|里有|中有)\s*[“\"']?(?P<keyword>[^”\"'，。！？\s]+?)[”\"']?(?:的)?(?:[A-Za-z0-9.]+)?(?:文件|文档)?(?:$|[，。！？\s])",
        r"(?:包含|含有|带有)\s*[“\"']?(?P<keyword>[^”\"'，。！？\s]+?)[”\"']?\s*(?:的)?(?:[A-Za-z0-9.]+)?(?:文件|文档)(?:$|[，。！？\s])",
    ]
    for pattern in patterns:
        match = re.search(pattern, message)
        if not match:
            continue
        keyword = _normalize_managed_filename_keyword(match.group("keyword"))
        if keyword:
            return keyword
    directory_subject = _managed_directory_subject_keyword_from_list_request(message)
    if directory_subject:
        return directory_subject
    return None


def _has_managed_file_list_filter_intent(
    *,
    message: str,
    path_prefix: str | None,
    extension: str | None,
    filename_contains: str | None,
) -> bool:
    """判断无明确 root 时是否仍是受管文件元数据列表请求。"""

    if not (path_prefix or extension or filename_contains):
        return False
    return any(keyword in message for keyword in ["列出", "查看", "显示"]) and "文件" in message


def _managed_file_read_filters_from_request(*, message: str, lowered: str) -> Dict[str, str] | None:
    """从“读取某目录下某文件并总结”中提取受管文件定位条件。"""

    if not _has_managed_file_read_intent(message=message, lowered=lowered):
        return None

    root_key = _managed_root_key_from_read_request(message)
    path_prefix = None
    filename_contains = _managed_filename_contains_from_list_request(message)
    directory_match = re.search(
        r"(?:读取|解析|总结|讲解|概括)\s*(?P<prefix>[^，。！？]+?)\s*(?:目录下|下|中|里|里面)\s*(?P<keyword>[^，。！？]+?)(?:的)?(?:相关)?(?:文件|文档|材料)",
        message,
    )
    if directory_match:
        prefix = directory_match.group("prefix").strip()
        keyword = directory_match.group("keyword").strip()
        if root_key and prefix == root_key:
            path_prefix = None
        else:
            path_prefix = _normalize_managed_path_prefix(prefix)
        filename_contains = _normalize_managed_filename_keyword(keyword) or filename_contains
    elif root_key:
        path_prefix = _managed_path_prefix_from_read_request(message=message, root_key=root_key)

    extension = _managed_extension_from_list_request(message)
    if not any([root_key, path_prefix, filename_contains, extension]):
        return None
    filters: Dict[str, str] = {}
    if root_key:
        filters["root_key"] = root_key
    if path_prefix:
        filters["path_prefix"] = path_prefix
    if extension:
        filters["extension"] = extension
    if filename_contains:
        filters["filename_contains"] = filename_contains
    return filters


def _managed_file_rename_filters_from_request(*, message: str, lowered: str) -> Dict[str, str] | None:
    """提取受管目录重命名请求的逻辑目录与文件过滤条件。"""

    if not (
        any(keyword in message for keyword in ["重命名", "改名", "文件名建议", "命名建议"])
        or any(keyword in lowered for keyword in ["rename", "filename suggestion"])
    ):
        return None
    if "文件" not in message and "文档" not in message and "材料" not in message:
        return None

    root_key = _managed_root_key_from_list_request(message)
    path_prefix = _managed_path_prefix_from_rename_request(message=message, root_key=root_key)
    filters: Dict[str, str] = {}
    if root_key:
        filters["root_key"] = root_key
    if path_prefix:
        filters["path_prefix"] = path_prefix
    extension = _managed_extension_from_list_request(message)
    filename_contains = _managed_filename_contains_from_list_request(message)
    if extension:
        filters["extension"] = extension
    if filename_contains:
        filters["filename_contains"] = filename_contains
    return filters


def _managed_path_prefix_from_rename_request(*, message: str, root_key: str | None) -> str | None:
    """从重命名表达中提取受管根目录内的子目录。"""

    if root_key:
        return _managed_path_prefix_from_list_request(message=message, root_key=root_key)

    rename_position = max(message.rfind("重命名"), message.rfind("改名"))
    tail = message[rename_position + (3 if message[rename_position:].startswith("重命名") else 2) :] if rename_position >= 0 else message
    match = re.search(
        r"(?P<prefix>[^，。！？]+?)(?:目录下|文件夹下|下|中|里)(?:的)?(?:所有|全部)?(?:文件|文档|材料)",
        tail,
    )
    if not match:
        match = re.search(
            r"(?:把|将)?(?P<prefix>[^，。！？]+?)(?:目录下|文件夹下|下|中|里)(?:的)?(?:所有|全部)?(?:文件|文档|材料).*(?:重命名|改名)",
            message,
        )
    if not match:
        return None
    prefix = re.sub(r"^(?:把|将|对|给)\s*", "", match.group("prefix").strip())
    return _normalize_managed_path_prefix(prefix)


def _has_managed_file_read_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否想读取受管目录里的文件正文。"""

    if "文件" not in message and "文档" not in message and "材料" not in message:
        return False
    if not (
        any(keyword in message for keyword in ["读取", "解析", "总结", "讲解", "概括", "内容"])
        or any(keyword in lowered for keyword in ["read", "summarize", "summary", "parse"])
    ):
        return False
    return any(keyword in message for keyword in ["下", "目录", "中", "里", "里面"]) or bool(
        _managed_root_key_from_read_request(message)
    )


def _managed_root_key_from_read_request(message: str) -> str | None:
    """从读取受管文件请求中提取 ASCII root_key。"""

    match = re.search(
        r"(?:读取|解析|总结|讲解|概括)\s*([A-Za-z0-9_-]+)\s*(?:目录下|下|中的|里的|里面的)",
        message,
    )
    return match.group(1) if match else None


def _managed_path_prefix_from_read_request(*, message: str, root_key: str) -> str | None:
    """从 root_key 后面的读取请求中提取可选子目录。"""

    root_match = re.search(re.escape(root_key), message)
    if not root_match:
        return None
    tail = message[root_match.end() :].strip()
    tail = re.sub(r"^(?:目录下|下|中的|里的|里面的|内|目录中|目录里)\s*", "", tail)
    tail = _strip_managed_file_filter_words(tail)
    tail = _strip_managed_subject_filter_words(tail)
    tail = re.sub(r"(?:并)?(?:总结|讲解|概括|说明|读取|解析)?(?:内容|正文)?$", "", tail).strip()
    return _normalize_managed_path_prefix(tail)


def _managed_path_prefix_from_list_request(*, message: str, root_key: str | None) -> str | None:
    """从“root_key 下某子目录中的文件”提取受管目录内的相对路径。"""

    if not root_key:
        if not any(keyword in message for keyword in ["列出", "查看", "显示"]) or "文件" not in message:
            return None
        tail = re.sub(r"^(?:列出|查看|显示)\s*", "", message).strip()
        tail = _strip_managed_file_filter_words(tail)
        tail = _strip_managed_year_filter_words(tail)
        subject_prefix = _managed_directory_prefix_from_subject_tail(tail)
        if subject_prefix:
            return _normalize_managed_path_prefix(subject_prefix)
        tail = _strip_managed_subject_filter_words(tail)
        tail = re.sub(r"^(?:的)?(?:所有|全部)?\s*", "", tail)
        tail = re.sub(
            r"\s*(?:目录|文件夹)?(?:中|里|下|里面)?(?:的)?(?:所有|全部)?(?:文件|目录)\s*$",
            "",
            tail,
        )
        tail = re.sub(r"(?:目录|文件夹)$", "", tail).strip()
        return _normalize_managed_path_prefix(tail)

    escaped_root = re.escape(root_key)
    slash_match = re.search(
        rf"{escaped_root}/(?P<path>[^\s，。！？]+?)\s*(?:下|目录下|中的|里的|里面的)?\s*(?:的)?\s*(?:所有)?\s*(?:文件|目录)",
        message,
    )
    if slash_match:
        return _normalize_managed_path_prefix(slash_match.group("path"))

    root_match = re.search(escaped_root, message)
    if not root_match:
        return None

    tail = message[root_match.end() :].strip()
    tail = re.sub(r"^(?:目录下|下|中的|里的|里面的|内|目录中|目录里)\s*", "", tail)
    tail = tail.strip()
    tail = re.sub(r"^(?:的)?(?:所有|全部)?\s*", "", tail)
    tail = _strip_managed_file_filter_words(tail)
    tail = _strip_managed_year_filter_words(tail)
    tail = _strip_managed_subject_filter_words(tail)
    tail = re.sub(r"(?:目录|文件夹)?(?:中|里|下|里面)(?:的)?$", "", tail).strip()
    tail = re.sub(r"(?:目录|文件夹)$", "", tail).strip()
    tail = re.sub(
        r"\s*(?:目录|文件夹)?(?:中|里|下|里面)?(?:的)?(?:所有|全部)?(?:文件|目录)\s*$",
        "",
        tail,
    )
    return _normalize_managed_path_prefix(tail)


def _normalize_managed_path_prefix(value: str | None) -> str | None:
    """规范化 Planner 从自然语言中解析出的受管目录相对路径。"""

    if value is None:
        return None

    normalized = value.replace("\\", "/").strip().strip("/").strip()
    normalized = re.sub(r"\s+", "", normalized)
    if normalized in {"", ".", "的", "所有", "全部"}:
        return None
    if _is_known_managed_file_extension(normalized):
        return None
    if _looks_like_managed_filename_filter_expression(normalized):
        return None
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        return None
    return normalized


def _strip_managed_file_filter_words(value: str) -> str:
    """移除尾部文件类型或文件名过滤表达，避免误当成目录。"""

    stripped = value.strip()
    extension_words = "|".join(
        sorted((re.escape(item.lstrip(".")) for item in MANAGED_EXTENSION_ALIASES), key=len, reverse=True)
    )
    stripped = re.sub(
        rf"(?:所有|全部)?(?:{extension_words})(?:文件|文档|表格|图片)?$",
        "",
        stripped,
        flags=re.IGNORECASE,
    ).strip()
    stripped = re.sub(
        r"(?:文件名|名称|名字)\s*(?:包含|含有|带有|里有|中有)\s*[“\"']?[^”\"'，。！？\s]+[”\"']?$",
        "",
        stripped,
    ).strip()
    return stripped


def _looks_like_managed_filename_filter_expression(value: str) -> bool:
    """判断路径片段是否其实是显式文件名过滤表达，而不是合法多级目录。"""

    return bool(
        re.search(
            r"^(?:文件名|名称|名字)\s*(?:包含|含有|带有|里有|中有)|^(?:包含|含有|带有)",
            value,
        )
    )


def _managed_year_from_list_request(message: str) -> str | None:
    """提取受管文件列表请求中的年份过滤条件。"""

    match = re.search(r"(?P<year>(?:19|20)\d{2})\s*年?", message)
    if not match:
        return None
    return match.group("year")


def _managed_directory_subject_keyword_from_list_request(message: str) -> str | None:
    """提取“目录下 xxx 的相关文件”中的文件名关键字。"""

    match = re.search(
        r"(?:下|中|里|里面)\s*(?:(?:关于|有关)\s*[“\"']?(?P<marked>[^”\"'，。！？]+?)[”\"']?(?:的)?(?:相关)?|[“\"']?(?P<related>[^”\"'，。！？]+?)[”\"']?(?:的)?相关)(?:文件|文档|材料)(?:$|[，。！？\s])",
        message,
    )
    if not match:
        return None
    return _normalize_managed_filename_keyword(match.group("marked") or match.group("related"))


def _managed_directory_prefix_from_subject_tail(value: str) -> str | None:
    """从“党办下科学发展观的相关文件”中提取目录前缀“党办”。"""

    match = re.search(
        r"^(?P<prefix>.+?)(?:下|中|里|里面)\s*(?:关于|有关)?\s*[“\"']?[^”\"'，。！？]+?[”\"']?(?:的)?(?:相关)?(?:文件|文档|材料)?$",
        value,
    )
    if not match:
        return None
    prefix = match.group("prefix").strip()
    return re.sub(r"(?:目录|文件夹)$", "", prefix).strip()


def _strip_managed_year_filter_words(value: str) -> str:
    """从目录片段中移除年份表达，避免“党办2026年”整体变成目录名。"""

    # 如果年份本身是 POSIX 路径段，例如“党办/2026/材料”，必须保留。
    return re.sub(r"(?<!/)(?:19|20)\d{2}\s*年?(?:的)?(?!/)", "", value).strip()


def _strip_managed_subject_filter_words(value: str) -> str:
    """从目录片段中移除“关于 xxx 的文件”这类文件名关键字表达。"""

    stripped = re.sub(
        r"(?:中|里|下|里面)?(?:(?:关于|有关|主题为|关键词为)\s*[“\"']?[^”\"'，。！？]+?[”\"']?(?:的)?(?:相关)?(?:文件|文档|材料)?|[“\"']?[^”\"'，。！？]+?[”\"']?的相关(?:文件|文档|材料)?)$",
        "",
        value,
    ).strip()
    return stripped


def _is_known_managed_file_extension(value: str) -> bool:
    """判断字符串是否是已知文件扩展名或别名。"""

    return value.lower().lstrip(".") in {
        extension.lstrip(".")
        for extension in MANAGED_EXTENSION_ALIASES
    }


def _normalize_managed_filename_keyword(value: str | None) -> str | None:
    """清理文件名包含条件，排除明显不是文件名关键字的过滤词。"""

    if value is None:
        return None
    keyword = value.strip().strip("“”\"'").strip()
    keyword = re.sub(r"(?:的)?相关$", "", keyword).strip()
    extension_words = "|".join(
        sorted((re.escape(item.lstrip(".")) for item in MANAGED_EXTENSION_ALIASES), key=len, reverse=True)
    )
    keyword = re.sub(
        rf"(?:的)?(?:相关)?(?:所有|全部)?(?:{extension_words})?(?:文件|文档|表格|图片|材料)$",
        "",
        keyword,
        flags=re.IGNORECASE,
    ).strip()
    if not keyword or _is_known_managed_file_extension(keyword):
        return None
    return keyword


def _has_plain_document_summary_intent(*, message: str, lowered: str) -> bool:
    """判断用户要总结文件正文，而不是查看已有分类建议。"""
    return _has_summary_intent(
        message=message,
        lowered=lowered,
    ) and not _has_classification_summary_intent(message=message)


def _has_summary_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否要求总结、概括或讲解正文内容。"""
    summary_keywords = ["总结", "概括", "大概", "讲解", "说明一下", "文章内容"]
    english_keywords = ["summary", "summarize", "explain", "overview"]
    return any(keyword in message for keyword in summary_keywords) or any(
        keyword in lowered for keyword in english_keywords
    )


def _has_answer_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否在针对附件正文提问。"""
    question_keywords = ["？", "?", "什么", "哪些", "如何", "怎么", "为什么", "是否", "问", "回答"]
    english_keywords = ["question", "answer", "what", "why", "how"]
    return any(keyword in message for keyword in question_keywords) or any(
        keyword in lowered for keyword in english_keywords
    )


def _has_spreadsheet_analysis_intent(
    *,
    message: str,
    lowered: str,
    attachments: List[Dict[str, Any]],
) -> bool:
    """判断用户是否要求对电子表格执行统计、汇总或筛选等分析。"""
    has_spreadsheet_attachment = any(
        Path(
            str(
                item.get("filename")
                or item.get("original_filename")
                or item.get("name")
                or ""
            )
        ).suffix.lower()
        in SPREADSHEET_SUFFIXES
        for item in attachments
    )
    has_spreadsheet_text = any(
        keyword in message for keyword in ["表格", "工作表", "汇总表", "表中", "表内", "表里"]
    ) or any(keyword in lowered for keyword in ["csv", "tsv", "excel", "xlsx", "xls", "spreadsheet", "sheet"])
    if not has_spreadsheet_attachment and not has_spreadsheet_text:
        return False

    chinese_operations = [
        "统计",
        "汇总",
        "合计",
        "求和",
        "平均",
        "最大",
        "最小",
        "排名",
        "占比",
        "筛选",
        "过滤",
        "分组",
        "对比",
        "趋势",
        "多少",
        "几条",
        "数量",
    ]
    english_operations = [
        "sum",
        "total",
        "count",
        "average",
        "avg",
        "max",
        "min",
        "group",
        "filter",
        "rank",
    ]
    return any(keyword in message for keyword in chinese_operations) or any(
        keyword in lowered for keyword in english_operations
    )


def _has_spreadsheet_profile_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否要求查看表格结构、工作表或字段信息。"""

    scope_keywords = ["表格", "工作表", "sheet", "excel", "csv", "tsv", "schema"]
    profile_keywords = ["结构", "字段", "列信息", "表头", "有哪些工作表", "有哪些sheet", "schema", "profile"]
    analysis_keywords = ["统计", "汇总", "合计", "求和", "平均", "最大", "最小", "筛选", "分组"]
    if any(keyword in message for keyword in analysis_keywords):
        return False
    return (
        any(keyword in message for keyword in scope_keywords)
        or any(keyword in lowered for keyword in scope_keywords)
    ) and (
        any(keyword in message for keyword in profile_keywords)
        or any(keyword in lowered for keyword in profile_keywords)
    )


def _has_spreadsheet_validation_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否要求检查表格公式错误或质量异常。"""

    validation_keywords = [
        "检查",
        "校验",
        "验证",
        "错误",
        "异常",
        "公式错误",
        "引用错误",
        "#REF!",
        "#DIV/0!",
        "#VALUE!",
        "#NAME?",
    ]
    spreadsheet_keywords = ["表格", "工作表", "excel", "xlsx", "xlsm", "csv", "tsv", "公式"]
    return (
        any(keyword in message for keyword in validation_keywords)
        or any(keyword.lower() in lowered for keyword in validation_keywords)
    ) and (
        any(keyword in message for keyword in spreadsheet_keywords)
        or any(keyword in lowered for keyword in spreadsheet_keywords)
    )


def _document_ids(attachments: List[Dict[str, Any]]) -> List[str]:
    """从消息附件中提取 document_id 列表。"""
    return [str(item["document_id"]) for item in attachments if item.get("document_id")]


def _resolved_scope_from_attachments(attachments: List[Dict[str, Any]]) -> str:
    """从后端解析后的附件中读取实际范围，避免 Planner 猜测文件边界。"""

    scopes = {str(item.get("context_scope") or "") for item in attachments if item.get("context_scope")}
    if len(scopes) == 1:
        return next(iter(scopes))
    if len(scopes) > 1:
        return "mixed"
    return "unspecified"
