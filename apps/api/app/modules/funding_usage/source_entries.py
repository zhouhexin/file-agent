"""把单人资助表的论文和会议记录安全映射到教师成果明细汇总表。"""

from __future__ import annotations

from copy import copy
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

from openpyxl import load_workbook

from app.modules.structured_extraction.export import _safe_spreadsheet_text


PAPER_HEADERS = ("序号", "申请人", "期刊名称", "论文名称", "论文类型", "资助类型", "发表时间", "资助类别", "资助金额")
MEETING_HEADERS = ("序号", "申请人", "会议名称", "地点", "召开时间", "是否CCF类", "资助类别", "资助金额", "现场照片")
SUMMARY_HEADERS = ("序号", "姓名", "论文数", "会议数", "成果条数", *map(str, range(2019, 2027)), "资助总额（元）")
PERSON_HEADERS = ("年份", "序号", "成果类型", "期刊/会议名称", "论文/成果名称", "论文类型", "资助类型",
                  "发表/召开时间", "资助类别", "资助金额（元）", "地点", "状态", "备注")


class SourceEntriesError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _name(value) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _text(value) -> str | None:
    result = str(value).strip() if value is not None else ""
    return _safe_spreadsheet_text(result) if result else None


def _cell(value):
    return _safe_spreadsheet_text(value) if isinstance(value, str) else value


def _amount(value) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if (not number.is_finite() or number < 0 or number > Decimal("1000000000")
            or number.as_tuple().exponent < -2):
        return None
    return number


def _year(value) -> int | None:
    if isinstance(value, (date, datetime)):
        result = value.year
    else:
        match = re.match(r"\s*((?:19|20)\d{2})(?!\d)", str(value or ""))
        result = int(match[1]) if match else None
    return result if result in range(2019, 2027) else None


def _footer(sheet) -> int:
    rows = [row for row in range(3, sheet.max_row + 1) if _name(sheet.cell(row, 1).value) == "合计"]
    if len(rows) != 1 or rows[0] != sheet.max_row:
        raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "个人成果页必须以唯一的合计行结束。")
    return rows[0]


def _check_headers(sheet, expected, row_number, *, source: bool) -> None:
    actual = tuple(_name(sheet.cell(row_number, index).value) for index in range(1, len(expected) + 1))
    if actual != tuple(_name(item) for item in expected):
        side = "上传表" if source else "目标表"
        raise SourceEntriesError("FUNDING_ENTRY_SOURCE_INVALID" if source else "FUNDING_ENTRY_TARGET_INVALID",
                                 f"{side}的“{sheet.title}”工作表表头与受控模板不一致。")


def _target_people(book) -> dict[str, int]:
    if "汇总" not in book:
        raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "目标表缺少“汇总”工作表。")
    summary = book["汇总"]
    _check_headers(summary, SUMMARY_HEADERS, 1, source=False)
    people: dict[str, int] = {}
    for row in range(2, summary.max_row + 1):
        name = _name(summary.cell(row, 2).value)
        label = _name(summary.cell(row, 1).value)
        if not name or label in {"总计", "合计", "小计", "总数"} or re.fullmatch(r"\d+人", name):
            continue
        if name in people:
            raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "目标汇总页有同名教师，无法唯一定位。")
        people[name] = row
    return people


