"""文件重命名标题候选的共享质量规则。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_TEMPORAL_NARRATIVE_PREFIX = re.compile(
    r"^(?:大约|约|上午|下午|中午|晚上|凌晨|当日|当天|次日|随后|之后|\d{1,2}\s*[时点])"
)
_LOW_INFORMATION_FILENAME_TERMS = re.compile(
    r"(?:附件|扫描件|scan|img|image|document|文档|文件|副本|copy|new)",
    flags=re.I,
)


def looks_like_body_sentence(value: str) -> bool:
    """识别正文段落、条款组合或课程要求被误选为标题的情况。"""

    text = value.strip()
    if not text:
        return False
    sentence_marks = len(re.findall(r"[。；！？]", text))
    section_marks = len(
        re.findall(r"(?:^|[\s。；])[一二三四五六七八九十百\d]+[、.]", text)
    )
    numeric_conditions = len(
        re.findall(r"(?:≥|≤|>=|<=|>|<|学分|年限|百分比|%)", text, flags=re.I)
    )
    temporal_narrative = bool(
        _TEMPORAL_NARRATIVE_PREFIX.match(text)
        and re.search(r"[，,]", text)
        and re.search(r"[。！？]$", text)
    )
    return (
        temporal_narrative
        or sentence_marks >= 2
        or section_marks >= 1
        or (sentence_marks >= 1 and numeric_conditions >= 2)
    )


def assess_narrative_filename_preservation(
    *,
    filename: str,
    pages: list[Any],
    elements: list[Any],
) -> dict[str, str] | None:
    """无显式标题且全文均为时间叙事正文时，保留有意义的原文件名。"""

    if any(_item_label(element) in {"title", "section_header"} for element in elements):
        return None
    lines = [
        line.strip()
        for page in pages[:3]
        for line in _item_text(page).splitlines()
        if line.strip()
    ]
    if len(lines) < 2 or not all(looks_like_body_sentence(line) for line in lines):
        return None
    filename_stem = _LOW_INFORMATION_FILENAME_TERMS.sub("", Path(filename).stem)
    filename_stem = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+|\d+", "", filename_stem)
    if len(filename_stem) <= 2:
        return None
    return {"reason_code": "NARRATIVE_BODY_WITHOUT_EXPLICIT_TITLE"}


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("text") or item.get("text_content") or "")
    return str(getattr(item, "text_content", "") or "")


def _item_label(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("label") or "").lower()
    return str(getattr(item, "label", "") or "").lower()
