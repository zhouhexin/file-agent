"""文件基础分类器测试。"""

from types import SimpleNamespace

from app.modules.agent.document_classifier import classify_document_text
from app.core.config import get_settings
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.input_fingerprint import digest_text
from app.modules.classification.purpose_policy import (
    PurposePackageMember,
    PurposePackageSnapshot,
)
from app.modules.classification.runtime_factory import ClassificationRuntimeFactory


def _purpose_package(*, category_id: str, content: str) -> PurposePackageSnapshot:
    """构造绑定当前测试内容的不可变用途包，不能依赖 source_context 路径猜测。"""

    return PurposePackageSnapshot(
        id=f"package-{category_id}",
        workspace_id="workspace-1",
        root_key="managed",
        source_container_id="container-1",
        purpose_category_id=category_id,
        taxonomy_key="unified_school_file_classification",
        taxonomy_version="2026-09-v10",
        policy_id="test-package",
        policy_version="1",
        manifest_digest=f"manifest-{category_id}",
        members=(
            PurposePackageMember(
                document_version_id="",
                sha256=digest_text(content),
            ),
        ),
        authorization_source="INITIAL_INGEST_TASK",
        source_request_id="request-1",
    )


def test_summary_fulltext_conflict_uses_fulltext_primary(monkeypatch):
    """摘要 Top-1 与全文冲突时必须采用全文候选，同时保留冲突审计字段。"""

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")
    monkeypatch.setenv("LLM_CLASSIFICATION_SUMMARY_ENABLED", "true")
    get_settings.cache_clear()
    summary_result = SimpleNamespace(
        classification_text="现从事学科及研究方向：计算机科学与技术",
        classification_summary=SimpleNamespace(id="classification-summary"),
        document_summary=SimpleNamespace(id="document-summary"),
        reused=False,
    )
    summary_service = SimpleNamespace(
        generate_or_reuse=lambda **_kwargs: summary_result,
    )
    service = DocumentClassificationService(
        graph_mode="off",
        summary_service=summary_service,
    )
    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content=(
                "专家鉴定意见表\n申报专业技术职务：教授\n"
                "校属各单位教师职务任职资格评审委员会对申报材料进行评审。"
            ),
            page_number=1,
            sheet_name=None,
        )
    ]

    try:
        result = service.classify(
            document_id="",
            extraction_run_id="summary-fulltext-conflict-run",
            filename="材料.doc",
        )
    finally:
        get_settings.cache_clear()

    assert result["categories"][0]["category_id"] == "school.hr.title-review"
    assert result["categories"][0]["summary_fulltext_agreement"] is False


def test_managed_title_review_package_overrides_intrinsic_file_topic(monkeypatch):
    """职称评定包中的论文或教学附件以包的业务用途作为唯一主分类。"""

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")
    monkeypatch.setenv("LLM_CLASSIFICATION_SUMMARY_ENABLED", "false")
    get_settings.cache_clear()
    service = DocumentClassificationService(graph_mode="off")
    content = "本科教学工作量和代表性科研成果。"
    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content=content,
            page_number=1,
            sheet_name=None,
        )
    ]

    try:
        result = service.classify(
            document_id="",
            extraction_run_id="managed-title-review-package-run",
            filename="5.填表说明及材料要求.doc",
            purpose_package=_purpose_package(
                category_id="school.hr.title-review",
                content=content,
            ),
        )
    finally:
        get_settings.cache_clear()

    category = result["categories"][0]
    assert category["category_id"] == "school.hr.title-review"
    assert category["source"] == "verified_purpose_package"
    assert category["confidence"] < 1.0
    assert category["evidence_items"][0]["type"] == "purpose_package_manifest"


def test_classifier_returns_taxonomy_category_path_with_evidence():
    """文件基础分类器应使用预置分类体系返回完整分类路径。"""

    categories = classify_document_text("本文件涉及教师职称申报材料。")

    assert categories[0]["name"] == "学校/人事师资/职称"
    assert categories[0]["category_path"] == ["学校", "人事师资", "职称"]
    assert categories[0]["taxonomy_key"] == "unified_school_file_classification"
    assert "职称" in categories[0]["evidence"]


def test_classifier_returns_college_category_path_with_evidence():
    """命中学院分类时应保留学院一级域，避免与学校分类混淆。"""

    categories = classify_document_text("本文件是学院年度计划、总结材料。")

    assert categories[0]["name"] == "学院/行政管理/年度计划、总结"
    assert "年度计划、总结" in categories[0]["evidence"]


def test_classifier_returns_other_when_no_keywords_match():
    """无法命中规则时应返回其他分类，避免空分类影响回执。"""

    categories = classify_document_text("这是一段暂时无法判断类型的普通文本。")

    assert categories == [
        {
            "name": "其他",
            "category_id": "system.other",
            "category_path": ["其他"],
            "confidence": 0.0,
            "status": "SUGGESTED",
            "source": "system_fallback",
            "evidence": [],
            "taxonomy_key": "unified_school_file_classification",
            "taxonomy_version": "2026-09-v10",
        }
    ]


