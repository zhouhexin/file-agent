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
    relation_role: Literal["PRIMARY", "SECONDARY", "RELATED", "DOCUMENT_TYPE"] = "RELATED"
    agent_run_id: str | None = Field(default=None, min_length=36, max_length=36)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)
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
    document_version_id: str | None = None
    working_copy_id: str | None = None
    action: str
    corrected_category_id: str | None
    corrected_category_path: list[str]
    positive_category_ids: list[str]
    negative_category_ids: list[str]
    changeset_id: str | None = None
    file_position_changed: bool = False
    user_message: str = "分类决定已保存，文件位置未改变。"
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


class ClassificationClarificationResolveRequest(BaseModel):
    """分类选择卡只允许提交后端签发的 option_id。"""

    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(min_length=1, max_length=80)


class ClassificationClarificationOptionResponse(BaseModel):
    """普通页面可见的文件与分类标签。"""

    id: str
    filename: str
    category_label: str


class ClassificationClarificationResponse(BaseModel):
    """分类歧义选择卡，不包含工作副本和建议内部 ID。"""

    id: str
    status: str
    prompt: str
    action: str
    options: list[ClassificationClarificationOptionResponse]
    expires_at: str


class ClassificationTaxonomyOptionResponse(BaseModel):
    """前端分类选择器可使用的当前 taxonomy 稳定选项。"""

    category_id: str
    label: str
    path: list[str]


class ClassificationTaxonomyOptionsResponse(BaseModel):
    """当前启用分类目录的安全扁平投影。"""

    taxonomy_key: str
    taxonomy_version: str
    options: list[ClassificationTaxonomyOptionResponse]
