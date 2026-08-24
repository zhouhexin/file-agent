"""结构化字段证据归属、文本支持和 bbox 校验。"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class EvidenceElement:
    """从 Persistent Store 读取的最小证据元素。"""

    id: str
    document_id: str
    extraction_run_id: str
    text: str
    page_number: int | None
    bbox: dict[str, Any]
    metadata: dict[str, Any]


def validate_field_evidence(
    *,
    raw_text: str | None,
    evidence_element_ids: Iterable[str],
    allowed_elements: dict[str, EvidenceElement],
    document_id: str,
    extraction_run_id: str,
) -> tuple[list[EvidenceElement], list[str]]:
    """仅接受属于本次文档和版面运行且能支持原值的证据元素。"""

    accepted: list[EvidenceElement] = []
    warnings: list[str] = []
    for element_id in dict.fromkeys(str(value) for value in evidence_element_ids if str(value)):
        element = allowed_elements.get(element_id)
        if element is None:
            warnings.append("EVIDENCE_ELEMENT_NOT_FOUND")
            continue
        if element.document_id != document_id or element.extraction_run_id != extraction_run_id:
            warnings.append("EVIDENCE_SCOPE_MISMATCH")
            continue
        accepted.append(element)
    if raw_text and accepted and not any(
        _text_supports_raw(element.text, raw_text) for element in accepted
    ):
        warnings.append("EVIDENCE_TEXT_MISMATCH")
    if raw_text and not accepted:
        warnings.append("EVIDENCE_REQUIRED")
    return accepted, list(dict.fromkeys(warnings))


def merge_evidence_bbox(elements: list[EvidenceElement]) -> dict[str, Any]:
    """合并同页证据矩形；跨页时使用首个元素并由 page_number 保持定位。"""

    boxes = [element.bbox for element in elements if _valid_bbox(element.bbox)]
    if not boxes:
        return {}
    page_number = elements[0].page_number
    same_page_boxes = [
        element.bbox
        for element in elements
        if element.page_number == page_number and _valid_bbox(element.bbox)
    ]
    return {
        "left": min(float(box["left"]) for box in same_page_boxes),
        "top": min(float(box["top"]) for box in same_page_boxes),
        "right": max(float(box["right"]) for box in same_page_boxes),
        "bottom": max(float(box["bottom"]) for box in same_page_boxes),
    }


def _text_supports_raw(evidence_text: str, raw_text: str) -> bool:
    """忽略常见排版空白后验证原值与证据文本的包含关系。"""

    evidence = re.sub(r"\s+", "", evidence_text or "").lower()
    raw = re.sub(r"\s+", "", raw_text or "").lower()
    return bool(raw and evidence and (raw in evidence or evidence in raw))


def _valid_bbox(value: dict[str, Any]) -> bool:
    """判断是否包含完整矩形坐标。"""

    if not isinstance(value, dict):
        return False
    coordinates = [value.get(key) for key in ("left", "top", "right", "bottom")]
    return bool(
        all(
            isinstance(item, (int, float)) and math.isfinite(float(item))
            for item in coordinates
        )
        and float(value["right"]) > float(value["left"])
        and float(value["bottom"]) > float(value["top"])
    )
