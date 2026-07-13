"""从文件正文和原名称提取年份、文号和标题。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.modules.file_rename.schemas import (
    FilenameMetadataResult,
    RenameEvidenceItem,
    RenameFieldResult,
    RenameFieldStatus,
)


_DOCUMENT_NUMBER_PATTERNS = [
    re.compile(
        r"(?P<prefix>[\u4e00-\u9fffA-Za-z]{1,20}?)\s*[〔\[（(](?P<year>(?:19|20)\d{2})[〕\]）)]\s*(?P<number>\d{1,6})\s*号"
    ),
    re.compile(r"(?P<year>(?:19|20)\d{2})\s*年\s*第\s*(?P<number>\d{1,6})\s*号"),
]
_YEAR_PATTERN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?:年)?(?!\d)")
_DATE_ONLY_PATTERN = re.compile(r"^\s*(?:19|20)\d{2}年(?:\d{1,2}月(?:\d{1,2}日)?)?\s*$")
_DOCUMENT_TYPE_TERMS = (
    "通知",
    "通报",
    "公告",
    "报告",
    "总结",
    "意见",
    "办法",
    "方案",
    "决定",
    "请示",
    "批复",
    "函",
    "纪要",
    "制度",
    "规定",
    "细则",
    "材料",
)


class FilenameMetadataExtractor:
    """使用确定性规则生成可审计命名字段。"""

    def extract(self, *, filename: str, pages: list[Any]) -> FilenameMetadataResult:
        """从按页正文和原文件名提取命名字段。"""

        normalized_pages = [_normalize_page(page) for page in pages]
        full_text = "\n".join(item["text"] for item in normalized_pages if item["text"])
        document_number, document_year = _extract_document_number(full_text, normalized_pages)
        year = document_year or _extract_year(full_text=full_text, filename=filename, pages=normalized_pages)
        title = _extract_title(
            filename=filename,
            pages=normalized_pages,
            document_number=document_number.value,
            year=year.value,
        )
        return FilenameMetadataResult(year=year, document_number=document_number, title=title)


def _normalize_page(page: Any) -> dict[str, Any]:
    """兼容字典和 DocumentPage ORM 对象。"""

    if isinstance(page, dict):
        return {
            "page_number": page.get("page_number"),
            "sheet_name": page.get("sheet_name"),
            "text": str(page.get("text") or page.get("text_content") or ""),
        }
    return {
        "page_number": getattr(page, "page_number", None),
        "sheet_name": getattr(page, "sheet_name", None),
        "text": str(getattr(page, "text_content", "") or ""),
    }


def _extract_document_number(
    full_text: str,
    pages: list[dict[str, Any]],
) -> tuple[RenameFieldResult, RenameFieldResult | None]:
    """提取完整文号，并同步生成文号年份。"""

    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for page in pages:
        for pattern in _DOCUMENT_NUMBER_PATTERNS:
            for match in pattern.finditer(page["text"]):
                if "prefix" in match.groupdict():
                    value = f"{match.group('prefix')}〔{match.group('year')}〕{match.group('number')}号"
                else:
                    value = f"{match.group('year')}年第{match.group('number')}号"
                candidates.append((value, match.group("year"), page))
    unique_values = list(dict.fromkeys(value for value, _, _ in candidates))
    if not unique_values:
        return _missing_field(), None
    first_value, first_year, first_page = candidates[0]
    if len(unique_values) > 1:
        return (
            RenameFieldResult(
                value=None,
                status=RenameFieldStatus.AMBIGUOUS,
                source="document_pages",
                confidence=0,
                alternatives=unique_values,
            ),
            None,
        )
    evidence = _evidence(first_page, quote=first_value, source="document_pages")
    document_number = RenameFieldResult(
        value=first_value,
        status=RenameFieldStatus.RESOLVED,
        source="document_pages",
        confidence=0.98,
        evidence_items=[evidence],
    )
    year = RenameFieldResult(
        value=first_year,
        status=RenameFieldStatus.RESOLVED,
        source="document_number",
        confidence=0.98,
        evidence_items=[evidence],
    )
    return document_number, year


def _extract_year(
    *,
    full_text: str,
    filename: str,
    pages: list[dict[str, Any]],
) -> RenameFieldResult:
    """从正文优先提取年份，文件名只作为回退。"""

    text_candidates = list(dict.fromkeys(_YEAR_PATTERN.findall(full_text)))
    if text_candidates:
        value = text_candidates[0]
        page = next((item for item in pages if value in item["text"]), pages[0] if pages else {})
        return RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="document_pages",
            confidence=0.9 if len(text_candidates) == 1 else 0.82,
            evidence_items=[_evidence(page, quote=value, source="document_pages")],
            alternatives=text_candidates[1:],
        )
    filename_match = _YEAR_PATTERN.search(Path(filename).stem)
    if filename_match:
        value = filename_match.group(1)
        return RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="filename",
            confidence=0.72,
            evidence_items=[RenameEvidenceItem(quote=value, source="filename")],
        )
    return _missing_field()


def _extract_title(
    *,
    filename: str,
    pages: list[dict[str, Any]],
    document_number: str | None,
    year: str | None,
) -> RenameFieldResult:
    """优先选择正文前部的公文标题，最后回退原文件名。"""

    candidates: list[tuple[int, int, str, dict[str, Any], str]] = []
    position = 0
    for page in pages[:3]:
        for raw_line in page["text"].splitlines()[:50]:
            line = _clean_title(raw_line, document_number=document_number, year=year)
            if not _is_title_candidate(line, raw_line=raw_line):
                position += 1
                continue
            score = 4 if line.endswith(_DOCUMENT_TYPE_TERMS) else 1
            if any(term in line for term in _DOCUMENT_TYPE_TERMS):
                score += 2
            if 6 <= len(line) <= 60:
                score += 2
            candidates.append((score, -position, line, page, raw_line.strip()))
            position += 1
    if candidates:
        _, _, value, page, quote = max(candidates, key=lambda item: (item[0], item[1]))
        return RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="document_pages",
            confidence=0.92,
            evidence_items=[_evidence(page, quote=quote, source="document_pages")],
        )

    filename_title = _clean_title(Path(filename).stem, document_number=document_number, year=year)
    if filename_title:
        return RenameFieldResult(
            value=filename_title,
            status=RenameFieldStatus.RESOLVED,
            source="filename",
            confidence=0.65,
            evidence_items=[RenameEvidenceItem(quote=Path(filename).stem, source="filename")],
        )
    return _missing_field()


def _clean_title(value: str, *, document_number: str | None, year: str | None) -> str:
    """从标题候选中移除已单独表达的年份和文号。"""

    cleaned = re.sub(r"\s+", " ", value).strip(" \t-_—:：，,。")
    if document_number:
        cleaned = cleaned.replace(document_number, "")
    if year:
        cleaned = re.sub(rf"^{re.escape(year)}\s*年?\s*", "", cleaned)
    cleaned = re.sub(r"^[〔\[（(]?\d+[〕\]）)]?\s*号?\s*", "", cleaned)
    return cleaned.strip(" \t-_—:：，,。")


def _is_title_candidate(value: str, *, raw_line: str) -> bool:
    """过滤日期、文号和明显正文句。"""

    if len(value) < 4 or len(value) > 120:
        return False
    if _DATE_ONLY_PATTERN.fullmatch(raw_line.strip()):
        return False
    if any(pattern.search(raw_line) for pattern in _DOCUMENT_NUMBER_PATTERNS):
        return False
    if value.startswith(("各", "现将", "根据", "为进一步", "经研究")) and not value.endswith(_DOCUMENT_TYPE_TERMS):
        return False
    return True


def _missing_field() -> RenameFieldResult:
    """构造缺失字段结果。"""

    return RenameFieldResult(status=RenameFieldStatus.MISSING, confidence=0)


def _evidence(page: dict[str, Any], *, quote: str, source: str) -> RenameEvidenceItem:
    """构造可定位证据。"""

    return RenameEvidenceItem(
        page_number=page.get("page_number"),
        sheet_name=page.get("sheet_name"),
        quote=quote,
        source=source,
    )

