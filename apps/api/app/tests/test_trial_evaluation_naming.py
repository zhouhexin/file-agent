"""应聘试讲意见表专用命名规则测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.db.models import Document
from app.modules.file_rename.trial_evaluation_naming import (
    should_preserve_trial_evaluation_source_name,
    suggest_trial_evaluation_filename,
)
from app.modules.file_rename.uploaded_suggestion_service import (
    UploadedRenameSuggestionService,
)


def test_trial_evaluation_filename_preserves_both_roles() -> None:
    suggestion = suggest_trial_evaluation_filename(
        original_filename=(
            "计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx"
        ),
        source_relative_path=(
            "2020/考查试讲表/原始考察表/7月3日/"
            "计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx"
        ),
    )

    assert suggestion is not None
    assert suggestion.filename == (
        "2020_计算机科学与工程学院应聘试讲意见表_"
        "应聘人李光磊_评议人鲁晓锋.docx"
    )


def test_trial_evaluation_rule_requires_two_names_and_managed_year() -> None:
    assert suggest_trial_evaluation_filename(
        original_filename="计算机科学与工程学院应聘试讲意见表-李光磊.docx",
        source_relative_path="2020/考查试讲表/意见表-李光磊.docx",
    ) is None


def test_trial_evaluation_source_name_is_protected_in_recruitment_review_directory() -> None:
    assert should_preserve_trial_evaluation_source_name(
        original_filename=(
            "计算机科学与工程学院应聘试讲意见表（院人才引育小组版）--陈婧-鲁晓锋.docx"
        ),
        source_relative_path=(
            "外来应聘/2022/考查试讲表/考察试讲表/9月7日/"
            "计算机科学与工程学院应聘试讲意见表（院人才引育小组版）--陈婧-鲁晓锋.docx"
        ),
    ) is True
    assert should_preserve_trial_evaluation_source_name(
        original_filename="计算机科学与工程学院应聘试讲意见表-王怀军.docx",
        source_relative_path=(
            "2022/考查试讲表/考察试讲表/9月7日/"
            "计算机科学与工程学院应聘试讲意见表-王怀军.docx"
        ),
    ) is True
    assert should_preserve_trial_evaluation_source_name(
        original_filename="计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx",
        source_relative_path=(
            "2020/考查试讲表/原始考察表/7月6日/"
            "计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx"
        ),
    ) is False
    assert suggest_trial_evaluation_filename(
        original_filename=(
            "计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx"
        ),
        source_relative_path="考查试讲表/意见表.docx",
    ) is None


def test_shared_rename_service_uses_trial_evaluation_template(monkeypatch) -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    service = UploadedRenameSuggestionService(
        db=db,
        user_id="trial-user",
        validation_service=SimpleNamespace(
            settings=SimpleNamespace(ocr_llm_fallback_quality_threshold=0.5)
        ),
    )
    monkeypatch.setattr(
        service,
        "_extract_document",
        lambda **_kwargs: (
            {"status": "COMPLETED", "extraction_run_id": "trial-run"},
            [SimpleNamespace(text_content="计算机科学与工程学院应聘试讲意见表")],
            [],
        ),
    )
    monkeypatch.setattr(
        service,
        "_managed_source_relative_path",
        lambda _document: (
            "2020/考查试讲表/原始考察表/7月3日/"
            "计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx"
        ),
    )
    document = Document(
        id="trial-document",
        user_id="trial-user",
        original_filename=(
            "计算机科学与工程学院应聘试讲意见表-李光磊-鲁晓锋.docx"
        ),
        content_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        size_bytes=12,
        sha256="a" * 64,
        status="ACTIVE",
    )

    suggestion, _extraction = service.suggest_for_initial_import(document=document)

    assert suggestion["status"] == "READY"
    assert suggestion["template_key"] == "recruitment_trial_evaluation"
    assert suggestion["proposed_filename"] == (
        "2020_计算机科学与工程学院应聘试讲意见表_"
        "应聘人李光磊_评议人鲁晓锋.docx"
    )
