from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ColumnType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    UNKNOWN = "unknown"


class ColumnProfile(StrictModel):
    column_id: str
    column_index: int
    name: str
    value_type: ColumnType
    non_empty_count: int
    sample_values: list[str] = Field(default_factory=list)


class SheetProfile(StrictModel):
    sheet_id: str
    sheet_name: str
    header_row: int
    row_count: int
    columns: list[ColumnProfile]


class WorkbookProfile(StrictModel):
    document_id: str
    filename: str
    sheets: list[SheetProfile]


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
    column_id: str
    operator: FilterOperator
    value: Any


class MetricSpec(StrictModel):
    operation: Aggregation
    column_id: str | None = None
    label: str = ""

    @model_validator(mode="after")
    def validate_metric(self) -> "MetricSpec":
        if self.operation == Aggregation.COUNT_ROWS and self.column_id is not None:
            raise ValueError("count_rows 不允许指定 column_id。")
        if self.operation != Aggregation.COUNT_ROWS and not self.column_id:
            raise ValueError("聚合运算必须指定 column_id。")
        return self


class SpreadsheetQueryPlan(StrictModel):
    sheet_id: str
    metric: MetricSpec
    group_by_column_id: str | None = None
    filters: list[SpreadsheetFilter] = Field(default_factory=list, max_length=3)
    sort_direction: str = "desc"
    limit: int = Field(default=50, ge=1, le=100)