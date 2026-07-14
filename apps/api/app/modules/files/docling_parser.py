"""Docling 本地结构化文档解析适配器。"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


def try_parse_with_docling(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
    ocr_enabled: bool = False,
) -> dict[str, Any]:
    """调用本地 Docling，任何依赖或转换异常都返回可回退的结构化错误。"""

    del filename, content_type
    try:
        converter = _build_converter(ocr_enabled)
    except (ImportError, ModuleNotFoundError) as exc:
        return _failure("DOCLING_NOT_AVAILABLE", f"Docling 未安装或依赖不完整：{exc}")
    except Exception as exc:
        return _failure("DOCLING_INITIALIZATION_FAILED", f"Docling 初始化失败：{exc}")

    try:
        conversion = converter.convert(file_path)
        document = conversion.document
        elements = _extract_elements(document)
        pages = _build_pages(document=document, elements=elements)
    except Exception as exc:
        return _failure("DOCLING_CONVERSION_FAILED", f"Docling 解析失败：{exc}")

    if not any(str(page.get("text") or "").strip() for page in pages):
        return _failure("DOCLING_EMPTY_CONTENT", "Docling 未提取到可用正文，已回退现有解析器。")
    return {
        "ok": True,
        "extractor": f"docling@{docling_runtime_version()}",
        "pages": pages,
        "elements": elements,
        "warnings": [],
    }


@lru_cache(maxsize=2)
def _build_converter(ocr_enabled: bool) -> Any:
    """构造并复用重量级转换器，默认关闭 Docling OCR 避免重复识别。"""

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    pdf_options = PdfPipelineOptions()
    pdf_options.do_ocr = ocr_enabled
    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_options),
        }
    )


def _extract_elements(document: Any) -> list[dict[str, Any]]:
    """把 DoclingDocument 元素转换为项目稳定结构。"""

    elements: list[dict[str, Any]] = []
    for element_index, yielded in enumerate(document.iterate_items()):
        item, level = yielded if isinstance(yielded, tuple) else (yielded, None)
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        provenance = list(getattr(item, "prov", None) or [])
        first_provenance = provenance[0] if provenance else None
        page_number = getattr(first_provenance, "page_no", None)
        bbox = _model_value(getattr(first_provenance, "bbox", None))
        elements.append(
            {
                "element_index": element_index,
                "label": _enum_value(getattr(item, "label", "text")),
                "text": text,
                "page_number": int(page_number) if page_number is not None else None,
                "bbox": bbox if isinstance(bbox, dict) else None,
                "content_layer": _enum_value(getattr(item, "content_layer", "body")),
                "parent_ref": _reference_value(getattr(item, "parent", None)),
                "metadata": {"hierarchy_level": level},
            }
        )
    return elements


def _build_pages(*, document: Any, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按页聚合结构化元素；无页码的 DOCX 统一作为第一页保存。"""

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for element in elements:
        grouped[int(element.get("page_number") or 1)].append(element)
    pages = [
        {
            "page_number": page_number,
            "sheet_name": None,
            "text": "\n".join(item["text"] for item in page_elements),
            "metadata": {
                "structured": True,
                "element_count": len(page_elements),
                "parser": "docling",
            },
        }
        for page_number, page_elements in sorted(grouped.items())
    ]
    if pages:
        return pages
    export_to_text = getattr(document, "export_to_text", None)
    text = str(export_to_text() if callable(export_to_text) else "").strip()
    if not text:
        return []
    return [
        {
            "page_number": 1,
            "sheet_name": None,
            "text": text,
            "metadata": {"structured": True, "element_count": 0, "parser": "docling"},
        }
    ]


def _enum_value(value: Any) -> str:
    """兼容枚举和字符串并统一小写。"""

    return str(getattr(value, "value", value) or "").lower()


def _model_value(value: Any) -> Any:
    """把 Pydantic 模型转换为普通 JSON 数据。"""

    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return value


def _reference_value(value: Any) -> str | None:
    """读取 Docling 引用对象中的稳定 cref。"""

    if value is None:
        return None
    return str(getattr(value, "cref", None) or getattr(value, "ref", None) or value)


def docling_runtime_version() -> str:
    """返回解析器版本，便于结果审计和后续复用判断。"""

    try:
        return version("docling")
    except PackageNotFoundError:
        return "unknown"


def _failure(code: str, message: str) -> dict[str, Any]:
    """构造允许调用方继续回退的解析结果。"""

    return {
        "ok": False,
        "error": {"code": code, "message": message},
        "pages": [],
        "elements": [],
    }
