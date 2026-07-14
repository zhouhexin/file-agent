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
_COMPACT_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)"
)
_DELIMITED_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})[-/.年]"
    r"(?P<month>0?[1-9]|1[0-2])[-/.月]"
    r"(?P<day>0?[1-9]|[12]\d|3[01])日?(?!\d)"
)
_FULL_DATE_PATTERN = re.compile(
    r"(?P<year>(?:19|20)\d{2})\s*年\s*"
    r"(?P<month>\d{1,2})\s*月\s*"
    r"(?P<day>\d{1,2})\s*日"
)
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
_TABLE_TITLE_TERMS = ("表", "清单", "台账")
_SPREADSHEET_SUFFIXES = {".xls", ".xlsx", ".xlsm", ".csv", ".tsv"}


class FilenameMetadataExtractor:
    """使用确定性规则生成可审计命名字段。"""

    def extract(
        self,
        *,
        filename: str,
        pages: list[Any],
        elements: list[Any] | None = None,
    ) -> FilenameMetadataResult:
        """从按页正文和原文件名提取命名字段。"""

        normalized_pages = [_normalize_page(page) for page in pages]
        normalized_elements = [_normalize_element(element) for element in elements or []]
        if normalized_elements:
            document_number, document_year = _extract_structured_document_number(normalized_elements)
        else:
            document_number, document_year = _extract_document_number(normalized_pages)
        document_date = (
            _extract_structured_issue_date(normalized_elements)
            or _extract_issue_date(normalized_pages)
            or _extract_filename_date(filename)
        )
        year = (
            _year_from_document_date(document_date)
            or document_year
            or _extract_year(filename=filename, pages=normalized_pages)
        )
        title = (
            _extract_structured_title(
                elements=normalized_elements,
                document_number=document_number.value,
                year=year.value,
            )
            or _extract_title(
                filename=filename,
                pages=normalized_pages,
                document_number=document_number.value,
                year=year.value,
            )
        )
        return FilenameMetadataResult(
            document_date=document_date,
            year=year,
            document_number=document_number,
            title=title,
        )


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


def _normalize_element(element: Any) -> dict[str, Any]:
    """兼容字典和 DocumentElement ORM 对象。"""

    if isinstance(element, dict):
        return {
            "element_index": element.get("element_index"),
            "label": str(element.get("label") or "text").lower(),
            "text": str(element.get("text") or element.get("text_content") or ""),
            "page_number": element.get("page_number"),
            "bbox": element.get("bbox") or element.get("bbox_json") or {},
            "content_layer": str(element.get("content_layer") or "body").lower(),
        }
    return {
        "element_index": getattr(element, "element_index", None),
        "label": str(getattr(element, "label", "text") or "text").lower(),
        "text": str(getattr(element, "text_content", "") or ""),
        "page_number": getattr(element, "page_number", None),
        "bbox": getattr(element, "bbox_json", {}) or {},
        "content_layer": str(getattr(element, "content_layer", "body") or "body").lower(),
    }


def _extract_structured_document_number(
    elements: list[dict[str, Any]],
) -> tuple[RenameFieldResult, RenameFieldResult | None]:
    """只从首页正文层的独立非段落元素提取当前文件文号。"""

    candidates: list[tuple[str, str, dict[str, Any], str]] = []
    for element in elements:
        if int(element.get("page_number") or 1) != 1 or not _is_body_element(element):
            continue
        if element["label"] in {"paragraph", "reference", "page_header", "page_footer"}:
            continue
        for raw_line in [line.strip() for line in element["text"].splitlines() if line.strip()]:
            candidate_line = raw_line.strip(" （()）")
            for pattern in _DOCUMENT_NUMBER_PATTERNS:
                match = pattern.fullmatch(candidate_line)
                if match is None:
                    continue
                if "prefix" in match.groupdict():
                    value = f"{match.group('prefix')}〔{match.group('year')}〕{match.group('number')}号"
                else:
                    value = f"{match.group('year')}年第{match.group('number')}号"
                candidates.append((value, match.group("year"), element, raw_line))
    unique_values = list(dict.fromkeys(value for value, _, _, _ in candidates))
    if not unique_values:
        return _missing_field(), None
    if len(unique_values) > 1:
        return (
            RenameFieldResult(
                status=RenameFieldStatus.AMBIGUOUS,
                source="document_structure",
                confidence=0,
                alternatives=unique_values,
            ),
            None,
        )
    value, year, element, quote = candidates[0]
    evidence = _element_evidence(element, quote=quote, source="document_structure_header")
    return (
        RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="document_structure_header",
            confidence=0.99,
            evidence_items=[evidence],
        ),
        RenameFieldResult(
            value=year,
            status=RenameFieldStatus.RESOLVED,
            source="document_number",
            confidence=0.99,
            evidence_items=[evidence],
        ),
    )


