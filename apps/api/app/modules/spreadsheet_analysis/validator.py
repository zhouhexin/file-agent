"""对 LLM 生成的电子表格查询计划执行白名单校验。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .schemas import (
    Aggregation,
    ColumnProfile,
    ColumnType,
    FilterOperator,
    SheetProfile,
    SpreadsheetFilter,
    SpreadsheetQueryPlan,
    WorkbookProfile,
)


class SpreadsheetPlanValidationError(ValueError):
    """查询计划引用了不存在或不允许的结构时抛出。"""


def validate_plan(
    *,
    profile: WorkbookProfile,
    plan: SpreadsheetQueryPlan,
) -> SpreadsheetQueryPlan:
    """校验每一个 Sheet / 列 / 操作 / 筛选条件都来自当前 Profile。"""

    if plan.clarification_required:
        return plan

    if plan.sheet_id is None or plan.metric is None:
        raise SpreadsheetPlanValidationError("查询计划不完整。")

    sheet = find_sheet(profile, plan.sheet_id)
    validate_metric_column(sheet, plan.metric.operation, plan.metric.column_id)
    validate_group_column(sheet, plan.group_by_column_id)
    for item in plan.filters:
        validate_filter(sheet, item)
    return plan


def find_sheet(profile: WorkbookProfile, sheet_id: str) -> SheetProfile:
    for sheet in profile.sheets:
        if sheet.sheet_id == sheet_id:
            return sheet
    raise SpreadsheetPlanValidationError("查询计划引用了不存在的工作表。")


def find_column(sheet: SheetProfile, column_id: str) -> ColumnProfile:
    for column in sheet.columns:
        if column.column_id == column_id:
            return column
    raise SpreadsheetPlanValidationError("查询计划引用了目标工作表中不存在的列。")


def validate_metric_column(
    sheet: SheetProfile,
    operation: Aggregation,
    column_id: str | None,
) -> None:
    if operation == Aggregation.COUNT_ROWS:
        if column_id is not None:
            raise SpreadsheetPlanValidationError("count_rows 不允许指定聚合列。")
        return

    if not column_id:
        raise SpreadsheetPlanValidationError("数值聚合缺少列 ID。")

    column = find_column(sheet, column_id)
    if column.value_type != ColumnType.NUMBER:
        raise SpreadsheetPlanValidationError(
            f"列“{column.name}”不是数值列，不能执行 {operation.value}。"
        )


def validate_group_column(sheet: SheetProfile, column_id: str | None) -> None:
    if column_id:
        find_column(sheet, column_id)


def validate_filter(sheet: SheetProfile, item: SpreadsheetFilter) -> None:
    column = find_column(sheet, item.column_id)
    if item.operator == FilterOperator.IN:
        if not isinstance(item.value, list) or not item.value:
            raise SpreadsheetPlanValidationError("in 筛选必须提供非空数组 value。")
        return

    if item.operator == FilterOperator.BETWEEN:
        if not isinstance(item.value, list) or len(item.value) != 2:
            raise SpreadsheetPlanValidationError("between 筛选必须提供恰好两个边界值。")
        if column.value_type == ColumnType.NUMBER:
            for boundary in item.value:
                if _to_decimal(boundary) is None:
                    raise SpreadsheetPlanValidationError("数值列的 between 边界必须是数值。")
        return

    if item.operator in {FilterOperator.EQUALS, FilterOperator.CONTAINS}:
        if item.value is None or (isinstance(item.value, str) and not item.value.strip()):
            raise SpreadsheetPlanValidationError("筛选条件 value 不能为空。")
        return

    raise SpreadsheetPlanValidationError("不支持的筛选操作。")


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "").replace("，", "")
    text = text.replace("￥", "").replace("¥", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None
