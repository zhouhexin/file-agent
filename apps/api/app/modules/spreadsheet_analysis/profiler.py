"""从 XLSX/XLSM/CSV/TSV 原件构建受控工作簿 Profile。"""

from __future__ import annotations

import csv
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

import openpyxl

from .schemas import ColumnProfile, ColumnType, SheetProfile, WorkbookProfile


MAX_PROFILE_SAMPLE_ROWS = 100
MAX_SAMPLE_VALUES_PER_COLUMN = 5
SUPPORTED_SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv"}


def profile_workbook(
    *,
    document_id: str,
    filename: str,
    file_path: Path,
) -> WorkbookProfile:
    """读取原始工作簿结构；不读取或修改数据库，也不修改文件。"""
    suffix = file_path.suffix.lower()

    if suffix not in SUPPORTED_SPREADSHEET_SUFFIXES:
        raise ValueError("当前仅支持 .xlsx、.xlsm、.csv 和 .tsv 文件。")

    if suffix in {".csv", ".tsv"}:
        sheets = [_profile_delimited_text(file_path=file_path, suffix=suffix)]
    else:
        sheets = _profile_excel(file_path=file_path)

    if not sheets:
        raise ValueError("工作簿中没有可分析的工作表。")

    return WorkbookProfile(
        document_id=document_id,
        filename=filename,
        sheets=sheets,
    )


def _profile_excel(*, file_path: Path) -> list[SheetProfile]:
    workbook = openpyxl.load_workbook(
        filename=file_path,
        read_only=True,
        data_only=True,
    )

    try:
        sheets: list[SheetProfile] = []

        for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
            header_row = detect_header_row(worksheet)
            headers = read_headers(worksheet, header_row)

            if not headers:
                continue

            sheet_id = f"sheet_{sheet_index}"
            columns = build_column_profiles(
                row_iterable=worksheet.iter_rows(
                    min_row=header_row + 1,
                    values_only=True,
                ),
                sheet_id=sheet_id,
                headers=headers,
            )

            sheets.append(
                SheetProfile(
                    sheet_id=sheet_id,
                    sheet_name=worksheet.title or f"Sheet{sheet_index}",
                    header_row=header_row,
                    row_count=_count_nonempty_rows(
                        worksheet.iter_rows(
                            min_row=header_row + 1,
                            values_only=True,
                        )
                    ),
                    columns=columns,
                )
            )

        return sheets
    finally:
        workbook.close()


def _profile_delimited_text(*, file_path: Path, suffix: str) -> SheetProfile:
    """读取 CSV/TSV 的表头和列 Profile；只采样文本，不修改文件。"""

    rows = _read_delimited_rows(file_path=file_path, suffix=suffix)

    if not rows:
        raise ValueError("表格文本文件为空，无法识别表头。")

    header_row_zero_based = _first_non_empty_row_index(rows)
    header_row = header_row_zero_based + 1
    headers = _read_headers_from_values(rows[header_row_zero_based])

    if not headers:
        raise ValueError("CSV 文件未识别到有效表头。")

    columns = build_column_profiles(
        row_iterable=rows[header_row_zero_based + 1 :],
        sheet_id="sheet_1",
        headers=headers,
    )

    return SheetProfile(
        sheet_id="sheet_1",
        sheet_name="TSV" if suffix == ".tsv" else "CSV",
        header_row=header_row,
        row_count=_count_nonempty_rows(rows[header_row_zero_based + 1 :]),
        columns=columns,
    )


def detect_header_row(worksheet: Any) -> int:
    """
    将第一个非空行固定作为 Excel 表头。

    规则：
    - 第 1 行有任何非空单元格：第 1 行就是表头；
    - 第 1 行完全为空：继续查找第 2 行、第 3 行……；
    - 整个 Sheet 都为空：兜底返回第 1 行。

    本函数不再根据单元格数量、唯一值或数值比例“猜测”表头，
    以避免把内容完整的数据行误判为表头。
    """
    return _first_non_empty_row_index(
        worksheet.iter_rows(values_only=True)
    ) + 1