def _candidates(book, filename: str) -> list[dict]:
    if len(book.worksheets) < 3:
        raise SourceEntriesError("FUNDING_ENTRY_SOURCE_INVALID", "上传表必须包含第 1 张论文页和第 3 张会议页。")
    paper, meeting = book.worksheets[0], book.worksheets[2]
    _check_headers(paper, PAPER_HEADERS, 1, source=True)
    _check_headers(meeting, MEETING_HEADERS, 1, source=True)
    rows = []
    known_names = set()
    for kind, sheet in (("论文", paper), ("会议", meeting)):
        for row_number in range(2, sheet.max_row + 1):
            values = [sheet.cell(row_number, col).value for col in range(1, 10)]
            if not any(value is not None and str(value).strip() for value in values):
                continue
            name = _name(values[1])
            if name:
                known_names.add(name)
            amount = _amount(values[8] if kind == "论文" else values[7])
            year = _year(values[6] if kind == "论文" else values[4])
            if kind == "论文":
                cells = (str(year) if year else None, _cell(values[0]), kind, _text(values[2]), _text(values[3]),
                         _text(values[4]), _text(values[5]), _text(values[6]), _text(values[7]),
                         float(amount) if amount is not None else None, None, None, None)
            else:
                notes = [f"是否CCF类：{values[5]}" if values[5] is not None else None,
                         f"现场照片：{values[8]}" if values[8] is not None else None]
                cells = (str(year) if year else None, _cell(values[0]), kind, _text(values[2]), None, None,
                         None, _text(values[4]), _text(values[6]),
                         float(amount) if amount is not None else None, _text(values[3]), None,
                         "；".join(note for note in notes if note) or None)
            rows.append({"source_sheet": sheet.title, "source_row": row_number, "kind": kind,
                         "name": name, "amount": float(amount) if amount is not None else None,
                         "year": year, "cells": cells, "owner_inferred": False})
    filename_name = _name(Path(filename).stem.rsplit("-", 1)[-1])
    inferred_name = next(iter(known_names)) if len(known_names) == 1 else None
    for item in rows:
        if (item["kind"] == "会议" and not item["name"]
                and inferred_name and filename_name == inferred_name):
            item["name"] = inferred_name
            item["owner_inferred"] = True
    return rows


def _duplicate_status(sheet, item: dict) -> str | None:
    footer = _footer(sheet)
    cells = item["cells"]
    title = _name(cells[4] if item["kind"] == "论文" else cells[3]).casefold()
    for row in range(3, footer):
        if _name(sheet.cell(row, 3).value) != item["kind"]:
            continue
        existing_title = _name(sheet.cell(row, 5 if item["kind"] == "论文" else 4).value).casefold()
        same_date_amount = (_name(sheet.cell(row, 8).value) == _name(cells[7])
                            and _amount(sheet.cell(row, 10).value) == _amount(cells[9]))
        if title:
            if existing_title != title:
                if item["kind"] == "会议" and not existing_title and same_date_amount:
                    return "POSSIBLE_DUPLICATE"
                continue
            return ("DUPLICATE" if same_date_amount
                    and _name(sheet.cell(row, 4).value) == _name(cells[3])
                    else "CONFLICT")
        if same_date_amount:
            return ("DUPLICATE" if not existing_title
                    and _name(sheet.cell(row, 9).value) == _name(cells[8])
                    and _name(sheet.cell(row, 11).value) == _name(cells[10])
                    else "POSSIBLE_DUPLICATE")
    return None


def evaluate(source_path: Path, target_path: Path, source_filename: str,
             *, confirm_inferred_owner: bool = False) -> tuple[list[dict], str]:
    """逐条预览；返回含内部 cells 的决定和唯一教师姓名。"""
    source = load_workbook(source_path, read_only=True, data_only=False)
    target = load_workbook(target_path, read_only=False, data_only=False)
    try:
        people = _target_people(target)
        candidates = _candidates(source, source_filename)
        names = {_name(item["name"]) for item in candidates if item["name"]}
        if len(names) != 1:
            raise SourceEntriesError("FUNDING_ENTRY_SOURCE_INVALID", "上传表必须只对应一位可辨认的申请人。")
        owner = next(iter(names))
        if any(cell.data_type == "f" for sheet in target for row in sheet for cell in row):
            raise SourceEntriesError("FUNDING_ENTRY_TARGET_FORMULA_UNSUPPORTED", "目标表含公式，不能安全保存新版本。")
        seen = set()
        for item in candidates:
            name = item["name"]
            cells = item["cells"]
            status = None
            if not name or name != owner:
                status = "OWNER_UNKNOWN"
            elif name not in people or name not in target:
                status = "TARGET_PERSON_MISSING"
            elif item["amount"] is None:
                status = "AMOUNT_INVALID"
            elif item["year"] is None:
                status = "YEAR_INVALID"
            elif item["kind"] == "论文" and not cells[4]:
                status = "TITLE_MISSING"
            else:
                sheet = target[name]
                _check_headers(sheet, PERSON_HEADERS, 2, source=False)
                duplicate = _duplicate_status(sheet, item)
                key = (item["kind"], _name(cells[4] if item["kind"] == "论文" else cells[3]).casefold(),
                       _name(cells[7]), item["amount"], _name(cells[8]))
                if duplicate:
                    status = duplicate
                elif key in seen:
                    status = "DUPLICATE"
                elif item["owner_inferred"] and not confirm_inferred_owner:
                    status = "OWNER_CONFIRMATION_REQUIRED"
                else:
                    status = "ADDED_REVIEW" if item["owner_inferred"] or (item["kind"] == "会议" and not cells[3]) else "ADDED"
                if status in {"ADDED", "ADDED_REVIEW"}:
                    seen.add(key)
            item["status"] = status
            item["missing_fields"] = (["申请人"] if item["owner_inferred"] else []) + (
                ["会议名称"] if item["kind"] == "会议" and not cells[3] else []) + (
                ["地点"] if item["kind"] == "会议" and not cells[10] else [])
        return candidates, owner
    finally:
        source.close()
        target.close()


