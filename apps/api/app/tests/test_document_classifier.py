"""文件基础分类器测试。"""

from app.modules.agent.document_classifier import classify_document_text


def test_classifier_returns_scholarship_category_with_evidence():
    """包含奖学金关键词的正文应返回奖学金分类和命中依据。"""

    categories = classify_document_text("学生张三获得一等奖学金，综合成绩优秀。")

    assert categories[0]["name"] == "奖学金"
    assert "奖学金" in categories[0]["evidence"]
    assert categories[0]["confidence"] > 0.6


def test_classifier_returns_activity_category_with_evidence():
    """包含志愿服务或社团活动关键词的正文应返回学生活动分类。"""

    categories = classify_document_text("该学生参加志愿服务和社团活动，表现良好。")

    assert categories[0]["name"] == "学生活动"
    assert "志愿" in categories[0]["evidence"]


def test_classifier_returns_other_when_no_keywords_match():
    """无法命中规则时应返回其他分类，避免空分类影响回执。"""

    categories = classify_document_text("这是一段暂时无法判断类型的普通文本。")

    assert categories == [
        {
            "name": "其他",
            "confidence": 0.2,
            "status": "SUGGESTED",
            "evidence": [],
        }
    ]
