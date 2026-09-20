"""共享累计表格的有界输入契约；不接收路径、SQL 或权限配置。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.agent.tool_schemas import StructuredFieldSpec


class StrictInput(BaseModel):
    """拒绝未知参数，避免模型误认为额外指令会被执行。"""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class DatasetCreate(StrictInput):
    """创建共享表格时一次固定字段；不配置 owner 或授权。"""

    name: str = Field(min_length=1, max_length=100)
    fields: list[StructuredFieldSpec] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def check_schema(self):
        """Demo 仅接受可确定性比较的标量字段。"""
        keys = [field.key for field in self.fields]
        if len(keys) != len(set(keys)):
            raise ValueError("字段 key 不能重复")
        for field in self.fields:
            if field.multiple or field.field_type not in {
                "string", "person_name", "organization", "integer", "decimal", "date"
            }:
                raise ValueError("Demo 仅支持文本、整数、小数和日期标量")
        return self


class FieldCandidate(StrictInput):
    """字段原文必须带回宿主正文中的稳定区段与可定位引文。"""

    raw_text: str | None = Field(default=None, max_length=4000)
    page_number: int | None = Field(default=1, ge=1)
    evidence_refs: list["DatasetEvidenceRef"] = Field(default_factory=list, max_length=4)
    confidence: float | None = Field(default=None, ge=0, le=1)


class DatasetEvidenceRef(StrictInput):
    """引用同一基础提取运行中的正式正文区段，可选字符范围用于消歧。"""

    source_unit_id: UUID
    quote: str = Field(min_length=1, max_length=8000)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        """字符范围必须成对出现且为有效左闭右开区间。"""

        if (self.start is None) != (self.end is None):
            raise ValueError("证据 start/end 必须同时提供")
        if self.start is not None and self.end <= self.start:
            raise ValueError("证据 end 必须大于 start")
        return self


class RecordCandidate(StrictInput):
    """记录索引按提交顺序由后端生成。"""

    fields: dict[str, FieldCandidate] = Field(max_length=20)


class DatasetAppend(StrictInput):
    """固定共享工作副本、内容版本、正文运行与提交键。"""

    document_id: UUID
    document_version_id: UUID
    extraction_run_id: UUID
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_origin: Literal["WORKBUDDY"] | None = None
    idempotency_key: str = Field(min_length=1, max_length=120)
    records: list[RecordCandidate] = Field(default_factory=list, max_length=100)


class QueryFilter(StrictInput):
    """字段级固定比较，拒绝任意表达式。"""

    field: str = Field(min_length=1, max_length=64)
    op: Literal["eq", "contains", "gte", "lte"] = "eq"
    value: str | int | float = Field(union_mode="left_to_right")

    @model_validator(mode="after")
    def limit_filter_text(self):
        """查询也限制资源，避免通过过滤值注入超大文本。"""
        if isinstance(self.value, str) and len(self.value) > 4000:
            raise ValueError("过滤值不能超过 4000 字符")
        return self


class DatasetQuery(StrictInput):
    """小数据集的确定性查询与全量筛选后求和。"""

    filters: list[QueryFilter] = Field(default_factory=list, max_length=10)
    sum_field: str | None = Field(default=None, max_length=64)
    include_review: bool = False
    offset: int = Field(default=0, ge=0, le=1000)
    limit: int = Field(default=100, ge=1, le=200)
