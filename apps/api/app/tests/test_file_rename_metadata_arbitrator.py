"""重命名字段级仲裁测试。"""

from __future__ import annotations

from app.modules.file_rename.metadata_arbitrator import (
    RenameMetadataArbitrator,
    RenameMetadataCandidate,
)
from app.modules.file_rename.schemas import (
    FilenameMetadataResult,
    RenameEvidenceItem,
    RenameFieldResult,
    RenameFieldStatus,
)


def _field(value: str | None, *, confidence: float, parser: str, label: str | None = None, source: str = "document"):
    """构造带解析器证据的字段结果。"""

    return RenameFieldResult(
        value=value,
        status=RenameFieldStatus.RESOLVED if value else RenameFieldStatus.MISSING,
        confidence=confidence,
        source=source,
        evidence_items=(
            [RenameEvidenceItem(quote=value or "", source=source, parser_name=parser, element_label=label)]
            if value
            else []
        ),
    )


def _metadata(*, parser: str, title: RenameFieldResult, year: str = "2024", number: str | None = None):
    """构造仲裁所需的完整字段集合。"""

    return RenameMetadataCandidate(
        parser_name=parser,
        metadata=FilenameMetadataResult(
            document_date=_field("20240712", confidence=0.93, parser=parser),
            year=_field(year, confidence=0.93, parser=parser),
            document_number=_field(number, confidence=0.94, parser=parser),
            title=title,
        ),
    )


def test_arbitrator_merges_evidence_when_parsers_agree():
    """两个解析器结果一致时应合并证据并提升置信度。"""

    result = RenameMetadataArbitrator().arbitrate(
        [
            _metadata(parser="docling", title=_field("学校印章使用管理的通知", confidence=0.94, parser="docling", label="title")),
            _metadata(parser="native", title=_field("学校印章使用管理的通知", confidence=0.92, parser="native")),
        ]
    )

    assert result.metadata.title.value == "学校印章使用管理的通知"
    assert result.metadata.title.source == "hybrid_agreement"
    assert {item.parser_name for item in result.metadata.title.evidence_items} == {"docling", "native"}


def test_arbitrator_prefers_native_title_over_docling_section_header():
    """普通章节标题不能覆盖原生解析器识别出的可靠主标题。"""

    result = RenameMetadataArbitrator().arbitrate(
        [
            _metadata(parser="docling", title=_field("工作要求", confidence=0.76, parser="docling", label="section_header")),
            _metadata(parser="native", title=_field("关于规范学校印章使用管理的通知", confidence=0.92, parser="native")),
        ]
    )

    assert result.metadata.title.value == "关于规范学校印章使用管理的通知"
    assert result.metadata.title.status == RenameFieldStatus.RESOLVED
    assert any(item["code"] == "RENAME_FIELD_FALLBACK_SELECTED" for item in result.warnings)


def test_arbitrator_marks_two_high_confidence_titles_ambiguous():
    """两个可靠主标题冲突时不得自动生成可执行名称。"""

    result = RenameMetadataArbitrator().arbitrate(
        [
            _metadata(parser="docling", title=_field("奖学金评审工作通知", confidence=0.94, parser="docling", label="title")),
            _metadata(parser="native", title=_field("奖学金评审实施方案", confidence=0.92, parser="native")),
        ]
    )

    assert result.metadata.title.status == RenameFieldStatus.AMBIGUOUS
    assert result.metadata.can_build_filename is False
    assert set(result.metadata.title.alternatives) == {"奖学金评审工作通知", "奖学金评审实施方案"}


def test_arbitrator_keeps_issue_date_year_when_document_number_year_differs():
    """落款年份与文号年份不同时保留命名年份并输出审计警告。"""

    result = RenameMetadataArbitrator().arbitrate(
        [
            _metadata(
                parser="native",
                title=_field("学校印章使用管理的通知", confidence=0.92, parser="native"),
                year="2024",
                number="西安理工发〔2023〕2号",
            )
        ]
    )

    assert result.metadata.year.value == "2024"
    assert any(item["code"] == "RENAME_DOCUMENT_NUMBER_YEAR_DIFFERS" for item in result.warnings)
