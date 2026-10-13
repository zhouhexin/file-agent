"""在 WorkBuddy 本机仅改 XLSX 目标数据格；其余 OOXML part 原字节保留。"""

from __future__ import annotations

import os
import math
import posixpath
import re
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


_EXCEL_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _excel_display_text(value):
    return _EXCEL_BREAK_RE.sub("\n", value) if isinstance(value, str) else value


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column(number: int) -> str:
    name = ""
    while number:
        number, digit = divmod(number - 1, 26)
        name = chr(65 + digit) + name
    return name


def _column_number(name: str) -> int:
    number = 0
    for character in name:
        number = 26 * number + ord(character) - 64
    return number


def _bounds(reference: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", reference or "")
    if not match:
        raise ValueError("WRITEBACK_LAYOUT_INVALID: 数据区域必须是明确的 A1:B2 格式")
    left, top, right, bottom = match.groups()
    result = (_column_number(left), int(top), _column_number(right), int(bottom))
    if result[0] > result[2] or result[1] > result[3]:
        raise ValueError("WRITEBACK_LAYOUT_INVALID: 数据区域边界无效")
    return result


def _sheet_path(package: dict[str, bytes], sheet_name: str) -> str:
    book = ET.fromstring(package["xl/workbook.xml"])
    relation_id = next((value for node in book.iter() if _local(node.tag) == "sheet"
        and node.attrib.get("name") == sheet_name for key, value in node.attrib.items()
        if key.endswith("}id") or key == "id"), None)
    if not relation_id:
        raise ValueError("WRITEBACK_SHEET_NOT_FOUND: 目标工作表不存在")
    relations = ET.fromstring(package["xl/_rels/workbook.xml.rels"])
    target = next((node.attrib.get("Target") for node in relations
                   if node.attrib.get("Id") == relation_id), None)
    if not target:
        raise ValueError("WRITEBACK_XLSX_INVALID: 工作表关系缺失")
    path = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                             else posixpath.join("xl", target))
    if not path.startswith("xl/worksheets/") or path not in package:
        raise ValueError("WRITEBACK_XLSX_INVALID: 工作表路径不安全")
    return path


def _cells(row: ET.Element | None) -> dict[int, ET.Element]:
    if row is None:
        return {}
    result = {}
    for cell in row:
        if _local(cell.tag) != "c":
            continue
        match = re.fullmatch(r"([A-Z]+)\d+", cell.attrib.get("r", ""))
        if match:
            result[_column_number(match.group(1))] = cell
    return result


def _cell_value(cell: ET.Element | None, strings: list[str]):
    if cell is None:
        return None
    formula = next((node for node in cell if _local(node.tag) == "f"), None)
    if formula is not None:
        return "=" + (formula.text or "")
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
    value = next((node.text for node in cell if _local(node.tag) == "v"), None)
    if cell.attrib.get("t") == "s" and value is not None:
        return strings[int(value)]
    return value


def _insert_row(sheet_data: ET.Element, row: ET.Element, number: int) -> None:
    for offset, current in enumerate(sheet_data):
        if _local(current.tag) == "row" and int(current.attrib.get("r", "0")) > number:
            sheet_data.insert(offset, row)
            return
    sheet_data.append(row)


def _insert_cell(row: ET.Element, cell: ET.Element, column: int) -> None:
    for offset, current in enumerate(row):
        match = re.match(r"([A-Z]+)\d+", current.attrib.get("r", ""))
        if match and _column_number(match.group(1)) > column:
            row.insert(offset, cell)
            return
    row.append(cell)


def _set_value(cell: ET.Element, value, qname) -> None:
    for child in list(cell):
        if _local(child.tag) in {"f", "v", "is"}:
            cell.remove(child)
    if isinstance(value, bool):
        cell.set("t", "b")
        ET.SubElement(cell, qname("v")).text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        cell.attrib.pop("t", None)
        ET.SubElement(cell, qname("v")).text = str(value)
    else:
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, qname("is"))
        text = ET.SubElement(inline, qname("t"))
        value = str(value)
        if value != value.strip() or "\n" in value or "\t" in value:
            text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        text.text = value


