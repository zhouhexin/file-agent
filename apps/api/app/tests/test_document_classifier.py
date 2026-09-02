"""文件基础分类器测试。"""

from types import SimpleNamespace

from app.modules.agent.document_classifier import classify_document_text
from app.core.config import get_settings
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.runtime_factory import ClassificationRuntimeFactory


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
            "category_path": ["其他"],
            "confidence": 0.2,
            "status": "SUGGESTED",
            "evidence": [],
            "taxonomy_key": "unified_school_file_classification",
            "taxonomy_version": "2026-09-v8",
        }
    ]


def test_classification_service_preserves_department_fallback_in_final_result(monkeypatch):
    """分类服务完成图谱和判定阶段后，仍应保留部门层级的最终兜底路径。"""

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

    assert result["categories"][0]["category_id"] == "school.finance.other"
    assert result["categories"][0]["category_path"] == ["学校", "财务", "其他"]
    assert result["categories"][0]["source"] == "rule_fallback"
    assert result["categories"][0]["evidence_items"]


def test_managed_source_full_text_is_used_for_fallback_evidence(monkeypatch):
    """受管源分析未传 fallback_text 时，完整页面正文仍应保留部门兜底证据。"""

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
    assert category["category_id"] == "school.finance.other"
    assert category["status"] == "SUGGESTED"
    assert category["evidence_items"]


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
