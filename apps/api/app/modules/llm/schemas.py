"""LLM 结构化输出 schema。

LLM 只能输出这里定义的受控结构，后续 Tool 调用仍由 Agent Planner 和 Tool Registry 校验。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    needs_file_context: bool = False
    target_scope: str = "unspecified"
    referenced_document_ids: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    skip_completed_ingest: bool = True
    tool_plan_hint: List[str] = Field(default_factory=list)
    response_style: str = "concise"
    clarification_question: Optional[str] = None

    # 受管目录相关字段只描述用户意图，真实目录和路径仍由后端白名单与 Tool schema 校验。
    managed_root_key: Optional[str] = None
    managed_path_prefix: Optional[str] = None
    managed_filename_contains: Optional[str] = None
    managed_extension: Optional[str] = None
    managed_query: Optional[str] = None

    @field_validator("target_scope")
    @classmethod
    def validate_target_scope(cls, value: str) -> str:
        """校验 LLM 只能输出受控的附件范围意图。"""

        if value not in ALLOWED_TARGET_SCOPES:
            raise ValueError(f"Unsupported target_scope: {value}")
        return value
