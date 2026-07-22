"""上传文件的基础格式、宏和加密风险检查。

当前阶段不接入病毒扫描引擎；本模块只读取受控暂存文件的少量容器元数据，绝不执行宏、脚本、
外部链接或嵌入对象，也不能把结果描述为“病毒扫描通过”。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import zipfile


@dataclass(slots=True)
class BasicFileRiskAssessment:
    """可持久化的基础风险结果，不包含文件正文或本地绝对路径。"""

    status: str = "PASS"
    extension: str = ""
    mime_consistent: bool | None = None
    macro_risk: bool = False
    encrypted: bool = False
    virus_scan_status: str = "NOT_IMPLEMENTED"
    warnings: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict:
        """转换为 JSON 可持久化结构。"""

        return asdict(self)


def inspect_basic_file_risks(*, file_path: Path, filename: str, content_type: str) -> BasicFileRiskAssessment:
    """检查格式/MIME、宏和加密边界；无法确定时给出警告而不是伪造安全结论。"""

    suffix = Path(filename).suffix.lower()
    result = BasicFileRiskAssessment(extension=suffix)
    expected_mime_types = _expected_mime_types(suffix)
    normalized_mime = content_type.lower().strip()
    if normalized_mime and normalized_mime != "application/octet-stream" and expected_mime_types:
        result.mime_consistent = normalized_mime in expected_mime_types
        if result.mime_consistent is False:
            result.warnings.append(
                {"code": "MIME_EXTENSION_MISMATCH", "message": "文件扩展名与浏览器上报的内容类型不一致。"}
            )

    if suffix == ".xlsm":
        result.macro_risk = True
    elif suffix in {".docx", ".xlsx"} and _ooxml_contains_macro(file_path):
        result.macro_risk = True
    if result.macro_risk:
        result.warnings.append(
            {"code": "OFFICE_MACRO_RISK", "message": "文件可能包含宏；系统只读取内容，绝不执行宏。"}
        )

    result.encrypted = _is_encrypted_pdf(file_path) if suffix == ".pdf" else _is_encrypted_ooxml(file_path, suffix)
    if result.encrypted:
        result.status = "NEEDS_REVIEW"
        result.warnings.append(
            {"code": "ENCRYPTED_FILE", "message": "文件已加密，已保护原件但不会尝试破解或自动解析。"}
        )
    elif result.warnings:
        result.status = "WARNING"
    return result


def _expected_mime_types(suffix: str) -> set[str]:
    """返回常见浏览器可能上报的受控 MIME 集合。"""

    return {
        ".pdf": {"application/pdf"},
        ".doc": {"application/msword"},
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        ".xls": {"application/vnd.ms-excel"},
        ".xlsx": {
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.ms-excel",
        },
        ".xlsm": {"application/vnd.ms-excel.sheet.macroenabled.12", "application/vnd.ms-excel"},
        ".txt": {"text/plain"},
        ".md": {"text/markdown", "text/plain"},
        ".csv": {"text/csv", "application/csv", "text/plain"},
    }.get(suffix, set())


def _ooxml_contains_macro(file_path: Path) -> bool:
    """只检查 OOXML 包条目名，不加载或执行其中任何对象。"""

    try:
        with zipfile.ZipFile(file_path) as archive:
            return any(name.lower().endswith("vbaproject.bin") for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def _is_encrypted_ooxml(file_path: Path, suffix: str) -> bool:
    """识别伪装为 OOXML 的 OLE 加密容器；普通损坏文件仍交给解析器报告。"""

    if suffix not in {".docx", ".xlsx", ".xlsm"}:
        return False
    try:
        with file_path.open("rb") as handle:
            header = handle.read(8)
    except OSError:
        return False
    return header == bytes.fromhex("D0CF11E0A1B11AE1")


def _is_encrypted_pdf(file_path: Path) -> bool:
    """使用本地 PDF 元数据判断密码保护，不尝试口令或解密。"""

    try:
        import fitz

        document = fitz.open(file_path)
        try:
            return bool(document.needs_pass)
        finally:
            document.close()
    except Exception:
        return False
