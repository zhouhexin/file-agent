"""只读、确定性的电子表格查询执行器。"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import openpyxl

from .schemas import (
    Aggregation,
    ColumnProfile,
    FilterOperator,
    SheetProfile,
    SpreadsheetFilter,
    SpreadsheetQueryPlan,
    WorkbookProfile,
)
from .validator import find_column, find_sheet


def execute_query(
    *,
    file_path: Path,
    profile: WorkbookProfile,
    plan: SpreadsheetQueryPlan,
) -> dict:
    """执行已经过 Validator 校验的单 Sheet 只读聚合查询。"""

    if plan.clarification_required or plan.sheet_id is None or plan.metric is None:
        raise ValueError("不能执行需要澄清或不完整的查询计划。")

    sheet = find_sheet(profile, plan.sheet_id)
    metric_column = (
        find_column(sheet, plan.metric.column_id)
        if plan.metric.column_id
        else None
    )
    group_column = (
        find_column(sheet, plan.group_by_column_id)
        if plan.group_by_column_id
        else None
    )

    values_by_group: dict[str, list[Decimal]] = defaultdict(list)
    rows_scanned = 0
    rows_matched = 0
    rows_included = 0
    rows_ignored = 0

    for row in iter_data_rows(file_path=file_path, sheet=sheet):
        rows_scanned += 1
        if not matches_filters(row, plan.filters):
            continue
        rows_matched += 1

        group_key = _display_value(row.get(group_column.column_id)) if group_column else "全部"
        group_key = group_key or "(空值)"

        if plan.metric.operation == Aggregation.COUNT_ROWS:
            values_by_group[group_key].append(Decimal("1"))
            rows_included += 1
            continue

        if metric_column is None:
            raise ValueError("数值聚合缺少目标列。")

        number = to_decimal(row.get(metric_column.column_id))
        if number is None:
            rows_ignored += 1
            continue
        values_by_group[group_key].append(number)
        rows_included += 1

    results = _aggregate_results(
        values_by_group=values_by_group,
        operation=plan.metric.operation,
        sort_direction=plan.sort_direction,
        limit=plan.limit,
    )

    return {
        "kind": "spreadsheet_analysis",
        "ok": True,
        "status": "COMPLETED",
        "sheet_id": sheet.sheet_id,
        "sheet_name": sheet.sheet_name,
        "metric": {
            "operation": plan.metric.operation.value,
            "column_id": metric_column.column_id if metric_column else None,
            "column_name": metric_column.name if metric_column else "行数",
            "label": plan.metric.label or _default_metric_label(plan.metric.operation, metric_column),
        },
        "group_by": (
            {
                "column_id": group_column.column_id,
                "column_name": group_column.name,
            }
            if group_column
            else None
        ),
        "filters": [
            _filter_receipt(sheet=sheet, item=item)
            for item in plan.filters
        ],
        "rows_scanned": rows_scanned,
        "rows_matched": rows_matched,
        "rows_included": rows_included,
        "rows_ignored": rows_ignored,
        "results": results,
        "warnings": _build_warnings(
            rows_matched=rows_matched,
            rows_included=rows_included,
            rows_ignored=rows_ignored,
            metric_column=metric_column,
        ),
    }


def iter_data_rows(*, file_path: Path, sheet: SheetProfile) -> Iterator[dict[str, Any]]:
    """按 Profile 中稳定 column_id 产出每条非空数据行。"""

    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        rows = _read_csv_rows(file_path)
        for row in rows[sheet.header_row :]:
            mapped = _map_row_to_columns(row=row, columns=sheet.columns)
            if _is_nonempty_mapped_row(mapped):
                yield mapped
        return

    if suffix not in {".xlsx", ".xlsm"}:
        raise ValueError("当前仅支持 .xlsx、.xlsm 和 .csv 文件。")

    workbook = openpyxl.load_workbook(
        filename=file_path,
        read_only=True,
        data_only=True,
    )
    try:
        worksheet = _open_selected_sheet(workbook=workbook, sheet_id=sheet.sheet_id)
        for row in worksheet.iter_rows(min_row=sheet.header_row + 1, values_only=True):
            mapped = _map_row_to_columns(row=row, columns=sheet.columns)
            if _is_nonempty_mapped_row(mapped):
                yield mapped
    finally:
        workbook.close()


def matches_filters(row: dict[str, Any], filters: list[SpreadsheetFilter]) -> bool:
    """应用经过校验的 AND 筛选条件。"""

    return all(_matches_filter(row.get(item.column_id), item) for item in filters)


def _matches_filter(cell_value: Any, item: SpreadsheetFilter) -> bool:
    if item.operator == FilterOperator.EQUALS:
        return _values_equal(cell_value, item.value)

    if item.operator == FilterOperator.CONTAINS:
        return _normalize_text(item.value) in _normalize_text(cell_value)

    if item.operator == FilterOperator.IN:
        return any(_values_equal(cell_value, candidate) for candidate in item.value)

    if item.operator == FilterOperator.BETWEEN:
        lower, upper = item.value
        cell_number = to_decimal(cell_value)
        lower_number = to_decimal(lower)
        upper_number = to_decimal(upper)
        if cell_number is not None and lower_number is not None and upper_number is not None:
            return lower_number <= cell_number <= upper_number

        cell_text = _normalize_text(cell_value)
        return _normalize_text(lower) <= cell_text <= _normalize_text(upper)

    return False


def to_decimal(value: Any) -> Decimal | None:
    """把常见数值、货币格式转为 Decimal；拒绝布尔值和不可解析文本。"""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).strip()
    text = text.replace(",", "").replace("，", "")
    text = text.replace("￥", "").replace("¥", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _aggregate_results(
    *,
    values_by_group: dict[str, list[Decimal]],
    operation: Aggregation,
    sort_direction: str,
    limit: int,
) -> list[dict[str, str]]:
    rows: list[dict[str, Any]] = []
    for group, values in values_by_group.items():
        if not values:
            continue
        value = _aggregate(values=values, operation=operation)
        rows.append(
            {
                "group": group,
                "value": _format_decimal(value),
                "_sort_value": value,
            }
        )

    reverse = sort_direction == "desc"
    rows.sort(key=lambda item: (item["_sort_value"], item["group"]), reverse=reverse)
    return [
        {"group": str(item["group"]), "value": str(item["value"])}
        for item in rows[:limit]
    ]


def _aggregate(*, values: list[Decimal], operation: Aggregation) -> Decimal:
    if operation == Aggregation.COUNT_ROWS:
        return Decimal(len(values))
    if operation == Aggregation.SUM:
        return sum(values, Decimal("0"))
    if operation == Aggregation.AVG:
        return sum(values, Decimal("0")) / Decimal(len(values))
    if operation == Aggregation.MIN:
        return min(values)
    if operation == Aggregation.MAX:
        return max(values)
    raise ValueError(f"不支持的聚合操作：{operation.value}")


def _open_selected_sheet(*, workbook: Any, sheet_id: str):
    try:
        sheet_index = int(sheet_id.removeprefix("sheet_")) - 1
    except ValueError as exc:
        raise ValueError("非法 sheet_id。") from exc
    if sheet_index < 0 or sheet_index >= len(workbook.worksheets):
        raise ValueError("目标工作表不存在。")
    return workbook.worksheets[sheet_index]


def _map_row_to_columns(*, row: Sequence[Any], columns: list[ColumnProfile]) -> dict[str, Any]:
    return {
        column.column_id: row[column.column_index - 1]
        if column.column_index - 1 < len(row)
        else None
        for column in columns
    }


def _is_nonempty_mapped_row(row: dict[str, Any]) -> bool:
    return any(value is not None and str(value).strip() for value in row.values())


def _read_csv_rows(file_path: Path) -> list[list[str]]:
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [list(row) for row in csv.reader(handle, dialect)]


def _filter_receipt(*, sheet: SheetProfile, item: SpreadsheetFilter) -> dict[str, Any]:
    column = find_column(sheet, item.column_id)
    return {
        "column_id": column.column_id,
        "column_name": column.name,
        "operator": item.operator.value,
        "value": item.value,
    }


def _build_warnings(
    *,
    rows_matched: int,
    rows_included: int,
    rows_ignored: int,
    metric_column: ColumnProfile | None,
) -> list[str]:
    warnings: list[str] = []
    if rows_matched == 0:
        warnings.append("没有数据行满足当前筛选条件。")
    if metric_column is not None and rows_included == 0 and rows_matched > 0:
        warnings.append(f"筛选后的记录中，“{metric_column.name}”没有可计算的数值。")
    if rows_ignored:
        warnings.append(f"有 {rows_ignored} 行因目标列为空或不是数值而未纳入计算。")
    return warnings


def _default_metric_label(operation: Aggregation, column: ColumnProfile | None) -> str:
    if operation == Aggregation.COUNT_ROWS:
        return "行数"
    column_name = column.name if column else "数值"
    return f"{column_name}（{operation.value}）"


def _values_equal(left: Any, right: Any) -> bool:
    left_number = to_decimal(left)
    right_number = to_decimal(right)
    if left_number is not None and right_number is not None:
        return left_number == right_number
    return _normalize_text(left) == _normalize_text(right)


def _normalize_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ").casefold()
    if isinstance(value, (date, time)):
        return value.isoformat().casefold()
    return str(value or "").strip().casefold()


def _display_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value or "").strip()


def _format_decimal(value: Decimal) -> str:
    if value == value.to_integral_value():
        return str(int(value))
    rendered = f"{value.normalize():f}"
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