def _extract_structured_issue_date(elements: list[dict[str, Any]]) -> RenameFieldResult | None:
    """从文档尾部正文元素提取完整落款日期。"""

    body_elements = [element for element in elements if _is_body_element(element)]
    if not body_elements:
        return None
    last_page = max(int(element.get("page_number") or 1) for element in body_elements)
    candidates: list[tuple[int, dict[str, Any], str, str]] = []
    for element in body_elements:
        page_number = int(element.get("page_number") or 1)
        if page_number < max(1, last_page - 1):
            continue
        for match in _FULL_DATE_PATTERN.finditer(element["text"]):
            candidates.append(
                (
                    int(element.get("element_index") or 0),
                    element,
                    _normalized_date(match),
                    match.group(0),
                )
            )
    if not candidates:
        return None
    _, element, document_date, quote = max(candidates, key=lambda item: item[0])
    return RenameFieldResult(
        value=document_date,
        status=RenameFieldStatus.RESOLVED,
        source="document_structure_date",
        confidence=0.99,
        evidence_items=[_element_evidence(element, quote=quote, source="document_structure_date")],
    )


def _extract_structured_title(
    *,
    elements: list[dict[str, Any]],
    document_number: str | None,
    year: str | None,
) -> RenameFieldResult | None:
    """优先从 Docling title/section_header 元素提取完整标题。"""

    candidates: list[tuple[int, str, dict[str, Any], str]] = []
    title_elements = [
        element
        for element in elements
        if _is_body_element(element)
        and int(element.get("page_number") or 1) <= 3
        and element["label"] in {"title", "section_header"}
    ]
    for index, element in enumerate(title_elements):
        variants = [(element["text"], element["text"], 1)]
        for count in (2, 3):
            segment = title_elements[index : index + count]
            if len(segment) != count or len({item.get("page_number") for item in segment}) != 1:
                continue
            variants.append(("".join(item["text"] for item in segment), "\n".join(item["text"] for item in segment), count))
        for value, quote, count in variants:
            cleaned = _clean_title(value, document_number=document_number, year=year)
            if not _is_title_candidate(cleaned, raw_line=value):
                continue
            label_score = 100 if element["label"] == "title" else 70
            score = label_score + _title_candidate_score(cleaned) + count
            candidates.append((score, cleaned, element, quote))
    if not candidates:
        return None
    _, value, element, quote = max(candidates, key=lambda item: item[0])
    return RenameFieldResult(
        value=value,
        status=RenameFieldStatus.RESOLVED,
        source="document_structure",
        confidence=0.99,
        evidence_items=[_element_evidence(element, quote=quote, source="document_structure")],
    )


def _is_body_element(element: dict[str, Any]) -> bool:
    """排除页眉、页脚和 furniture 内容层。"""

    return element.get("content_layer") not in {"furniture"} and element.get("label") not in {
        "page_header",
        "page_footer",
    }


