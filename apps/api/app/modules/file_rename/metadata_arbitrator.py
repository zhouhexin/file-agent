"""对多个解析器生成的重命名字段执行确定性仲裁。"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from app.modules.file_rename.schemas import (
    FilenameMetadataResult,
    RenameEvidenceItem,
    RenameFieldResult,
    RenameFieldStatus,
)


@dataclass(frozen=True)
class RenameMetadataCandidate:
    """一个解析器生成的完整命名字段候选。"""

    parser_name: str
    metadata: FilenameMetadataResult


@dataclass(frozen=True)
class RenameArbitrationResult:
    """字段级仲裁结果及可展示警告。"""

    metadata: FilenameMetadataResult
    warnings: list[dict[str, Any]] = field(default_factory=list)


class RenameMetadataArbitrator:
    """逐字段比较候选，解析器之间不存在整体优先级。"""

    def arbitrate(self, candidates: list[RenameMetadataCandidate]) -> RenameArbitrationResult:
        """生成最终字段；高质量冲突保持歧义而不是自动猜测。"""

        if not candidates:
            missing = RenameFieldResult(status=RenameFieldStatus.MISSING, confidence=0)
            return RenameArbitrationResult(
                metadata=FilenameMetadataResult(
                    document_date=missing,
                    year=missing,
                    document_number=missing,
                    title=missing,
                ),
                warnings=[{"code": "RENAME_METADATA_MISSING", "message": "没有可用于重命名的解析结果。"}],
            )

        warnings: list[dict[str, Any]] = []
        selected = {
            field_name: self._select_field(field_name, candidates, warnings)
            for field_name in ("document_date", "year", "document_number", "title")
        }
        document_number_year = _year_from_document_number(selected["document_number"].value)
        if selected["year"].value and document_number_year and selected["year"].value != document_number_year:
            warnings.append(
                {
                    "code": "RENAME_DOCUMENT_NUMBER_YEAR_DIFFERS",
                    "message": "落款日期年份与文号年份不同，命名年份使用已仲裁的落款年份。",
                    "year": selected["year"].value,
                    "document_number_year": document_number_year,
                }
            )
        return RenameArbitrationResult(metadata=FilenameMetadataResult(**selected), warnings=warnings)

    def _select_field(
        self,
        field_name: str,
        candidates: list[RenameMetadataCandidate],
        warnings: list[dict[str, Any]],
    ) -> RenameFieldResult:
        """按值分组选择单个字段，并保留同值证据或冲突候选。"""

        resolved = [
            (candidate.parser_name, getattr(candidate.metadata, field_name))
            for candidate in candidates
            if getattr(candidate.metadata, field_name).status == RenameFieldStatus.RESOLVED
            and getattr(candidate.metadata, field_name).value
        ]
        if not resolved:
            alternatives = list(
                dict.fromkeys(
                    alternative
                    for candidate in candidates
                    for alternative in getattr(candidate.metadata, field_name).alternatives
                )
            )
            status = RenameFieldStatus.AMBIGUOUS if alternatives else RenameFieldStatus.MISSING
            return RenameFieldResult(status=status, confidence=0, alternatives=alternatives)

        grouped: dict[str, list[tuple[str, RenameFieldResult]]] = {}
        for parser_name, field_result in resolved:
            grouped.setdefault(_comparable_value(field_result.value or ""), []).append((parser_name, field_result))
        if len(grouped) == 1:
            agreeing = next(iter(grouped.values()))
            return _merge_agreeing_fields(field_name, agreeing)

        ranked = sorted(
            resolved,
            key=lambda item: _effective_score(field_name, item[1]),
            reverse=True,
        )
        top_parser, top = ranked[0]
        second_score = _effective_score(field_name, ranked[1][1])
        top_score = _effective_score(field_name, top)
        alternatives = list(dict.fromkeys(item.value for _, item in ranked if item.value))
        if top_score >= 0.85 and second_score >= 0.85:
            warnings.append(_conflict_warning(field_name, alternatives))
            return RenameFieldResult(
                status=RenameFieldStatus.AMBIGUOUS,
                source="parser_conflict",
                confidence=0,
                alternatives=alternatives,
                evidence_items=_merge_evidence([item for _, field in ranked for item in field.evidence_items]),
            )
        if top_score - second_score < 0.12:
            warnings.append(_conflict_warning(field_name, alternatives))
            return RenameFieldResult(
                status=RenameFieldStatus.AMBIGUOUS,
                source="parser_conflict",
                confidence=0,
                alternatives=alternatives,
                evidence_items=_merge_evidence([item for _, field in ranked for item in field.evidence_items]),
            )

        warnings.append(
            {
                "code": "RENAME_FIELD_FALLBACK_SELECTED",
                "message": f"{_FIELD_LABELS[field_name]}候选不一致，已选择证据更强的结果。",
                "field": field_name,
                "selected_parser": top_parser,
                "alternatives": alternatives,
            }
        )
        return _with_selection_reason(top, f"{top_parser} 候选得分高于其他解析器")


_FIELD_LABELS = {
    "document_date": "发文日期",
    "year": "年份",
    "document_number": "文号",
    "title": "标题",
}


def _effective_score(field_name: str, field_result: RenameFieldResult) -> float:
    """根据字段证据校正基础置信度，避免普通章节标题获得高优先级。"""

    score = field_result.confidence
    labels = {item.element_label for item in field_result.evidence_items if item.element_label}
    if field_name == "title":
        if "title" in labels:
            score += 0.03
        elif "section_header" in labels:
            score -= 0.12
    if field_result.source == "filename":
        score -= 0.08
    return max(0, min(1, score))


def _merge_agreeing_fields(
    field_name: str,
    agreeing: list[tuple[str, RenameFieldResult]],
) -> RenameFieldResult:
    """合并多个解析器对同一字段值的证据。"""

    parser_names = list(dict.fromkeys(parser_name for parser_name, _ in agreeing))
    best = max(agreeing, key=lambda item: _effective_score(field_name, item[1]))[1]
    confidence = min(0.99, best.confidence + (0.03 if len(parser_names) > 1 else 0))
    reason = "多个解析器结果一致" if len(parser_names) > 1 else f"采用 {parser_names[0]} 解析结果"
    return best.model_copy(
        update={
            "source": "hybrid_agreement" if len(parser_names) > 1 else best.source,
            "confidence": confidence,
            "evidence_items": _merge_evidence(
                [
                    item.model_copy(update={"selection_reason": reason})
                    for _, field_result in agreeing
                    for item in field_result.evidence_items
                ]
            ),
        }
    )


def _with_selection_reason(field_result: RenameFieldResult, reason: str) -> RenameFieldResult:
    """在不改变字段值的情况下记录选择原因。"""

    return field_result.model_copy(
        update={
            "evidence_items": [item.model_copy(update={"selection_reason": reason}) for item in field_result.evidence_items]
        }
    )


def _merge_evidence(items: list[RenameEvidenceItem]) -> list[RenameEvidenceItem]:
    """按可定位字段去重证据，避免 OperationPlan 重复展示相同引用。"""

    unique: dict[tuple[Any, ...], RenameEvidenceItem] = {}
    for item in items:
        key = (item.parser_name, item.page_number, item.element_index, item.quote, item.source)
        unique.setdefault(key, item)
    return list(unique.values())


def _comparable_value(value: str) -> str:
    """仅消除空白差异，不擅自改写业务字段。"""

    return re.sub(r"\s+", "", value).strip()


def _year_from_document_number(value: str | None) -> str | None:
    """从已确认文号中读取年份，用于生成非阻断冲突警告。"""

    if not value:
        return None
    match = re.search(r"(?:19|20)\d{2}", value)
    return match.group(0) if match else None


def _conflict_warning(field_name: str, alternatives: list[str]) -> dict[str, Any]:
    """构造高置信度字段冲突警告。"""

    return {
        "code": "RENAME_FIELD_AMBIGUOUS",
        "message": f"{_FIELD_LABELS[field_name]}存在多个可靠候选，需要用户复核。",
        "field": field_name,
        "alternatives": alternatives,
    }
