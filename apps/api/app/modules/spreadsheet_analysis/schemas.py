"""电子表格分析的受控 schema。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """拒绝 LLM 或调用方注入的未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class ColumnType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    UNKNOWN = "unknown"


class ColumnProfile(StrictModel):
    """工作表中一列的稳定标识与采样信息。"""

    column_id: str = Field(min_length=1)
    column_index: int = Field(ge=1)
    name: str = Field(min_length=1)
    value_type: ColumnType
    non_empty_count: int = Field(ge=0)
    sample_values: list[str] = Field(default_factory=list, max_length=5)


class SheetProfile(StrictModel):
    """单个 Sheet 的结构摘要。"""

    sheet_id: str = Field(min_length=1)
    sheet_name: str = Field(min_length=1)
    header_row: int = Field(ge=1)
    row_count: int = Field(ge=0)
    columns: list[ColumnProfile] = Field(default_factory=list)


class WorkbookProfile(StrictModel):
    """供 LLM 规划器选择 Sheet / 列 ID 的最小工作簿摘要。"""

    document_id: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    sheets: list[SheetProfile] = Field(default_factory=list)


class Aggregation(StrEnum):
    COUNT_ROWS = "count_rows"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class FilterOperator(StrEnum):
    EQUALS = "equals"
    CONTAINS = "contains"
    IN = "in"
    BETWEEN = "between"


class SpreadsheetFilter(StrictModel):
    """受控筛选条件；column_id 必须在后续 Validator 中确认属于目标 Sheet。"""

    column_id: str = Field(min_length=1)
    operator: FilterOperator
    value: Any


class MetricSpec(StrictModel):
    """单个聚合指标。第一版只允许一个指标，降低审计与展示复杂度。"""

    operation: Aggregation
    column_id: str | None = None
    label: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def validate_metric(self) -> "MetricSpec":
        if self.operation == Aggregation.COUNT_ROWS and self.column_id is not None:
            raise ValueError("count_rows 不允许指定 column_id。")
        if self.operation != Aggregation.COUNT_ROWS and not self.column_id:
            raise ValueError("sum、avg、min、max 必须指定 column_id。")
        return self


class SpreadsheetQueryPlan(StrictModel):
    """LLM 生成、Validator 校验、Executor 执行的只读查询计划。"""

    clarification_required: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)

    sheet_id: str | None = None
    metric: MetricSpec | None = None
    group_by_column_id: str | None = None
    filters: list[SpreadsheetFilter] = Field(default_factory=list, max_length=3)
    sort_direction: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=50, ge=1, le=100)

    @model_validator(mode="after")
    def validate_plan(self) -> "SpreadsheetQueryPlan":
        if self.clarification_required:
            if not self.clarification_question:
                raise ValueError("需要澄清时必须提供 clarification_question。")
            return self

        if not self.sheet_id:
            raise ValueError("执行查询必须指定 sheet_id。")
        if self.metric is None:
            raise ValueError("执行查询必须指定 metric。")
        return self