def apply(target_book, decisions: list[dict], owner: str) -> dict:
    """仅添加决定为 ADDED 的行，按个人页重新计算该教师汇总。"""
    people = _target_people(target_book)
    if owner not in people or owner not in target_book:
        raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "目标教师及个人页不存在。")
    sheet = target_book[owner]
    _check_headers(sheet, PERSON_HEADERS, 2, source=False)
    for item in decisions:
        if item["status"] not in {"ADDED", "ADDED_REVIEW"}:
            continue
        footer = _footer(sheet)
        sheet.insert_rows(footer)
        sample_row = footer - 1 if footer > 3 else footer + 1
        for col, value in enumerate(item["cells"], 1):
            cell = sheet.cell(footer, col)
            original = sheet.cell(sample_row, col)
            cell._style = copy(original._style)
            cell.alignment = copy(original.alignment)
            cell.value = value
        notes = [item["cells"][12]] if item["cells"][12] else []
        if item["owner_inferred"]:
            notes.append("申请人根据同文件唯一姓名关联，待核对")
        if item["kind"] == "会议" and not item["cells"][3]:
            notes.append("会议名称待核对")
        if item["kind"] == "会议" and not item["cells"][10]:
            notes.append("地点待核对")
        sheet.cell(footer, 12).value = "待核对" if item["status"] == "ADDED_REVIEW" else None
        sheet.cell(footer, 13).value = "；".join(notes) or None
    footer = _footer(sheet)
    counts = {"论文": 0, "会议": 0}
    years = {year: 0 for year in range(2019, 2027)}
    total = Decimal("0")
    for row in range(3, footer):
        kind = _name(sheet.cell(row, 3).value)
        if kind not in counts:
            raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "个人页有无法计数的成果类型。")
        amount = _amount(sheet.cell(row, 10).value)
        year = _year(sheet.cell(row, 1).value)
        if amount is None or year is None:
            raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "个人页已有金额或年份无效，不能重新计算。")
        counts[kind] += 1
        years[year] += 1
        total += amount
    total_count = sum(counts.values())
    span = "、".join(str(year) for year, count in years.items() if count)
    sheet.cell(footer, 4).value = f"共 {total_count} 项（论文 {counts['论文']}、会议 {counts['会议']}；{span}）"
    sheet.cell(footer, 10).value = float(total)
    summary = target_book["汇总"]
    row = people[owner]
    summary.cell(row, 3).value = counts["论文"]
    summary.cell(row, 4).value = counts["会议"] or None
    summary.cell(row, 5).value = total_count
    for col, count in enumerate(years.values(), 6):
        summary.cell(row, col).value = count or None
    summary.cell(row, 14).value = float(total)
    total_rows = [index for index in range(2, summary.max_row + 1)
                  if _name(summary.cell(index, 1).value) == "总计"]
    if len(total_rows) > 1:
        raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "目标汇总页有多个总计行。")
    if total_rows:
        total_row = total_rows[0]
        for col in range(3, 15):
            aggregate = Decimal("0")
            for person_row in people.values():
                value = summary.cell(person_row, col).value
                if value is None:
                    continue
                number = _amount(value) if col == 14 else None
                if col < 14:
                    try:
                        number = Decimal(str(value)) if not isinstance(value, bool) else None
                    except InvalidOperation:
                        number = None
                    if number is not None and (not number.is_finite() or number < 0 or number != int(number)):
                        number = None
                if number is None:
                    raise SourceEntriesError("FUNDING_ENTRY_TARGET_INVALID", "汇总页已有数量或金额无效，不能重算总计。")
                aggregate += number
            summary.cell(total_row, col).value = float(aggregate) if col == 14 else int(aggregate)
    return {"paper_count": counts["论文"], "meeting_count": counts["会议"],
            "total_count": total_count, "research_total": float(total)}