def _shift_scoped_footer(root: ET.Element, data: ET.Element, first_row: int, count: int) -> None:
    """给定向追加腾出行；只支持可安全重定位的普通单元格与合并区域。"""
    unsupported = {"autoFilter", "tableParts", "dataValidations", "conditionalFormatting",
                   "hyperlinks", "rowBreaks", "drawing", "legacyDrawing", "extLst"}
    if any(_local(node.tag) in unsupported for node in root):
        raise ValueError("WRITEBACK_ROW_INSERT_UNSUPPORTED: 工作表包含不能安全移动的区域引用")
    if any(_local(node.tag) == "f" for node in data.iter()):
        raise ValueError("WRITEBACK_ROW_INSERT_UNSUPPORTED: 工作表公式不能安全移动")
    for row in data:
        if _local(row.tag) != "row" or int(row.attrib.get("r", "0")) < first_row:
            continue
        number = int(row.attrib["r"]) + count
        row.set("r", str(number))
        for cell in row:
            if _local(cell.tag) == "c":
                match = re.fullmatch(r"([A-Z]+)\d+", cell.attrib.get("r", ""))
                if not match:
                    raise ValueError("WRITEBACK_XLSX_INVALID: 尾注单元格坐标无效")
                cell.set("r", f"{match.group(1)}{number}")
    for node in root.iter():
        if _local(node.tag) != "mergeCell":
            continue
        left, top, right, bottom = _bounds(node.attrib.get("ref", ""))
        if top < first_row <= bottom:
            raise ValueError("WRITEBACK_ROW_INSERT_UNSUPPORTED: 合并区域跨越插入点")
        if top >= first_row:
            node.set("ref", f"{_column(left)}{top + count}:{_column(right)}{bottom + count}")
    dimension = next((node for node in root if _local(node.tag) == "dimension"), None)
    if dimension is not None:
        match = re.fullmatch(r"([A-Z]+\d+:)?([A-Z]+)(\d+)", dimension.attrib.get("ref", ""))
        if match and int(match.group(3)) >= first_row:
            dimension.set("ref", f"{match.group(1) or ''}{match.group(2)}{int(match.group(3)) + count}")


def _data_row_style_cells(row_nodes: dict[int, ET.Element], strings: list[str], *,
                          left: int, right: int, top: int, bottom: int,
                          sequence: set[int]) -> dict[int, ET.Element]:
    """新增行继承数据区里样式覆盖最完整的一行。

    空模板的每一行原本都没有值（只有带样式的空格子），若只按“有值的列数”挑行，
    会被只含零星占位值（如 O6 的 "."）的行抢到，导致新增行缺少自动换行等模板样式。
    因此先比样式覆盖（单元格数量），再比实际填值数量，最后沿用较新的行。
    """
    fallback = _cells(row_nodes.get(bottom))
    candidates = []
    for number in range(top, bottom + 1):
        cells = _cells(row_nodes.get(number))
        if not cells:
            continue
        covered = sum(1 for column in range(left, right + 1) if column in cells)
        populated = sum(_cell_value(cells.get(column), strings) is not None
                        for column in range(left, right + 1) if column - left not in sequence)
        candidates.append((covered, populated, number, cells))
    if not candidates:
        return fallback
    return {**fallback, **max(candidates, key=lambda item: item[:3])[3]}


