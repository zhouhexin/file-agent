"""将结构化电子表格分析结果格式化为用户可读文本。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any


def format_spreadsheet_analysis_response(results: list[dict[str, Any]]) -> str:
    """格式化一个或多个表格分析 Tool 结果。"""

    if not results:
        return "未获得可展示的表格分析结果。"

    blocks = [_format_one_result(result) for result in results]
    return "\n\n".join(block for block in blocks if block)


def _format_one_result(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "").upper()
    if status == "NEEDS_CLARIFICATION":
        return _format_clarification(result)
    if not result.get("ok") or status == "FAILED":
        return _format_failure(result)

    filename = str(result.get("filename") or "该文件")
    sheet_name = str(result.get("sheet_name") or "未知工作表")
    metric = result.get("metric") if isinstance(result.get("metric"), dict) else {}
    group_by = result.get("group_by") if isinstance(result.get("group_by"), dict) else None
    rows = [item for item in result.get("results", []) if isinstance(item, dict)]

    operation = _operation_label(str(metric.get("operation") or ""))
    column_name = str(metric.get("column_name") or "行数")
    lines = [
        f"已完成《{filename}》中 Sheet“{sheet_name}”的表格分析。",
        f"统计方式：{operation}“{column_name}”。",
    ]

    if group_by:
        lines.append(f"分组字段：{group_by.get('column_name') or '未命名列'}。")

    filters = [item for item in result.get("filters", []) if isinstance(item, dict)]
    if filters:
        lines.append("筛选条件：" + "；".join(_format_filter(item) for item in filters) + "。")

    if not rows:
        lines.append("没有找到符合条件的数据。")
    elif group_by:
        lines.append("结果：")
        lines.extend(
            f"- {item.get('group') or '(空值)'}：{_format_number(item.get('value'))}"
            for item in rows
        )
    else:
        value = _format_number(rows[0].get("value"))
        lines.append(f"结果：{value}")

    lines.append(
        "数据范围："
        f"扫描 {int(result.get('rows_scanned') or 0)} 行，"
        f"筛选匹配 {int(result.get('rows_matched') or 0)} 行，"
        f"纳入计算 {int(result.get('rows_included') or 0)} 行，"
        f"忽略 {int(result.get('rows_ignored') or 0)} 行。"
    )

    warnings = [str(item) for item in result.get("warnings", []) if str(item).strip()]
    if warnings:
        lines.append("提示：" + "；".join(warnings))
    return "\n".join(lines)


def _format_clarification(result: dict[str, Any]) -> str:
    question = str(result.get("message") or "请明确希望统计的字段或分组维度。")
    lines = [question]
    available_sheets = [item for item in result.get("available_sheets", []) if isinstance(item, dict)]
    if available_sheets:
        lines.append("当前文件可用字段：")
        for sheet in available_sheets:
            columns = [str(column) for column in sheet.get("columns", []) if str(column).strip()]
            rendered = "、".join(columns[:12])
            suffix = "……" if len(columns) > 12 else ""
            lines.append(f"- Sheet“{sheet.get('sheet_name') or '未知'}”：{rendered}{suffix}")
    return "\n".join(lines)


def _format_failure(result: dict[str, Any]) -> str:
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    message = str(error.get("message") or result.get("message") or "表格分析未完成。")
    return f"表格分析未完成：{message}"


def _format_filter(item: dict[str, Any]) -> str:
    column = str(item.get("column_name") or "未知列")
    operator = str(item.get("operator") or "")
    value = item.get("value")
    operator_label = {
        "equals": "等于",
        "contains": "包含",
        "in": "属于",
        "between": "介于",
    }.get(operator, operator)
    if isinstance(value, list):
        rendered = "、".join(str(part) for part in value)
    else:
        rendered = str(value)
    return f"“{column}”{operator_label}“{rendered}”"


def _operation_label(operation: str) -> str:
    return {
        "count_rows": "计数",
        "sum": "求和",
        "avg": "平均值",
        "min": "最小值",
        "max": "最大值",
    }.get(operation, operation or "统计")


def _format_number(value: Any) -> str:
    if value is None:
        return "0"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if number == number.to_integral_value():
        return f"{int(number):,}"
    rendered = f"{number:,.10f}".rstrip("0").rstrip(".")
    return rendered
