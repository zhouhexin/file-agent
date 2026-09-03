"""OCR 文件自动重命名的保守质量门禁。"""

from __future__ import annotations

from pathlib import Path
from typing import Any


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".bmp", ".dib", ".tif", ".tiff", ".gif"}
_TITLE_LABELS = {"title", "section_header"}


def assess_ocr_rename_quality(
    *,
    filename: str,
    pages: list[Any],
    elements: list[Any],
    minimum_quality_score: float,
) -> dict[str, Any] | None:
    """低质量 OCR 或缺少标题层级时返回保留原名的审计原因。"""

    suffix = Path(filename).suffix.lower()
    page_metadata = [_metadata(page) for page in pages]
    ocr_metadata = [
        metadata
        for metadata in page_metadata
        if metadata.get("ocr_fallback")
        or metadata.get("ocr_source")
        or metadata.get("ocr_quality_score") is not None
    ]
    if suffix not in _IMAGE_SUFFIXES and not ocr_metadata:
        return None

    scores = [
        float(metadata["ocr_quality_score"])
        for metadata in ocr_metadata
        if metadata.get("ocr_quality_score") is not None
    ]
    if not scores or min(scores) < minimum_quality_score:
        return {
            "reason_code": "OCR_QUALITY_LOW",
            "minimum_quality_score": minimum_quality_score,
            "observed_quality_scores": scores,
        }

    if not any(_is_title_element(element) for element in elements):
        return {
            "reason_code": "OCR_TITLE_STRUCTURE_UNVERIFIED",
            "minimum_quality_score": minimum_quality_score,
            "observed_quality_scores": scores,
        }
    return None


def _metadata(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item.get("metadata") or item.get("metadata_json") or {})
    return dict(getattr(item, "metadata_json", {}) or {})


def _is_title_element(item: Any) -> bool:
    if isinstance(item, dict):
        label = str(item.get("label") or "").lower()
        text = str(item.get("text") or item.get("text_content") or "").strip()
        page_number = item.get("page_number")
    else:
        label = str(getattr(item, "label", "") or "").lower()
        text = str(getattr(item, "text_content", "") or "").strip()
        page_number = getattr(item, "page_number", None)
    return label in _TITLE_LABELS and bool(text) and page_number in {None, 1}
