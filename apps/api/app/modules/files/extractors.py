"""上传文件内容解析器。"""

from __future__ import annotations

import csv
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List


def extract_document_text(*, file_path: Path, filename: str, content_type: str) -> Dict[str, Any]:
    """按文件类型解析文本内容，并返回统一结构。"""

    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"} or content_type.startswith("text/"):
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        return _completed("plain-text", [{"page_number": 1, "sheet_name": None, "text": text, "metadata": {}}])
    if suffix == ".csv":
        text = _extract_csv_text(file_path)
        return _completed("csv", [{"page_number": 1, "sheet_name": None, "text": text, "metadata": {}}])
    if suffix in {".xlsx", ".xls"}:
        return _extract_excel_text(file_path)
    if suffix == ".pdf" or content_type == "application/pdf":
        return _extract_pdf_text(file_path)
    if content_type.startswith("image/") or suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        return _extract_image_text(file_path)
    return _failed("unsupported", "UNSUPPORTED_FILE_TYPE", f"暂不支持解析该文件类型：{filename}")


def _extract_csv_text(file_path: Path) -> str:
    """使用标准库读取 CSV 并转成行文本。"""

    raw_text = file_path.read_text(encoding="utf-8", errors="ignore")
    rows = csv.reader(StringIO(raw_text))
    return "\n".join(["\t".join(row) for row in rows])


def _extract_excel_text(file_path: Path) -> Dict[str, Any]:
    """使用 openpyxl 读取 Excel 文本。"""

    try:
        import openpyxl
    except ImportError:
        return _failed("excel", "EXCEL_EXTRACTOR_NOT_AVAILABLE", "缺少 openpyxl，无法解析 Excel 文件。")

    workbook = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    pages: List[Dict[str, Any]] = []
    for sheet_index, sheet in enumerate(workbook.worksheets, start=1):
        lines = []
        for row in sheet.iter_rows(values_only=True):
            values = ["" if value is None else str(value) for value in row]
            if any(values):
                lines.append("\t".join(values))
        pages.append(
            {
                "page_number": sheet_index,
                "sheet_name": sheet.title,
                "text": "\n".join(lines),
                "metadata": {"sheet_index": sheet_index},
            }
        )
    workbook.close()
    return _completed("excel", pages)


def _extract_pdf_text(file_path: Path) -> Dict[str, Any]:
    """使用 PyMuPDF 读取 PDF 页面文本。"""

    try:
        import fitz
    except ImportError:
        return _failed("pdf", "PDF_EXTRACTOR_NOT_AVAILABLE", "缺少 PyMuPDF，无法解析 PDF 文件。")

    pages: List[Dict[str, Any]] = []
    with fitz.open(file_path) as document:
        for index, page in enumerate(document, start=1):
            pages.append(
                {
                    "page_number": index,
                    "sheet_name": None,
                    "text": page.get_text("text"),
                    "metadata": {"page_index": index - 1},
                }
            )
    return _completed("pdf", pages)


def _extract_image_text(file_path: Path) -> Dict[str, Any]:
    """使用 pytesseract 对图片做 OCR。"""

    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return _failed("ocr", "OCR_EXTRACTOR_NOT_AVAILABLE", "缺少 Pillow 或 pytesseract，无法执行图片 OCR。")

    try:
        text = pytesseract.image_to_string(Image.open(file_path), lang="chi_sim+eng")
    except Exception as exc:
        return _failed("ocr", "OCR_ENGINE_NOT_AVAILABLE", f"OCR 引擎不可用：{exc}")
    return _completed("ocr", [{"page_number": 1, "sheet_name": None, "text": text, "metadata": {}}])


def _completed(extractor: str, pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """构造解析成功结果。"""

    return {"ok": True, "status": "COMPLETED", "extractor": extractor, "pages": pages}


def _failed(extractor: str, code: str, message: str) -> Dict[str, Any]:
    """构造结构化解析失败结果。"""

    return {
        "ok": False,
        "status": "FAILED",
        "extractor": extractor,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "user_action_required": False,
        },
        "pages": [],
    }
