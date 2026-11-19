from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from io import BytesIO
from zipfile import ZipFile

from openpyxl import load_workbook

MAX_ROWS = 100_000
MAX_COLUMNS = 100
SUMMARY_HEADERS = ["序号", "姓名", "总科研成果资助金额", "其他资助金额（院优青、专委、其他资助）", "总支出", "剩余总资助金额"]
DETAIL_HEADERS = ["序号", "使用登记原文", "日期", "确认使用金额（元）", "核对说明"]


class WorkbookImportError(ValueError):
    pass


def _money(value, *, signed=False):
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
        if (not number.is_finite() or abs(number) > Decimal("1000000000")
                or (number < 0 and not signed) or number != number.quantize(Decimal("0.01"))):
            return None
        return str(number.quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None


def _text(value):
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return "" if value is None else str(value)


def _safe_workbook(data):
    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 2_000 or sum(item.file_size for item in entries) > 100_000_000:
                raise WorkbookImportError("Excel 解压后文件过大，已停止导入。")
        # Formulas have no trustworthy cached values until recalculated; never treat them as zero.
        return load_workbook(BytesIO(data), read_only=True, data_only=False)
    except WorkbookImportError:
        raise
    except Exception as exc:
        raise WorkbookImportError("上传文件不是有效的 XLSX，或 Excel 无法读取。") from exc


def _rows(sheet, width):
    if (sheet.max_row or 0) > MAX_ROWS or (sheet.max_column or 0) > MAX_COLUMNS:
        raise WorkbookImportError(f"工作表“{sheet.title}”超过行列数量限制。")
    # One streaming pass per worksheet, including worksheets with unspecified dimensions.
    for index, row in enumerate(sheet.iter_rows(max_col=width), 1):
        if index > MAX_ROWS:
            raise WorkbookImportError(f"工作表“{sheet.title}”超过行数限制。")
        yield row


def parse_roster(data):
    book = _safe_workbook(data)
    try:
        sheet = book.worksheets[0]
        rows = iter(_rows(sheet, MAX_COLUMNS))
        headers = [_text(cell.value).strip() for cell in next(rows, ())]
        employee_cols = [i for i, value in enumerate(headers) if value in {"教师工号", "工号", "职工号"}]
        name_cols = [i for i, value in enumerate(headers) if value in {"姓名", "教师姓名"}]
        if len(employee_cols) != 1 or len(name_cols) != 1:
            raise WorkbookImportError("工号名单首个工作表必须有唯一的工号列和姓名列。")
        employee_col, name_col = employee_cols[0], name_cols[0]
        result, seen = [], set()
        for row_number, row in enumerate(rows, 2):
            cell = row[employee_col]
            value = cell.value
            name = row[name_col].value
            if value is None and name is None:
                continue
            if cell.data_type == "f" or row[name_col].data_type == "f" or isinstance(value, bool):
                raise WorkbookImportError(f"名单第 {row_number} 行必须填写真实工号和姓名，不能使用公式。")
            if isinstance(value, (int, float)):
                if not float(value).is_integer() or value < 0 or value > 999999999999999:
                    raise WorkbookImportError(f"名单第 {row_number} 行工号无效，请以文本填写。")
                fmt = str(cell.number_format or "")
                value = str(int(value)).zfill(len(fmt)) if fmt and set(fmt) == {"0"} else str(int(value))
            employee = _text(value).strip()
            if (not employee or len(employee) > 64 or any(ord(c) < 32 for c in employee)
                    or not isinstance(name, str) or not name.strip() or len(name.strip()) > 100):
                raise WorkbookImportError(f"工号名单第 {row_number} 行工号或姓名为空或无效。")
            if employee in seen:
                raise WorkbookImportError(f"工号 {employee} 重复出现。")
            seen.add(employee)
            result.append({"employee_no": employee, "name": name.strip()})
        if not result:
            raise WorkbookImportError("工号名单没有有效人员记录。")
        return result
    finally:
        book.close()


def parse_funding_workbook(data):
    book = _safe_workbook(data)
    try:
        if not book.worksheets or book.worksheets[0].title != "使用登记汇总":
            raise WorkbookImportError("余额表第一张工作表必须名为“使用登记汇总”。")
        rows = iter(_rows(book.worksheets[0], 6))
        if [cell.value for cell in next(rows, ())] != SUMMARY_HEADERS:
            raise WorkbookImportError("余额表汇总页表头与当前资助余额表格式不一致。")
        people, names, sequences = [], set(), set()
        for row_no, row in enumerate(rows, 2):
            values = [cell.value for cell in row]
            if all(value is None for value in values):
                continue
            seq, name = values[:2]
            if (not isinstance(name, str) or not name.strip() or type(seq) is not int or seq < 1
                    or seq in sequences or name.strip() in names):
                raise WorkbookImportError(f"汇总页第 {row_no} 行姓名或序号无效、重复。")
            names.add(name.strip())
            sequences.add(seq)
            people.append({"sequence": seq, "name": name.strip(),
                           "research_total": _money(values[2]), "other_total": _money(values[3]),
                           "total_spent": _money(values[4]), "remaining": _money(values[5], signed=True),
                           "details": [], "needs_review": any(
                               values[i] not in (None, "") and _money(values[i], signed=(i == 5)) is None
                               for i in range(2, 6))})
        if not people:
            raise WorkbookImportError("余额表没有教师汇总行。")
        by_name = {person["name"]: person for person in people}
        by_sequence = {person["sequence"]: person for person in people}
        assigned = set()
        for sheet in book.worksheets[1:]:
            rows = iter(_rows(sheet, 5))
            if [cell.value for cell in next(rows, ())] != DETAIL_HEADERS:
                raise WorkbookImportError(f"工作表“{sheet.title}”不是有效的个人明细页，已停止导入以免丢失明细。")
            person = by_name.get(sheet.title)
            if person is None and sheet.title.startswith("人员") and sheet.title[2:].isdigit():
                person = by_sequence.get(int(sheet.title[2:]))
            if person is None or person["name"] in assigned:
                raise WorkbookImportError(f"个人明细工作表“{sheet.title}”无法唯一匹配教师。")
            assigned.add(person["name"])
            for row in rows:
                values = [cell.value for cell in row]
                if values[0] in SUMMARY_HEADERS[2:]:
                    break
                if all(value is None for value in values):
                    continue
                amount = _money(values[3])
                note = _text(values[4]).strip()
                review = amount is None or "待核对" in note
                person["needs_review"] |= review
                person["details"].append({"sequence": len(person["details"]) + 1,
                                          "registration": _text(values[1]), "date": _text(values[2]),
                                          "usage_amount": amount, "note": note, "needs_review": review})
        for person in people:
            if person["total_spent"] is None or any(item["usage_amount"] is None for item in person["details"]):
                person["needs_review"] = True
                continue
            spent = sum((Decimal(item["usage_amount"]) for item in person["details"]), Decimal("0.00"))
            if spent != Decimal(person["total_spent"]):
                person["needs_review"] = True
            if person["research_total"] is not None or person["other_total"] is not None:
                expected = Decimal(person["research_total"] or "0") + Decimal(person["other_total"] or "0") - spent
                if person["remaining"] is None or expected != Decimal(person["remaining"]):
                    person["needs_review"] = True
            elif person["remaining"] is not None:
                person["needs_review"] = True
        return people
    finally:
        book.close()
