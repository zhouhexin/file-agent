"""分类体系关键词匹配测试。"""

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import match_document_text


def test_matcher_returns_specific_school_category_path():
    """正文命中子分类名称时，应返回包含学校一级域的完整分类路径。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("本文件涉及教师职称申报材料。", taxonomy)

    assert matches[0]["name"] == "学校/人事师资/职称"
    assert matches[0]["category_path"] == ["学校", "人事师资", "职称"]
    assert matches[0]["taxonomy_key"] == "school_file_classification"
    assert matches[0]["taxonomy_version"] == "2026-06"
    assert "职称" in matches[0]["evidence"]


def test_matcher_prefers_longer_category_name():
    """同时可能命中短词和长词时，应优先返回更具体的长分类名称。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("请归档学院财务管理相关制度。", taxonomy)

    assert matches[0]["category_path"] == ["学院", "财务管理"]
    assert matches[0]["evidence"] == ["财务管理"]


def test_matcher_returns_other_when_no_taxonomy_keywords_match():
    """无法命中配置分类时，应返回带 taxonomy 信息的其他分类。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("这是一段无法判断归类的普通文本。", taxonomy)

    assert matches == [
        {
            "name": "其他",
            "category_path": ["其他"],
            "confidence": 0.2,
            "status": "SUGGESTED",
            "evidence": [],
            "taxonomy_key": "school_file_classification",
            "taxonomy_version": "2026-06",
        }
    ]
