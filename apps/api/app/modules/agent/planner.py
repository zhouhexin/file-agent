"""MVP Agent Runtime 使用的确定性 Planner 和计划 schema。

后续真实 Planner 可以调用 LLM，但仍必须返回这里定义的声明式结构。
Shell 命令、SQL 写入和文件系统路径会在 Tool dispatch 前被拒绝。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.llm.schemas import UserIntentPlan


FORBIDDEN_INPUT_KEYS = {"shell", "shell_command", "sql", "sql_write", "path", "file_path", "absolute_path"}
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
DOCUMENT_CLASSIFICATION_HINTS = {"read_document_classifications", "read-document-classifications"}
AGENT_CAPABILITY_HINTS = {"read_agent_capabilities", "read-agent-capabilities", "capability_help"}
CLASSIFICATION_TAXONOMY_HINTS = {"read_classification_taxonomy", "read-classification-taxonomy"}
SPREADSHEET_ANALYSIS_HINTS = {"analyze_spreadsheet", "analyze-spreadsheet"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".csv"}


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
            raise ValueError(f"Planner step contains forbidden direct action keys: {sorted(forbidden)}")
        return value

    @field_validator("writes")
    @classmethod
    def reject_direct_writes(cls, value: List[str]) -> List[str]:
        """拒绝直接指向 shell、SQL 或文件系统的写入声明。"""

        bad_writes = [item for item in value if item.startswith(("filesystem:", "shell:", "sql:"))]
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

        if (
            not attachments
            and not _should_extract_text(message=message, lowered=lowered)
            and not _has_classification_intent(message=message, lowered=lowered)
        ):
            return _general_chat_plan(intent="GENERAL_CHAT", user_goal=message)

        document_ids = _document_ids(attachments) or [_first_document_id(attachments)]
        document_id = document_ids[0]
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
                slots={"document_ids": document_ids, "requested_outputs": ["text", "summary", "receipt"]},
                selected_skills=["chat-intake", "document-text-extract", "document-reading"],
                steps=[
                    _extract_document_text_step(
                        document_id=item,
                        index=index,
                        force_reprocess=_should_force_reprocess(message=message, lowered=lowered),
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
                slots={"document_ids": document_ids, "requested_outputs": ["classification_summary"]},
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
                        force_reprocess=_should_force_reprocess(message=message, lowered=lowered),
                    )
                    for index, item in enumerate(document_ids, start=1)
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )
        if _should_extract_text(message=message, lowered=lowered):
            requested_outputs = _requested_outputs_for_message(message=message, lowered=lowered)
            return PlannerOutput(
                intent="SUMMARIZE_DOCUMENTS" if "summary" in requested_outputs else "EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                slots={
                    "document_ids": document_ids,
                    "requested_outputs": requested_outputs,
                },
                selected_skills=["chat-intake", "document-text-extract", "document-classification", "change-report"],
                steps=[
                    _extract_document_text_step(
                        document_id=item,
                        index=index,
                        force_reprocess=_should_force_reprocess(message=message, lowered=lowered),
                    )
                    for index, item in enumerate(document_ids, start=1)
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )
        if _has_classification_intent(message=message, lowered=lowered):
            return PlannerOutput(
                intent="CLASSIFY_FILES",
                user_goal=message,
                slots={"document_ids": document_ids, "requested_outputs": ["classification", "receipt"]},
                selected_skills=["chat-intake", "document-text-extract", "document-classification", "change-report"],
                steps=[
                    _extract_document_text_step(
                        document_id=item,
                        index=index,
                        force_reprocess=_should_force_reprocess(message=message, lowered=lowered),
                    )
                    for index, item in enumerate(document_ids, start=1)
                ],
                evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )

        return PlannerOutput(
            intent="CLASSIFY_FILES",
            user_goal=message,
            slots={"document_ids": document_ids, "requested_outputs": ["classification", "receipt"]},
            selected_skills=["chat-intake", "file-ingest", "document-classification", "change-report"],
            steps=[
                {
                    "step_id": "step-1",
                    "skill": "file-ingest",
                    "tool_name": "document-convert",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["pages", "metadata", "artifacts"],
                    "writes": ["document_pages", "artifacts", "change_items"],
                },
                {
                    "step_id": "step-2",
                    "skill": "file-ingest",
                    "tool_name": "metadata-extract",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["metadata"],
                    "writes": ["documents.metadata"],
                },
                {
                    "step_id": "step-3",
                    "skill": "document-classification",
                    "tool_name": "multi-label-classify",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["document_categories"],
                    "writes": ["document_categories"],
                },
                {
                    "step_id": "step-4",
                    "skill": "change-report",
                    "tool_name": "change-report",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["receipt"],
                    "writes": ["change_sets"],
                },
            ],
            evidence_policy={"require_page_or_cell": True, "allow_no_evidence_answer": False},
            confirmation_policy={"operation_plan_required": False},
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
    requested_capabilities = set(intent_plan.required_capabilities).union(intent_plan.tool_plan_hint)
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
        requested_capabilities.intersection(SPREADSHEET_ANALYSIS_HINTS)
        or _has_spreadsheet_analysis_intent(
            message=message,
            lowered=lowered,
            attachments=attachments,
        )
    ):
        analysis_document_ids = document_ids or _document_ids(attachments)
        if analysis_document_ids:
            return _spreadsheet_analysis_plan(
                user_goal=intent_plan.user_goal or message,
                document_ids=analysis_document_ids,
                question=message,
                selected_skills=["llm-understanding", "spreadsheet-analysis"],
                response_style=intent_plan.response_style,
                clarification_question=intent_plan.clarification_question,
                llm_intent_plan=intent_plan.model_dump(),
            )

    if _has_plain_document_summary_intent(message=message, lowered=lowered):
        extraction_document_ids = document_ids or [_first_document_id(attachments)]
        return PlannerOutput(
            intent="SUMMARIZE_DOCUMENTS",
            user_goal=intent_plan.user_goal,
            slots={
                "document_ids": extraction_document_ids,
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
                    force_reprocess=_should_force_reprocess(message=message, lowered=lowered),
                )
                for index, document_id in enumerate(extraction_document_ids, start=1)
            ],
            evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
            confirmation_policy={"operation_plan_required": False},
        )

    if _has_classification_summary_intent(message=message) or requested_capabilities.intersection(DOCUMENT_CLASSIFICATION_HINTS):
        return PlannerOutput(
            intent="SUMMARIZE_CLASSIFICATIONS",
            user_goal=intent_plan.user_goal,
            slots={
                "document_ids": document_ids,
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

    if requested_capabilities.intersection(TEXT_EXTRACTION_HINTS):
        extraction_document_ids = document_ids or [_first_document_id(attachments)]
        requested_outputs = _requested_outputs_for_intent(
            intent=intent_plan.intent,
            message=message,
        )
        return PlannerOutput(
            intent=intent_plan.intent,
            user_goal=intent_plan.user_goal,
            slots={
                "document_ids": extraction_document_ids,
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
                    force_reprocess=_should_force_reprocess(message=message, lowered=lowered),
                )
                for index, document_id in enumerate(extraction_document_ids, start=1)
            ],
            evidence_policy={"require_page_or_cell": False, "allow_no_evidence_answer": True},
            confirmation_policy={"operation_plan_required": False},
        )

    uses_document_insights = (
        intent_plan.needs_file_context
        or bool(requested_capabilities.intersection(DOCUMENT_INSIGHT_HINTS))
    )
    if uses_document_insights:
        return PlannerOutput(
            intent=intent_plan.intent,
            user_goal=intent_plan.user_goal,
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
        user_goal=intent_plan.user_goal,
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


def _first_document_id(attachments: List[Dict[str, Any]]) -> str:
    """为 MVP 确定性计划解析第一个附件文档 id。"""

    if not attachments:
        return "document-memory"
    return str(attachments[0].get("document_id") or "document-memory")


def _extract_document_text_step(*, document_id: str, index: int, force_reprocess: bool = False) -> Dict[str, Any]:
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

    chinese_patterns = ["你可以做什么", "你能做什么", "你有什么功能", "系统有什么功能", "可以帮我做什么", "你可以实现什么功能"]
    english_patterns = ["what can you do", "capabilities", "what are your features"]
    return any(pattern in message for pattern in chinese_patterns) or any(
        pattern in lowered for pattern in english_patterns
    )


def _has_classification_taxonomy_intent(*, message: str, lowered: str) -> bool:
    """判断用户是否在询问系统固定分类目录，而不是文件已生成的分类建议。"""

    taxonomy_keywords = ["分类目录", "分类体系", "归类表", "文件归类表", "支持的文件分类", "支持哪些分类", "有哪些分类"]
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


def _has_plain_document_summary_intent(*, message: str, lowered: str) -> bool:
    """判断用户要总结文件正文，而不是查看已有分类建议。"""

    return _has_summary_intent(message=message, lowered=lowered) and not _has_classification_summary_intent(
        message=message
    )


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
    """仅凭“电子表格附件 + 分析操作”路由，不依赖任何业务字段名。"""

    has_spreadsheet = any(
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
    if not has_spreadsheet:
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


def _document_ids(attachments: List[Dict[str, Any]]) -> List[str]:
    """从消息附件中提取 document_id 列表。"""

    return [str(item["document_id"]) for item in attachments if item.get("document_id")]
