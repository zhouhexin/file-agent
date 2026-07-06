"""表格工作台 Tool 结果格式化。"""

from __future__ import annotations

from typing import Any


def format_spreadsheet_workbench_response(results: list[dict[str, Any]]) -> str:
    """把 Profile 和校验结果格式化为用户可读回执。"""

    if not results:
        return "未获得可展示的表格工作台结果。"
    blocks = [_format_result(result) for result in results]
    return "\n\n".join(block for block in blocks if block)


def _format_result(result: dict[str, Any]) -> str:
    """按 Tool 输出类型选择格式化策略。"""

    if result.get("kind") == "spreadsheet_profile":
        return _format_profile(result)
    if result.get("kind") == "spreadsheet_validation":
        return _format_validation(result)
    return ""


def _format_profile(result: dict[str, Any]) -> str:
    """格式化表格结构摘要。"""

    if not result.get("ok"):
        return _format_failure(result, fallback="表格结构读取失败。")

    filename = str(result.get("filename") or "该文件")
    sheets = [sheet for sheet in result.get("sheets", []) if isinstance(sheet, dict)]
    lines = [f"已读取《{filename}》的表格结构，共 {len(sheets)} 个 Sheet。"]
    for sheet in sheets[:8]:
        columns = [item for item in sheet.get("columns", []) if isinstance(item, dict)]
        column_names = "、".join(str(column.get("name") or "") for column in columns[:12])
        suffix = "……" if len(columns) > 12 else ""
        lines.append(
            f"- {sheet.get('sheet_name') or '未知工作表'}："
            f"{int(sheet.get('row_count') or 0)} 行数据，字段：{column_names}{suffix}"
        )
    lines.extend(_format_warnings(result))
    return "\n".join(lines)


def _format_validation(result: dict[str, Any]) -> str:
    """格式化表格质量校验结果。"""

    if not result.get("ok"):
        return _format_failure(result, fallback="表格校验失败。")

    filename = str(result.get("filename") or "该文件")
    formula_errors = [item for item in result.get("formula_errors", []) if isinstance(item, dict)]
    lines = [f"已完成《{filename}》的表格质量校验。"]
    if formula_errors:
        lines.append(f"发现 {len(formula_errors)} 个公式或单元格错误：")
        for item in formula_errors[:20]:
            lines.append(
                f"- {item.get('sheet_name') or '未知工作表'}!{item.get('cell') or '?'}："
                f"{item.get('error') or '未知错误'}"
            )
    else:
        lines.append("未发现显式公式错误。")
    lines.extend(_format_warnings(result))
    return "\n".join(lines)


def _format_warnings(result: dict[str, Any]) -> list[str]:
    """格式化校验警告。"""

    warnings = [item for item in result.get("warnings", []) if isinstance(item, dict)]
    if not warnings:
        return []
    lines = ["提示："]
    lines.extend(f"- {item.get('message') or item.get('code')}" for item in warnings[:20])
    return lines


def _format_failure(result: dict[str, Any], *, fallback: str) -> str:
    """格式化结构化失败结果。"""

    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    return str(error.get("message") or fallback)
