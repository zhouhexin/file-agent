"""分类建议反馈 API 数据契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


CategoryPathSegment = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class ClassificationFeedbackRequest(BaseModel):
    """用户对一条分类建议的明确反馈。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["ACCEPT", "REJECT", "CORRECT"]
    corrected_category_id: str | None = Field(default=None, max_length=255)
    corrected_category_path: list[CategoryPathSegment] = Field(default_factory=list, max_length=20)
    comment: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_correction(self) -> "ClassificationFeedbackRequest":
        """更正操作必须提供目标稳定 ID 或完整路径。"""

        if self.action == "CORRECT" and not (
            (self.corrected_category_id or "").strip() or self.corrected_category_path
        ):
            raise ValueError("更正分类时必须提供 corrected_category_id 或 corrected_category_path。")
        if self.action != "CORRECT" and (
            self.corrected_category_id or self.corrected_category_path
        ):
            raise ValueError("只有 CORRECT 操作可以提供更正后的分类。")
        return self


class ClassificationFeedbackResponse(BaseModel):
    """已持久化反馈及其样本含义。"""

    id: str
    suggestion_id: str
    document_id: str
    action: str
    corrected_category_id: str | None
    corrected_category_path: list[str]
    positive_category_ids: list[str]
    negative_category_ids: list[str]
    created_at: datetime


class ClassificationFeedbackSummaryResponse(BaseModel):
    """冷启动反馈积累状态。"""

    total: int
    accepted: int
    rejected: int
    corrected: int
    unique_documents: int
    evaluation_min_samples: int
    ready_to_freeze_evaluation_set: bool