def patch_workbook(*, baseline: Path, output: Path, sheet_name: str,
                   labels: list[str], rows: list[list], layout: dict | None,
                   scoped_a_i: bool = False) -> None:
    """从冻结基线重建候选；重复调用不会在已编辑结果上重复追加。"""
    if not rows or any(len(row) != len(labels) for row in rows):
        raise ValueError("WRITEBACK_ROWS_MISMATCH: 每行必须覆盖全部目标列")
    rows = [[_excel_display_text(value) for value in row] for row in rows]
    if baseline.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("WRITEBACK_TARGET_TOO_LARGE: 工作簿超过 32 MiB 回传上限")
    with zipfile.ZipFile(baseline) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if (len(names) != len(set(names)) or len(names) > 1000
                or sum(info.file_size for info in infos) > 64 * 1024 * 1024
                or any(info.file_size > (16 if info.filename.lower().endswith((".xml", ".rels")) else 32) * 1024 * 1024
                       for info in infos)):
            raise ValueError("WRITEBACK_XLSX_INVALID: 工作簿包含重复部件或异常体积")
        package = {}
        actual_total = 0
        for info in infos:
            part_limit = (16 if info.filename.lower().endswith((".xml", ".rels")) else 32) * 1024 * 1024
            chunks = []
            actual_part = 0
            with archive.open(info) as source:
                while chunk := source.read(1024 * 1024):
                    actual_part += len(chunk)
                    actual_total += len(chunk)
                    if actual_part > part_limit or actual_total > 64 * 1024 * 1024:
                        raise ValueError("WRITEBACK_XLSX_INVALID: 工作簿实际解压体积超过安全上限")
                    chunks.append(chunk)
            package[info.filename] = b"".join(chunks)
    sheet_path = _sheet_path(package, sheet_name)
    strings = []
    if "xl/sharedStrings.xml" in package:
        shared = ET.fromstring(package["xl/sharedStrings.xml"])
        strings = ["".join(node.text or "" for node in item.iter() if _local(node.tag) == "t")
                   for item in shared if _local(item.tag) == "si"]
    root = ET.fromstring(package[sheet_path])
    if not layout and any(_local(node.tag) == "tableParts" for node in root):
        raise ValueError("WRITEBACK_TABLE_UNSUPPORTED: 此模板包含 Excel 表格对象，当前本地写入器不能安全扩展其范围")
    namespace = root.tag.split("}", 1)[0][1:] if root.tag.startswith("{") else ""
    qname = lambda name: f"{{{namespace}}}{name}" if namespace else name
    data = next((node for node in root if _local(node.tag) == "sheetData"), None)
    if data is None:
        raise ValueError("WRITEBACK_XLSX_INVALID: 工作表缺少数据区")
    row_nodes = {int(node.attrib["r"]): node for node in data
                 if _local(node.tag) == "row" and node.attrib.get("r", "").isdigit()}
    if layout:
        left, top, right, bottom = _bounds(layout["data_range"])
        if right - left + 1 != len(labels):
            raise ValueError("WRITEBACK_LAYOUT_INVALID: 数据区列数与目标字段不一致")
        sequence = {offset for offset, label in enumerate(labels) if label in {"序号", "编号"}}
        if scoped_a_i and left == 1:
            sequence.add(0)
        available = [number for number in range(top, bottom + 1) if not any(
            _cell_value(_cells(row_nodes.get(number)).get(left + offset), strings) is not None
            for offset in range(len(labels)) if offset not in sequence)]
        extra = max(0, len(rows) - len(available)) if scoped_a_i else 0
        if extra:
            if bottom + extra > 1_048_576:
                raise ValueError("TARGET_EXCEL_DATA_RANGE_FULL: 已达到 Excel 最大行数")
            _shift_scoped_footer(root, data, bottom + 1, extra)
            row_nodes = {int(node.attrib["r"]): node for node in data
                         if _local(node.tag) == "row" and node.attrib.get("r", "").isdigit()}
            available.extend(range(bottom + 1, bottom + 1 + extra))
        if len(available) < len(rows):
            raise ValueError("TARGET_EXCEL_DATA_RANGE_FULL: 模板预留行不足")
        destinations = available[:len(rows)]
        style_cells = _data_row_style_cells(row_nodes, strings, left=left, right=right,
                                            top=top, bottom=bottom, sequence=sequence)
    else:
        header = next((number for number in sorted(row_nodes)
                       if any(_cell_value(cell, strings) is not None for cell in _cells(row_nodes[number]).values())), None)
        if header is None:
            raise ValueError("WRITEBACK_TEMPLATE_CHANGED: 目标缺少标题行")
        actual = [_cell_value(_cells(row_nodes[header]).get(column), strings) for column in range(1, len(labels) + 1)]
        if [str(item or "").strip() for item in actual] != [str(item).strip() for item in labels]:
            raise ValueError("WRITEBACK_TEMPLATE_CHANGED: 目标标题与 checkout 不一致")
        left = 1
        last = max((number for number in row_nodes if number > header and any(
            _cell_value(_cells(row_nodes[number]).get(column), strings) is not None
            for column in range(1, len(labels) + 1))), default=header)
        destinations = list(range(last + 1, last + 1 + len(rows)))
        sequence = set()
        # 空模板没有数据样式行；不能把标题行样式伪装成新增数据行。
        style_cells = _cells(row_nodes[last]) if last > header else {}
    for number, values in zip(destinations, rows):
        target_row = row_nodes.get(number)
        if target_row is None:
            target_row = ET.Element(qname("row"), {"r": str(number)})
            if scoped_a_i and layout:
                for column in range(left, right + 1):
                    style = style_cells.get(column)
                    if style is not None and "s" in style.attrib:
                        ET.SubElement(target_row, qname("c"), {
                            "r": f"{_column(column)}{number}", "s": style.attrib["s"]})
            _insert_row(data, target_row, number)
            row_nodes[number] = target_row
        target_cells = _cells(target_row)
        if scoped_a_i and _cell_value(target_cells.get(left), strings) is None:
            previous = next((_cell_value(_cells(row_nodes.get(prior)).get(left), strings)
                             for prior in range(number - 1, top - 1, -1)
                             if _cell_value(_cells(row_nodes.get(prior)).get(left), strings) is not None), None)
            try:
                sequence_value = int(previous) if previous is not None else 0
                if (sequence_value < (0 if previous is None else 1)
                        or previous is not None and str(sequence_value) != str(previous)):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError(f"TARGET_EXCEL_SEQUENCE_MISSING: A{number} 无法从上一序号生成") from None
            values = [sequence_value + 1, *values[1:]]
        for offset, value in enumerate(values):
            column = left + offset
            before = target_cells.get(column)
            existing = _cell_value(before, strings)
            if offset in sequence and existing is not None:
                if value is not None and str(value) != str(existing):
                    raise ValueError(f"TARGET_EXCEL_SEQUENCE_CONFLICT: {_column(column)}{number}")
                continue
            if existing is not None:
                raise ValueError(f"WRITEBACK_EXISTING_CELL_CHANGED: {_column(column)}{number}")
            if value is None:
                continue
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                raise ValueError(f"WRITEBACK_FORMULA_NOT_ALLOWED: {_column(column)}{number}")
            if before is None:
                before = ET.Element(qname("c"), {"r": f"{_column(column)}{number}"})
                if "s" in getattr(style_cells.get(column), "attrib", {}):
                    before.set("s", style_cells[column].attrib["s"])
                _insert_cell(target_row, before, column)
            _set_value(before, value, qname)
        multiline = [value for value in values if isinstance(value, str) and "\n" in value]
        if multiline:
            visual_lines = max(sum(max(1, math.ceil(len(part) / 30)) for part in value.split("\n"))
                               for value in multiline)
            requested_height = min(409.5, max(30.0, visual_lines * 18.0))
            previous_height = float(target_row.attrib.get("ht", "0"))
            if requested_height > previous_height:
                target_row.set("ht", str(requested_height))
                target_row.set("customHeight", "1")
    final = max(destinations)
    dimension = next((node for node in root if _local(node.tag) == "dimension"), None)
    if dimension is not None and dimension.attrib.get("ref"):
        reference = dimension.attrib["ref"]
        match = re.fullmatch(r"(.+:)?([A-Z]+)(\d+)", reference)
        if match and final > int(match.group(3)):
            dimension.set("ref", f"{match.group(1) or ''}{match.group(2)}{final}")
    package[sheet_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="writeback-", suffix=".xlsx", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as result:
            for info in infos:
                result.writestr(info, package[info.filename])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def patch_existing_cells(*, baseline: Path, output: Path, sheet_name: str,
                         row_number: int, changes: dict[str, tuple[object, object]]) -> None:
    """只从冻结基线替换指定行的数据格；不对已有候选反复编辑。"""
    if not changes or baseline.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("CORRECTION_INPUT_INVALID: 更正格为空或目标过大")
    with zipfile.ZipFile(baseline) as archive:
        infos = archive.infolist()
        if (len(infos) > 1000 or len({info.filename for info in infos}) != len(infos)
                or sum(info.file_size for info in infos) > 64 * 1024 * 1024):
            raise ValueError("WRITEBACK_XLSX_INVALID: 工作簿部件或体积异常")
        package = {}
        total = 0
        for info in infos:
            limit = (16 if info.filename.lower().endswith((".xml", ".rels")) else 32) * 1024 * 1024
            chunks = []
            size = 0
            with archive.open(info) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    total += len(chunk)
                    if size > limit or total > 64 * 1024 * 1024:
                        raise ValueError("WRITEBACK_XLSX_INVALID: 工作簿实际解压体积超过安全上限")
                    chunks.append(chunk)
            package[info.filename] = b"".join(chunks)
    sheet_path = _sheet_path(package, sheet_name)
    strings = []
    if "xl/sharedStrings.xml" in package:
        root = ET.fromstring(package["xl/sharedStrings.xml"])
        strings = ["".join(node.text or "" for node in item.iter() if _local(node.tag) == "t")
                   for item in root if _local(item.tag) == "si"]
    root = ET.fromstring(package[sheet_path])
    namespace = root.tag.split("}", 1)[0][1:] if root.tag.startswith("{") else ""
    qname = lambda name: f"{{{namespace}}}{name}" if namespace else name
    data = next((node for node in root if _local(node.tag) == "sheetData"), None)
    row = next((node for node in data or [] if _local(node.tag) == "row"
                and node.attrib.get("r") == str(row_number)), None)
    if row is None:
        raise ValueError("CORRECTION_ROW_MISMATCH: 当前模板没有指定数据行")
    cells = _cells(row)
    for coordinate, (expected_before, after) in changes.items():
        match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", coordinate)
        if not match or int(match.group(2)) != row_number:
            raise ValueError("CORRECTION_CELL_INVALID: 更正坐标不属于目标行")
        column = _column_number(match.group(1))
        existing = cells.get(column)
        before = _cell_value(existing, strings)
        if (before is None) != (expected_before is None) or (before is not None and str(before) != str(expected_before)):
            raise ValueError(f"CORRECTION_BEFORE_CHANGED: {coordinate}")
        if before is not None and str(before).startswith("="):
            raise ValueError(f"WRITEBACK_FORMULA_NOT_ALLOWED: {coordinate}")
        if isinstance(after, str) and after.startswith(("=", "+", "-", "@")):
            raise ValueError(f"WRITEBACK_FORMULA_NOT_ALLOWED: {coordinate}")
        if existing is None:
            if after is None:
                continue
            existing = ET.Element(qname("c"), {"r": coordinate})
            _insert_cell(row, existing, column)
        if after is None:
            for child in list(existing):
                if _local(child.tag) in {"f", "v", "is"}:
                    existing.remove(child)
            existing.attrib.pop("t", None)
        else:
            _set_value(existing, after, qname)
    package[sheet_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="correction-", suffix=".xlsx", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, "w") as result:
            for info in infos:
                result.writestr(info, package[info.filename])
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
