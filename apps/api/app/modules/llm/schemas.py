"""LLM 结构化输出 schema。

LLM 只能输出这里定义的受控结构，后续 Tool 调用仍由 Agent Planner 和 Tool Registry 校验。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class UserIntentPlan(BaseModel):
    """LLM 对用户自然语言需求的结构化理解结果。"""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1)
    user_goal: str = Field(min_length=1)
    needs_file_context: bool = False
    referenced_document_ids: List[str] = Field(default_factory=list)
    required_capabilities: List[str] = Field(default_factory=list)
    skip_completed_ingest: bool = True
    tool_plan_hint: List[str] = Field(default_factory=list)
    response_style: str = "concise"
    clarification_question: Optional[str] = None
