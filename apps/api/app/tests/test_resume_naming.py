"""个人简历专用命名规则测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.modules.file_rename.resume_naming import suggest_resume_filename
from app.modules.file_rename.uploaded_suggestion_service import (
    UploadedRenameSuggestionService,
)
from app.db.models import Document


_STRUCTURED_RESUME = "出生年月\n教育经历\n工作经历\n发表论文"


@pytest.mark.parametrize(
    ("filename", "expected_name"),
    [
        ("1967_姓名洪明輝出生年月日.pdf", "洪明輝"),
        ("_卫凡-东京工业大学.doc", "卫凡"),
        ("_尹毅峰1_.doc", "尹毅峰"),
        ("于富财的个人履历.DOC", "于富财"),
        ("于富财的个人履历[1].DOC", "于富财"),
        ("任方简历-西电.doc", "任方"),
        ("刘汉强-西安电子科技大学-博士研究生.doc", "刘汉强"),
        ("温苗利.doc", "温苗利"),
        ("王青龙(1).doc", "王青龙"),
        ("西北工业大学-航空宇航制造工程-洪歧[1].doc", "洪歧"),
    ],
)
def test_resume_filename_patterns_use_personal_resume_template(
    filename: str,
    expected_name: str,
) -> None:
    """历史应聘简历文件名应稳定提取姓名并保留原扩展名类型。"""

    suggestion = suggest_resume_filename(
        original_filename=filename,
        pages=[SimpleNamespace(text_content=_STRUCTURED_RESUME)],
    )

    assert suggestion is not None
    assert suggestion.filename == f"{expected_name}_个人简历{filename[filename.rfind('.'):].lower()}"


def test_resume_body_name_has_priority_over_filename() -> None:
    """正文姓名字段优先于文件名和人员目录候选。"""

    suggestion = suggest_resume_filename(
        original_filename="李四简历.docx",
        pages=[
            SimpleNamespace(
                text_content="个人简历\n姓名：张三\n教育经历\n工作经历\n联系方式"
            )
        ],
        source_relative_path="2014应聘人员/王五/李四简历.docx",
    )

    assert suggestion is not None
    assert suggestion.filename == "张三_个人简历.docx"
    assert suggestion.evidence_source == "resume_body_name"


def test_resume_uses_applicant_container_when_filename_has_no_name() -> None:
    """正文未标姓名且文件名泛化时，可使用应聘材料包中的人员目录。"""

    suggestion = suggest_resume_filename(
        original_filename="个人简历.pdf",
        pages=[SimpleNamespace(text_content="个人简历\n教育经历\n工作经历")],
        source_relative_path="2015/王青龙/个人简历.pdf",
    )

    assert suggestion is not None
    assert suggestion.filename == "王青龙_个人简历.pdf"
    assert suggestion.evidence_source == "managed_source_container"


def test_research_interest_statement_is_not_renamed_as_resume() -> None:
    """应聘材料中的研究兴趣声明不能仅凭业务目录被改成个人简历。"""

    suggestion = suggest_resume_filename(
        original_filename="Statement of Research Interest.pdf",
        pages=[SimpleNamespace(text_content="Research interests and proposed projects")],
        source_relative_path="2015/John Smith/Statement of Research Interest.pdf",
    )

    assert suggestion is None


def test_document_mentioning_resume_is_not_itself_a_resume() -> None:
    """招聘通知正文仅提到提交个人简历时不能触发简历专用命名。"""

    suggestion = suggest_resume_filename(
        original_filename="招聘材料提交通知.docx",
        pages=[SimpleNamespace(text_content="请应聘人员提交个人简历、证书和代表性成果。")],
        source_relative_path="2015/王青龙/招聘材料提交通知.docx",
    )

    assert suggestion is None


def test_multiple_labeled_names_do_not_guess_resume_owner() -> None:
    """正文出现多个姓名字段时必须保留原名等待复核。"""

    suggestion = suggest_resume_filename(
        original_filename="个人简历汇总.docx",
        pages=[
            SimpleNamespace(
                text_content="个人简历\n姓名：张三\n教育经历\n姓名：李四\n工作经历"
            )
        ],
    )

    assert suggestion is None


def test_shared_rename_service_applies_personal_resume_template(monkeypatch) -> None:
    """同步、批量上传和单文件上传共用的建议服务应返回简历专用模板。"""

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    validation_service = SimpleNamespace(
        settings=SimpleNamespace(ocr_llm_fallback_quality_threshold=0.5)
    )
    service = UploadedRenameSuggestionService(
        db=db,
        user_id="resume-user",
        validation_service=validation_service,
    )
    monkeypatch.setattr(
        service,
        "_extract_document",
        lambda **_kwargs: (
            {"status": "COMPLETED", "extraction_run_id": "resume-run"},
            [SimpleNamespace(text_content="个人简历\n姓名：王青龙\n教育经历\n工作经历")],
            [],
        ),
    )
    monkeypatch.setattr(service, "_managed_source_relative_path", lambda _document: "")
    document = Document(
        id="resume-document",
        user_id="resume-user",
        original_filename="王青龙(1).DOC",
        content_type="application/msword",
        size_bytes=12,
        sha256="a" * 64,
        status="ACTIVE",
    )

    suggestion, _extraction = service.suggest_for_initial_import(document=document)

    assert suggestion["status"] == "READY"
    assert suggestion["template_key"] == "personal_resume"
    assert suggestion["resume_name"] == "王青龙"
    assert suggestion["proposed_filename"] == "王青龙_个人简历.doc"
