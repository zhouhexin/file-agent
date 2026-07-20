"""文件基础分类器测试。"""

from app.modules.agent.document_classifier import classify_document_text


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
            "taxonomy_version": "2026-07-v2",
        }
    ]