def test_classification_service_uses_single_other_instead_of_department_fallback(monkeypatch):
    """部门和文号不足以确认业务时，最终只能使用单一 OTHER。"""

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://test:test@localhost/test",
    )
    get_settings.cache_clear()
    try:
        result = DocumentClassificationService(graph_mode="off").classify(
            document_id="document-finance-fallback",
            extraction_run_id="run-finance-fallback",
            filename="财务处关于“两新”项目配套资金的工作通知.docx",
            fallback_text="财务处关于“两新”项目配套资金的工作通知。",
        )
    finally:
        get_settings.cache_clear()

    assert result["categories"][0]["category_id"] == "system.other"
    assert result["categories"][0]["category_path"] == ["其他"]
    assert result["categories"][0]["source"] == "system_fallback"
    assert result["classification_outcome"] == "OTHER"


def test_managed_source_full_text_without_business_action_uses_other(monkeypatch):
    """只有部门名称而没有业务动作时，完整正文也不能制造部门 fallback。"""

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://test:test@localhost/test",
    )
    get_settings.cache_clear()
    service = DocumentClassificationService(graph_mode="off")
    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content="财务处关于临时联络事项的工作通知。",
            page_number=1,
            sheet_name=None,
        )
    ]

    try:
        result = service.classify(
            document_id="",
            extraction_run_id="managed-source-run",
            filename="临时联络事项.docx",
        )
    finally:
        get_settings.cache_clear()

    category = result["categories"][0]
    assert category["category_id"] == "system.other"
    assert category["status"] == "SUGGESTED"
    assert category["evidence_items"] == []


def test_true_resume_uses_local_structure_and_body_evidence(monkeypatch):
    """真实单人简历依赖局部正文结构，不依赖受管路径继承。"""

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://test:test@localhost/test",
    )
    get_settings.cache_clear()
    service = DocumentClassificationService(graph_mode="off")
    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content=(
                "个人简历\n姓名：李小和\n教育经历：博士后。"
                "工作经历：参加科研项目。联系方式：example@example.com。"
            ),
            page_number=1,
            sheet_name=None,
        )
    ]

    try:
        result = service.classify(
            document_id="",
            extraction_run_id="managed-resume-run",
            filename="李小和简历.doc",
        )
    finally:
        get_settings.cache_clear()

    category = result["categories"][0]
    assert category["category_id"] == "college.hr.faculty-recruitment"
    assert category["category_path"] == ["学院", "人事师资", "师资招聘"]
    assert category["status"] == "SUGGESTED"
    assert category["evidence_items"][0]["page_number"] == 1


def test_managed_recruitment_package_overrides_intrinsic_document_topic(monkeypatch):
    """应聘材料包中的论文、证书等附件应统一继承师资招聘业务用途。"""

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")
    get_settings.cache_clear()
    service = DocumentClassificationService(graph_mode="off")
    content = "国家自然科学基金项目研究成果及代表性论文。"
    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content=content,
            page_number=1,
            sheet_name=None,
        )
    ]

    try:
        result = service.classify(
            document_id="",
            extraction_run_id="managed-package-run",
            filename="代表性论文.pdf",
            purpose_package=_purpose_package(
                category_id="college.hr.faculty-recruitment",
                content=content,
            ),
        )
    finally:
        get_settings.cache_clear()

    category = result["categories"][0]
    assert category["category_id"] == "college.hr.faculty-recruitment"
    assert category["source"] == "verified_purpose_package"
    assert category["evidence_items"][0]["type"] == "purpose_package_manifest"
    assert any(
        item["category_id"] in {"school.research", "college.research"}
        for item in result["categories"][1:]
    )


def test_managed_recruitment_root_loose_file_still_uses_document_content(monkeypatch):
    """应聘根目录散文件没有原容器层级时，仍按文件内容分类。"""

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")
    get_settings.cache_clear()
    service = DocumentClassificationService(graph_mode="off")
    service._load_pages = lambda extraction_run_id: [
        SimpleNamespace(
            text_content="国家自然科学基金项目研究成果及代表性论文。",
            page_number=1,
            sheet_name=None,
        )
    ]

    try:
        result = service.classify(
            document_id="",
            extraction_run_id="managed-loose-file-run",
            filename="代表性论文.pdf",
            default_organization_root="学院",
            source_context="外来应聘/代表性论文.pdf",
        )
    finally:
        get_settings.cache_clear()

    assert result["categories"][0]["source"] != "verified_purpose_package"


def test_runtime_factory_classifier_identity_matches_created_service(monkeypatch):
    """新鲜度检查与实际分类运行必须共享完全相同的分类器版本。"""

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://test:test@localhost/test",
    )
    monkeypatch.setenv("GRAPH_CLASSIFICATION_ENABLED", "false")
    get_settings.cache_clear()
    try:
        factory = ClassificationRuntimeFactory(get_settings())
        service = factory.create(db=None, user_id="classifier-version-user")

        assert factory.classifier_version_for_user(
            user_id="classifier-version-user"
        ) == service.classifier_version
    finally:
        get_settings.cache_clear()
