"""图片结构化抽取 Provider、服务和持久化之间的稳定数据契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LayoutBoundingBox(BaseModel):
    """页面坐标系中的矩形证据范围。"""

    model_config = ConfigDict(extra="forbid")

    left: float
    top: float
    right: float
    bottom: float

    @field_validator("left", "top", "right", "bottom")
    @classmethod
    def validate_coordinate(cls, value: float) -> float:
        """所有坐标都必须是有限数值，具体顺序由 Provider 归一化。"""

        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("bbox coordinate must be finite")
        return value


class LayoutElement(BaseModel):
    """与具体 PP-Structure SDK 版本解耦的版面元素。"""

    model_config = ConfigDict(extra="forbid")

    element_index: int = Field(ge=0)
    element_type: str = Field(default="text", min_length=1, max_length=80)
    text: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    bbox: LayoutBoundingBox | None = None
    reading_order: int = Field(default=0, ge=0)
    parent_ref: str | None = Field(default=None, max_length=200)
    table_id: str | None = Field(default=None, max_length=120)
    row_start: int | None = Field(default=None, ge=0)
    row_end: int | None = Field(default=None, ge=0)
    column_start: int | None = Field(default=None, ge=0)
    column_end: int | None = Field(default=None, ge=0)


class LayoutPage(BaseModel):
    """一页图片或扫描 PDF 页的版面解析结果。"""

    model_config = ConfigDict(extra="forbid")

    page_number: int = Field(ge=1)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    rotation: float = 0
    elements: list[LayoutElement] = Field(default_factory=list)


class LayoutParseResult(BaseModel):
    """PP-Structure Provider 对业务层输出的普通可序列化结果。"""

    model_config = ConfigDict(extra="forbid")

    provider: str
    provider_version: str
    pages: list[LayoutPage] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class CandidateFieldValue(BaseModel):
    """抽取模型返回、尚未经过后端归一化的字段候选。"""

    model_config = ConfigDict(extra="forbid")

    raw_text: str | None = Field(default=None, max_length=4000)
    value: Any = None
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_element_ids: list[str] = Field(default_factory=list, max_length=20)


class CandidateRecord(BaseModel):
    """抽取模型返回的一条动态记录。"""

    model_config = ConfigDict(extra="forbid")

    record_index: int = Field(ge=1)
    fields: dict[str, CandidateFieldValue] = Field(default_factory=dict)


class CandidateExtraction(BaseModel):
    """动态字段映射 Provider 的严格输出。"""

    model_config = ConfigDict(extra="forbid")

    discovered_fields: list[dict[str, Any]] = Field(default_factory=list, max_length=40)
    records: list[CandidateRecord] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list, max_length=100)


class NormalizedField(BaseModel):
    """经过类型归一化和证据校验后的单字段事实候选。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    field_type: str
    raw_text: str | None = None
    normalized_value: Any = None
    confidence: float = Field(default=0, ge=0, le=1)
    status: Literal[
        "EXTRACTED",
        "NORMALIZED",
        "NEEDS_REVIEW",
        "MISSING",
        "CONFLICTED",
        "REJECTED",
    ]
    page_number: int | None = None
    bbox: dict[str, Any] = Field(default_factory=dict)
    evidence_element_ids: list[str] = Field(default_factory=list)
    warning_codes: list[str] = Field(default_factory=list)


class StructuredExtractionResult(BaseModel):
    """服务层完成抽取后用于持久化和回执的统一结果。"""

    model_config = ConfigDict(extra="forbid")

    field_schema: list[dict[str, Any]]
    records: list[dict[str, Any]]
    review_items: list[dict[str, Any]]
    record_count: int = Field(ge=0)
    field_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    missing_required_field_count: int = Field(ge=0)
    quality_score: float = Field(ge=0, le=1)
    quality_band: Literal["HIGH", "MEDIUM", "LOW"]
    retryable: bool
    recommended_retry_strategy: Literal["NONE", "REOCR", "VISION_CROP"]
    low_confidence_field_keys: list[str]
    original_unchanged: bool = True