def _extract_document_number(
    pages: list[dict[str, Any]],
) -> tuple[RenameFieldResult, RenameFieldResult | None]:
    """只从第一页前部的独立文号行提取当前文件文号。"""

    candidates: list[tuple[str, str, dict[str, Any]]] = []
    for page in pages[:1]:
        lines = [line.strip() for line in page["text"].splitlines()[:25] if line.strip()]
        for raw_line in lines:
            candidate_line = raw_line.strip(" （()）")
            if not candidate_line:
                continue
            for pattern in _DOCUMENT_NUMBER_PATTERNS:
                match = pattern.fullmatch(candidate_line)
                if match is None:
                    continue
                if "prefix" in match.groupdict():
                    value = f"{match.group('prefix')}〔{match.group('year')}〕{match.group('number')}号"
                else:
                    value = f"{match.group('year')}年第{match.group('number')}号"
                candidates.append((value, match.group("year"), {**page, "quote": raw_line}))
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
    evidence = _evidence(
        first_page,
        quote=str(first_page.get("quote") or first_value),
        source="document_header",
    )
    document_number = RenameFieldResult(
        value=first_value,
        status=RenameFieldStatus.RESOLVED,
        source="document_header",
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


def _extract_issue_date(pages: list[dict[str, Any]]) -> RenameFieldResult | None:
    """优先从文档末尾的完整落款日期提取发文年份。"""

    standalone_candidates: list[tuple[str, dict[str, Any], str]] = []
    candidates: list[tuple[str, dict[str, Any], str]] = []
    for page in pages[-2:]:
        lines = [line.strip() for line in page["text"].splitlines() if line.strip()]
        # 落款日期后可能附带长表格，因此独立日期行需要扫描完整尾页。
        for raw_line in lines:
            if not _DATE_ONLY_PATTERN.fullmatch(raw_line):
                continue
            match = _FULL_DATE_PATTERN.search(raw_line)
            if match:
                standalone_candidates.append((_normalized_date(match), page, match.group(0)))
        for raw_line in lines[-40:]:
            match = _FULL_DATE_PATTERN.search(raw_line)
            if match:
                candidates.append((_normalized_date(match), page, match.group(0)))
    selected_candidates = standalone_candidates or candidates
    if not selected_candidates:
        return None
    document_date, page, quote = selected_candidates[-1]
    return RenameFieldResult(
        value=document_date,
        status=RenameFieldStatus.RESOLVED,
        source="document_date",
        confidence=0.98,
        evidence_items=[_evidence(page, quote=quote, source="document_date")],
    )


def _extract_filename_date(filename: str) -> RenameFieldResult:
    """从文件名提取完整日期，多个日期并存时采用最后出现的版本日期。"""

    stem = Path(filename).stem
    candidates: list[tuple[int, str, str]] = []
    for pattern in (_DELIMITED_DATE_PATTERN, _COMPACT_DATE_PATTERN):
        for match in pattern.finditer(stem):
            value = _normalized_date(match) if "month" in match.groupdict() else match.group(0)
            candidates.append((match.start(), value, match.group(0)))
    if not candidates:
        return _missing_field()
    _, value, quote = max(candidates, key=lambda item: item[0])
    return RenameFieldResult(
        value=value,
        status=RenameFieldStatus.RESOLVED,
        source="filename",
        confidence=0.76,
        evidence_items=[RenameEvidenceItem(quote=quote, source="filename")],
    )


def _year_from_document_date(document_date: RenameFieldResult) -> RenameFieldResult | None:
    """从已经解析并保留证据的完整日期派生年份字段。"""

    if document_date.status != RenameFieldStatus.RESOLVED or not document_date.value:
        return None
    return document_date.model_copy(update={"value": document_date.value[:4]})


def _normalized_date(match: re.Match[str]) -> str:
    """将带分隔符的日期统一为 YYYYMMDD。"""

    return f"{match.group('year')}{int(match.group('month')):02d}{int(match.group('day')):02d}"


def _extract_year(
    *,
    filename: str,
    pages: list[dict[str, Any]],
) -> RenameFieldResult:
    """从文件名或首页标题区域回退提取年份，不扫描正文引用。"""

    filename_stem = Path(filename).stem
    compact_date_match = _COMPACT_DATE_PATTERN.search(filename_stem)
    if compact_date_match:
        value = compact_date_match.group("year")
        return RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="filename",
            confidence=0.76,
            evidence_items=[RenameEvidenceItem(quote=compact_date_match.group(0), source="filename")],
        )

    filename_match = _YEAR_PATTERN.search(filename_stem)
    if filename_match:
        value = filename_match.group(1)
        return RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="filename",
            confidence=0.72,
            evidence_items=[RenameEvidenceItem(quote=value, source="filename")],
        )

    first_page = pages[0] if pages else {}
    header_lines = [
        line.strip()
        for line in str(first_page.get("text") or "").splitlines()[:6]
        if line.strip()
    ]
    for raw_line in header_lines:
        match = _YEAR_PATTERN.search(raw_line)
        if not match or len(raw_line) > 80:
            continue
        value = match.group(1)
        return RenameFieldResult(
            value=value,
            status=RenameFieldStatus.RESOLVED,
            source="document_header",
            confidence=0.82,
            evidence_items=[_evidence(first_page, quote=raw_line, source="document_header")],
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
        raw_lines = [
            line.strip()
            for line in page["text"].splitlines()[:50]
            if line.strip() and not _is_page_number_marker(line)
        ]
        for line_index, raw_line in enumerate(raw_lines):
            variants = [(raw_line, raw_line, 1)]
            # Word/PDF 转文本后标题经常被拆成两至三行，需要作为一个标题候选共同评分。
            for line_count in (2, 3):
                segment = raw_lines[line_index : line_index + line_count]
                if len(segment) == line_count:
                    variants.append(("".join(segment), "\n".join(segment), line_count))
            for raw_value, evidence_quote, line_count in variants:
                line = _clean_title(raw_value, document_number=document_number, year=year)
                if not _is_title_candidate(line, raw_line=raw_value):
                    continue
                score = _title_candidate_score(line)
                if line_count > 1 and line.endswith(_DOCUMENT_TYPE_TERMS + _TABLE_TITLE_TERMS):
                    score += 1
                candidates.append((score, -position, line, page, evidence_quote))
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

    filename_stem = Path(filename).stem
    if Path(filename).suffix.lower() in _SPREADSHEET_SUFFIXES:
        filename_stem = _spreadsheet_filename_title(filename_stem)
    filename_title = _clean_title(filename_stem, document_number=document_number, year=year)
    if filename_title:
        return RenameFieldResult(
            value=filename_title,
            status=RenameFieldStatus.RESOLVED,
            source="filename",
            confidence=0.65,
            evidence_items=[RenameEvidenceItem(quote=Path(filename).stem, source="filename")],
        )
    return _missing_field()


def _spreadsheet_filename_title(stem: str) -> str:
    """清理表格文件名中的附件标记、处理日期和提交单位后缀。"""

    cleaned = re.sub(r"^\s*附件(?:\s+|[_-]+)", "", stem, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"[（(]\s*(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?\s*[）)]",
        "",
        cleaned,
    )
    cleaned = re.sub(
        r"[-_—]\s*[^-_—]{1,40}?(?:19|20)\d{6}\s*$",
        "",
        cleaned,
    )
    cleaned = re.sub(r"new(?=[-_—]|$)", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("摸底统计表", "统计表")
    return re.sub(r"\s+", " ", cleaned).strip(" 	-_—:：，,。")


def _title_candidate_score(value: str) -> int:
    """为单行或合并后的标题候选计算确定性分数。"""

    score = 4 if value.endswith(_DOCUMENT_TYPE_TERMS) else 1
    if any(term in value for term in _DOCUMENT_TYPE_TERMS):
        score += 2
    # “表”只在标题结尾时作为表格标题信号，避免附表正文抢占主标题。
    if value.endswith(_TABLE_TITLE_TERMS):
        score += 3
    if 6 <= len(value) <= 60:
        score += 2
    return score


def _is_page_number_marker(value: str) -> bool:
    """过滤 PDF 抽取产生的“-1-”等独立页码行。"""

    return bool(re.fullmatch(r"\s*[-—]?\s*\d{1,4}\s*[-—]?\s*", value))


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
    if "\t" in raw_line:
        return False
    if _DATE_ONLY_PATTERN.fullmatch(raw_line.strip()):
        return False
    if any(pattern.search(raw_line) for pattern in _DOCUMENT_NUMBER_PATTERNS):
        return False
    if value.startswith(("各", "现将", "根据", "为进一步", "经研究")) and not value.endswith(
        _DOCUMENT_TYPE_TERMS + _TABLE_TITLE_TERMS
    ):
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


def _element_evidence(element: dict[str, Any], *, quote: str, source: str) -> RenameEvidenceItem:
    """构造包含结构标签和位置的可定位证据。"""

    return RenameEvidenceItem(
        page_number=element.get("page_number"),
        quote=quote,
        source=source,
        element_index=element.get("element_index"),
        element_label=element.get("label"),
        bbox=element.get("bbox") or None,
    )
