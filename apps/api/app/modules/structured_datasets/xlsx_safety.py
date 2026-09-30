"""累计 Excel 的有界 OOXML 预检；任何工作簿解析器调用前先执行。"""

from __future__ import annotations

import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET


@dataclass(frozen=True)
class XlsxLimits:
    """为新旧累计表入口共用的小型工作簿安全上限。"""

    archive_bytes: int = 32 * 1024 * 1024
    total_uncompressed_bytes: int = 64 * 1024 * 1024
    xml_part_bytes: int = 16 * 1024 * 1024
    other_part_bytes: int = 32 * 1024 * 1024
    entries: int = 1000
    worksheet_cells: int = 50000
    total_cells: int = 100000
    xml_elements: int = 300000


LIMITS = XlsxLimits()


class XlsxSafetyError(ValueError):
    """预检失败时只暴露稳定错误码，不回显 ZIP 内的任意正文。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _central_directory_count(path: Path, limits: XlsxLimits) -> None:
    """在 zipfile 建立大量 ZipInfo 对象前读取末尾 EOCD 的条目计数。"""

    with path.open("rb") as source:
        source.seek(max(path.stat().st_size - 65557, 0))
        trailer = source.read(65557)
    position = trailer.rfind(b"PK\x05\x06")
    if position < 0 or len(trailer) - position < 22:
        raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel ZIP 目录缺失或损坏。")
    _, disk, directory_disk, disk_entries, total_entries, directory_size, _, _ = struct.unpack_from(
        "<4s4H2LH", trailer, position
    )
    if disk or directory_disk or disk_entries != total_entries or total_entries == 0:
        raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "不支持多卷或空 ZIP 工作簿。")
    if total_entries > limits.entries or total_entries == 65535 or directory_size > 4 * 1024 * 1024:
        raise XlsxSafetyError("XLSX_PACKAGE_TOO_COMPLEX", "Excel ZIP 条目或目录超过安全上限。")


def inspect_xlsx(path: Path, *, limits: XlsxLimits = LIMITS) -> None:
    """按块核验真实解压量、CRC 和必要部件，拒绝宏及异常工作表。"""

    if not path.is_file() or path.stat().st_size > limits.archive_bytes:
        raise XlsxSafetyError("XLSX_ARCHIVE_TOO_LARGE", "Excel 文件超过此功能的 32 MiB 上限。")
    _central_directory_count(path, limits)
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > limits.entries:
                raise XlsxSafetyError("XLSX_PACKAGE_TOO_COMPLEX", "Excel ZIP 条目超过安全上限。")
            names = [item.filename for item in infos]
            if len({name.casefold() for name in names}) != len(names):
                raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel ZIP 包含重复部件。")
            required = {"[Content_Types].xml", "xl/workbook.xml", "xl/_rels/workbook.xml.rels"}
            if not required.issubset(names):
                raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel 缺少必需的 OOXML 部件。")
            total = 0
            total_cells = 0
            total_xml_elements = 0
            for info in infos:
                name = info.filename
                if (not name or name.startswith("/") or "\\" in name or "\x00" in name
                        or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))):
                    raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel 包含不安全的部件路径。")
                lowered = name.casefold()
                if ("vbaproject" in lowered or lowered.startswith(("xl/embeddings/", "xl/activex/"))
                        or lowered.endswith((".vbs", ".exe", ".dll", ".js", ".ps1"))
                        or info.flag_bits & 1):
                    raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel 包含宏、可执行嵌入件或加密部件。")
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel 包含不支持的压缩格式。")
                is_xml = name.lower().endswith((".xml", ".rels"))
                part_limit = limits.xml_part_bytes if is_xml else limits.other_part_bytes
                if info.file_size > part_limit or total + info.file_size > limits.total_uncompressed_bytes:
                    raise XlsxSafetyError("XLSX_UNCOMPRESSED_TOO_LARGE", "Excel 解压体积超过安全上限。")
                actual = 0
                prefix = b""
                with archive.open(info) as source:
                    while chunk := source.read(1024 * 1024):
                        actual += len(chunk)
                        total += len(chunk)
                        if actual > part_limit or total > limits.total_uncompressed_bytes:
                            raise XlsxSafetyError("XLSX_UNCOMPRESSED_TOO_LARGE", "Excel 实际解压体积超过安全上限。")
                        if is_xml:
                            sample = (prefix + chunk).lower()
                            if b"<!doctype" in sample or b"<!entity" in sample:
                                raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel XML 包含禁止的实体声明。")
                            prefix = sample[-16:]
                if actual != info.file_size:
                    raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel ZIP 声明与实际解压量不符。")
                if is_xml:
                    with archive.open(info) as source:
                        cells = 0
                        depth = 0
                        for event, element in ET.iterparse(source, events=("start", "end")):
                            if event == "start":
                                depth += 1
                                total_xml_elements += 1
                                if depth > 64 or total_xml_elements > limits.xml_elements:
                                    raise XlsxSafetyError("XLSX_PACKAGE_TOO_COMPLEX", "Excel XML 结构超过安全上限。")
                                if name.startswith("xl/worksheets/") and name.endswith(".xml") and element.tag.rsplit("}", 1)[-1] == "c":
                                    cells += 1
                                    total_cells += 1
                                    if cells > limits.worksheet_cells or total_cells > limits.total_cells:
                                        raise XlsxSafetyError("XLSX_SHEET_TOO_COMPLEX", "Excel 工作表单元格超过安全上限。")
                            else:
                                depth -= 1
                                element.clear()
            content_types = archive.read("[Content_Types].xml").lower()
            if b"macroenabled" in content_types:
                raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "此入口不接受含宏的 Excel。")
    except (OSError, zipfile.BadZipFile, RuntimeError, ET.ParseError) as exc:
        raise XlsxSafetyError("XLSX_PACKAGE_INVALID", "Excel ZIP 或 XML 无法安全读取。") from exc
