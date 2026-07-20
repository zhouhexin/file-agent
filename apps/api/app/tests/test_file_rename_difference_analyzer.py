"""重命名差异风险分析器测试。"""

from app.modules.file_rename.difference_analyzer import RenameDifferenceAnalyzer
from app.modules.file_rename.schemas import (
    FilenameMetadataResult,
    RenameEvidenceItem,
    RenameFieldResult,
    RenameFieldStatus,
)


def _field(value: str | None = None, *, page: int | None = None, source: str = "document_structure"):
    evidence = []
    if value and page is not None:
        evidence = [RenameEvidenceItem(quote=value, page_number=page, source=source)]
    return RenameFieldResult(
        value=value,
        status=RenameFieldStatus.RESOLVED if value else RenameFieldStatus.MISSING,
        source=source,
        confidence=0.9 if value else 0,
        evidence_items=evidence,
    )


def _metadata(title: str, *, page: int = 1) -> FilenameMetadataResult:
    return FilenameMetadataResult(
        year=_field("2024", page=page),
        document_number=_field(),
        title=_field(title, page=page),
    )


def test_low_information_filename_does_not_create_high_risk_by_similarity_alone():
    result = RenameDifferenceAnalyzer().analyze(
        original_filename="附件1.pdf",
        proposed_filename="2024_关于开展奖学金评审工作的通知.pdf",
        metadata=_metadata("关于开展奖学金评审工作的通知"),
    )

    assert result.risk_level.value == "LOW"
    assert "LOW_FILENAME_SIMILARITY" not in result.reason_codes


def test_unrelated_meaningful_filename_creates_high_risk():
    result = RenameDifferenceAnalyzer().analyze(
        original_filename="实验室设备采购预算.xlsx",
        proposed_filename="2024_学生奖学金评审通知.xlsx",
        metadata=_metadata("学生奖学金评审通知"),
    )

    assert result.risk_level.value == "HIGH"
    assert "LOW_FILENAME_SIMILARITY" in result.reason_codes


def test_later_page_title_is_hard_blocked():
    result = RenameDifferenceAnalyzer().analyze(
        original_filename="通知.pdf",
        proposed_filename="2024_第三页模板标题.pdf",
        metadata=_metadata("第三页模板标题", page=3),
    )

    assert "TITLE_FROM_LATER_PAGE" in result.hard_blockers


def test_title_missing_from_evidence_is_hard_blocked():
    metadata = _metadata("建议标题")
    metadata.title.evidence_items[0].quote = "完全不同的原文"

    result = RenameDifferenceAnalyzer().analyze(
        original_filename="年度材料.docx",
        proposed_filename="2024_建议标题.docx",
        metadata=metadata,
    )

    assert "TITLE_NOT_IN_EVIDENCE" in result.hard_blockers


def test_parser_title_fallback_conflict_increases_risk():
    result = RenameDifferenceAnalyzer().analyze(
        original_filename="学校印章通知.docx",
        proposed_filename="2024_学校印章使用管理通知.docx",
        metadata=_metadata("学校印章使用管理通知"),
        arbitration_warnings=[{"code": "RENAME_FIELD_FALLBACK_SELECTED", "field": "title"}],
    )

    assert result.risk_level.value in {"MEDIUM", "HIGH"}
    assert "PARSER_TITLE_CONFLICT" in result.reason_codes


def test_extension_change_is_hard_blocked():
    result = RenameDifferenceAnalyzer().analyze(
        original_filename="年度总结.docx",
        proposed_filename="2024_年度总结.pdf",
        metadata=_metadata("年度总结"),
    )

    assert "TARGET_EXTENSION_CHANGED" in result.hard_blockers


def test_body_sentence_cannot_be_used_as_title():
    title = "工程硕士研究生学制为二年半到五年；总学分≥32，其中学位课学分≥18。四、课程设置见附录。五、开题报告"

    result = RenameDifferenceAnalyzer().analyze(
        original_filename="计算机技术工程硕士课程.doc",
        proposed_filename=f"{title}.doc",
        metadata=_metadata(title),
    )

    assert result.risk_level.value == "HIGH"
    assert "BODY_SENTENCE_AS_TITLE" in result.hard_blockers


def test_formal_title_with_year_and_one_clause_is_not_body_sentence():
    title = "关于开展2024年度工程硕士课程设置修订工作的通知"

    result = RenameDifferenceAnalyzer().analyze(
        original_filename="工程硕士课程修订通知.docx",
        proposed_filename=f"2024_{title}.docx",
        metadata=_metadata(title),
    )

    assert "BODY_SENTENCE_AS_TITLE" not in result.hard_blockers


def test_numbered_section_heading_cannot_be_used_as_document_title():
    title = "五、开题报告"

    result = RenameDifferenceAnalyzer().analyze(
        original_filename="工程硕士课程.doc",
        proposed_filename=f"{title}.doc",
        metadata=_metadata(title),
    )

    assert "BODY_SENTENCE_AS_TITLE" in result.hard_blockers
