"""在调用模型前确定性分析重命名建议的差异风险。"""

from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Any

from app.modules.file_rename.schemas import FilenameMetadataResult
from app.modules.file_rename.title_quality import looks_like_body_sentence
from app.modules.file_rename.validation_schemas import RenameRiskAssessment, RenameRiskLevel


class RenameDifferenceAnalyzer:
    """根据文件名、字段证据和解析器警告计算可审计风险。"""

    def analyze(
        self,
        *,
        original_filename: str,
        proposed_filename: str,
        metadata: FilenameMetadataResult,
        arbitration_warnings: list[dict[str, Any]] | None = None,
    ) -> RenameRiskAssessment:
        """返回稳定原因代码；硬阻断不能被后续模型覆盖。"""

        score = 0.0
        reasons: list[str] = []
        blockers: list[str] = []
        original_stem = _normalize(Path(original_filename).stem)
        proposed_stem = _normalize(Path(proposed_filename).stem)
        title = _normalize(metadata.title.value or "")

        if Path(original_filename).suffix.lower() != Path(proposed_filename).suffix.lower():
            blockers.append("TARGET_EXTENSION_CHANGED")

        evidence = metadata.title.evidence_items
        evidence_quotes = _normalize("".join(item.quote for item in evidence))
        evidence_pages = [item.page_number for item in evidence if item.page_number is not None]
        if evidence_pages and min(evidence_pages) > 1:
            blockers.append("TITLE_FROM_LATER_PAGE")
        if metadata.title.source != "filename" and title and (not evidence_quotes or title not in evidence_quotes):
            blockers.append("TITLE_NOT_IN_EVIDENCE")
        if looks_like_body_sentence(metadata.title.value or ""):
            blockers.append("BODY_SENTENCE_AS_TITLE")

        low_information = _is_low_information_name(original_stem)
        similarity = _combined_similarity(original_stem, title or proposed_stem)
        if not low_information and similarity < 0.22:
            score += 0.65
            reasons.append("LOW_FILENAME_SIMILARITY")
        elif not low_information and similarity < 0.42:
            score += 0.35
            reasons.append("MODERATE_FILENAME_SIMILARITY")

        for warning in arbitration_warnings or []:
            code = str(warning.get("code") or "")
            field = str(warning.get("field") or "")
            if field == "title" and code in {"RENAME_FIELD_AMBIGUOUS", "RENAME_FIELD_FALLBACK_SELECTED"}:
                score += 0.35
                reasons.append("PARSER_TITLE_CONFLICT")
            if field == "document_number" and code == "RENAME_FIELD_AMBIGUOUS":
                blockers.append("DOCUMENT_NUMBER_CONFLICT")
            if field in {"document_date", "year"} and code == "RENAME_FIELD_AMBIGUOUS":
                blockers.append("DOCUMENT_DATE_CONFLICT")

        if len(metadata.title.value or "") > 100:
            score += 0.2
            reasons.append("TITLE_UNUSUALLY_LONG")
        if any(code in {"OCR_QUALITY_LOW", "OCR_LOW_CONFIDENCE"} for code in _warning_codes(arbitration_warnings)):
            score += 0.35
            reasons.append("OCR_QUALITY_LOW")

        score = min(1.0, score + (0.7 if blockers else 0.0))
        level = RenameRiskLevel.HIGH if score >= 0.65 else RenameRiskLevel.MEDIUM if score >= 0.35 else RenameRiskLevel.LOW
        return RenameRiskAssessment(
            risk_level=level,
            risk_score=round(score, 4),
            reason_codes=list(dict.fromkeys(reasons)),
            hard_blockers=list(dict.fromkeys(blockers)),
        )


def _normalize(value: str) -> str:
    """消除比较无关字符，但不改写业务词语。"""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def _combined_similarity(left: str, right: str) -> float:
    """组合序列和二元字符集合相似度，兼顾中英文文件名。"""

    if not left or not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right).ratio()
    left_pairs = {left[index : index + 2] for index in range(max(1, len(left) - 1))}
    right_pairs = {right[index : index + 2] for index in range(max(1, len(right) - 1))}
    union = left_pairs | right_pairs
    jaccard = len(left_pairs & right_pairs) / len(union) if union else 0.0
    return max(sequence, jaccard)


def _is_low_information_name(value: str) -> bool:
    """识别扫描件、附件编号等无法提供标题语义的旧文件名。"""

    compact = re.sub(r"(?:附件|扫描件|scan|img|image|document|文档|文件|副本|copy|new)", "", value, flags=re.I)
    compact = re.sub(r"\d+", "", compact)
    return len(compact) <= 2


def _warning_codes(warnings: list[dict[str, Any]] | None) -> set[str]:
    """读取结构化警告代码。"""

    return {str(item.get("code") or "") for item in warnings or []}