def _first_non_empty_row_index(
    rows: Iterable[Sequence[Any]],
) -> int:
    """返回第一个非空行的 0-based 下标；找不到时返回 0。"""
    for row_index, row in enumerate(rows):
        if _is_nonempty_row(row):
            return row_index

    return 0


def read_headers(worksheet: Any, header_row: int) -> list[str]:
    """读取并去重 Excel 表头，跳过尾部全空列。"""
    row = next(
        worksheet.iter_rows(
            min_row=header_row,
            max_row=header_row,
            values_only=True,
        ),
        (),
    )
    return _read_headers_from_values(row)


def _read_headers_from_values(row: Sequence[Any]) -> list[str]:
    last_non_empty = 0

    for index, value in enumerate(row, start=1):
        if _normalize_header_value(value):
            last_non_empty = index

    if last_non_empty == 0:
        return []

    used: dict[str, int] = {}
    headers: list[str] = []

    for column_index, value in enumerate(row[:last_non_empty], start=1):
        base_name = _normalize_header_value(value) or f"列{column_index}"
        used[base_name] = used.get(base_name, 0) + 1
        name = (
            base_name
            if used[base_name] == 1
            else f"{base_name}_{used[base_name]}"
        )
        headers.append(name)

    return headers


def build_column_profiles(
    *,
    row_iterable: Iterable[Sequence[Any]],
    sheet_id: str,
    headers: list[str],
) -> list[ColumnProfile]:
    """采样前若干个非空值，推断列类型并形成稳定 column_id。"""
    values_by_column: list[list[Any]] = [[] for _ in headers]
    rows_seen = 0

    for row in row_iterable:
        if rows_seen >= MAX_PROFILE_SAMPLE_ROWS:
            break

        if not _is_nonempty_row(row):
            continue

        rows_seen += 1

        for column_index in range(len(headers)):
            value = row[column_index] if column_index < len(row) else None

            if _is_empty(value):
                continue

            values_by_column[column_index].append(value)

    columns: list[ColumnProfile] = []

    for column_index, header in enumerate(headers, start=1):
        values = values_by_column[column_index - 1]
        columns.append(
            ColumnProfile(
                column_id=f"{sheet_id}_col_{column_index}",
                column_index=column_index,
                name=header,
                value_type=infer_column_type(values),
                non_empty_count=len(values),
                sample_values=[
                    _display_value(value)
                    for value in values[:MAX_SAMPLE_VALUES_PER_COLUMN]
                ],
            )
        )

    return columns


def infer_column_type(values: Sequence[Any]) -> ColumnType:
    """按采样值多数推断一列的基础类型。"""
    non_empty = [value for value in values if not _is_empty(value)]

    if not non_empty:
        return ColumnType.UNKNOWN

    if all(isinstance(value, bool) for value in non_empty):
        return ColumnType.BOOLEAN

    if all(isinstance(value, (datetime, date, time)) for value in non_empty):
        return ColumnType.DATE

    if _ratio(non_empty, _is_number_value) >= 0.8:
        return ColumnType.NUMBER

    return ColumnType.STRING


def _read_delimited_rows(*, file_path: Path, suffix: str) -> list[list[str]]:
    """读取 CSV/TSV 行；TSV 固定制表符，CSV 尝试嗅探常见分隔符。"""

    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)

        if suffix == ".tsv":
            return [list(row) for row in csv.reader(handle, delimiter="\t")]

        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        return [list(row) for row in csv.reader(handle, dialect)]


def _count_nonempty_rows(rows: Iterable[Sequence[Any]]) -> int:
    return sum(1 for row in rows if _is_nonempty_row(row))


def _is_nonempty_row(row: Sequence[Any]) -> bool:
    return any(not _is_empty(value) for value in row)


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_header_value(value: Any) -> str:
    return str(value or "").strip()


def _display_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")

    if isinstance(value, (date, time)):
        return value.isoformat()

    return str(value).strip()


def _ratio(values: Sequence[Any], predicate) -> float:
    if not values:
        return 0.0

    return sum(1 for value in values if predicate(value)) / len(values)


def _is_number_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False

    if isinstance(value, (int, float, Decimal)):
        return True

    if isinstance(value, str):
        return _to_decimal(value) is not None

    return False


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
