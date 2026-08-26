"""LLM 结构化输出 schema。

LLM 只能输出这里定义的受控结构，后续 Tool 调用仍由 Agent Planner 和 Tool Registry 校验。
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modules.agent.planner_contracts import CapabilitySuggestionDraft
from app.modules.retrieval.semantic_plan import FileSearchSemanticPlan

ALLOWED_TARGET_SCOPES = {
    "unspecified",
    "current_message",
    "latest_upload_batch",
    "all_conversation",
    "all_recent_context",
    "ordinal_reference",
    "filename_reference",
    "none",
}


class UserIntentPlan(BaseModel):
    """LLM 对用户自然语言需求的结构化理解结果。"""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)
    decision_type: Literal["TOOL_PLAN", "DIRECT_RESPONSE", "CLARIFY"] = "TOOL_PLAN"
    direct_response: Optional[str] = Field(default=None, max_length=4000)
    needs_file_context: bool = False
    target_scope: str = "unspecified"
    referenced_document_ids: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    skip_completed_ingest: bool = True
    tool_plan_hint: List[str] = Field(default_factory=list)
    response_style: str = "concise"
    clarification_question: Optional[str] = None
    capability_suggestions: List[CapabilitySuggestionDraft] = Field(
        default_factory=list,
        max_length=5,
    )

    # 受管目录相关字段只描述用户意图，真实目录和路径仍由后端白名单与 Tool schema 校验。
    managed_root_key: Optional[str] = None
    managed_path_prefix: Optional[str] = None
    managed_path_candidates: List[str] = Field(default_factory=list, max_length=10)
    managed_scope_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    managed_filename_contains: Optional[str] = None
    managed_extension: Optional[str] = None
    managed_query: Optional[str] = None
    file_search_plan: Optional[FileSearchSemanticPlan] = None

    @field_validator("target_scope")
    @classmethod
    def validate_target_scope(cls, value: str) -> str:
        """校验 LLM 只能输出受控的附件范围意图。"""

        if value not in ALLOWED_TARGET_SCOPES:
            raise ValueError(f"Unsupported target_scope: {value}")
        return value

    @model_validator(mode="after")
    def validate_decision_payload(self) -> "UserIntentPlan":
        """校验直接回复和澄清分支具备必要文本，避免进入空响应节点。"""

        if self.decision_type == "DIRECT_RESPONSE" and not str(
            self.direct_response or ""
        ).strip():
            raise ValueError("DIRECT_RESPONSE requires direct_response")
        if self.decision_type == "CLARIFY" and not str(
            self.clarification_question or ""
        ).strip():
            raise ValueError("CLARIFY requires clarification_question")
        return self
