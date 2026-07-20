"""分类体系关键词匹配测试。"""

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import DocumentFeatures, match_document_text, recall_category_candidates


def test_matcher_returns_specific_school_category_path():
    """正文命中子分类名称时，应返回包含学校一级域的完整分类路径。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("本文件涉及教师职称申报材料。", taxonomy)

    assert matches[0]["name"] == "学校/人事师资/职称"
    assert matches[0]["category_path"] == ["学校", "人事师资", "职称"]
    assert matches[0]["taxonomy_key"] == "unified_school_file_classification"
    assert matches[0]["taxonomy_version"] == "2026-07-v2"
    assert "职称" in matches[0]["evidence"]


def test_matcher_prefers_longer_category_name():
    """同时可能命中短词和长词时，应优先返回更具体的长分类名称。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("请归档学院财务管理相关制度。", taxonomy)

    assert matches[0]["category_path"] == ["学院", "财务管理"]
    assert matches[0]["evidence"] == ["财务管理"]
    assert ["学校", "财务"] not in [item["category_path"] for item in matches]


def test_matcher_returns_multiple_categories_sorted_and_deduped():
    """单个文件命中多个分类时，应返回多个去重后的分类建议并按置信度排序。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("学校教师职称材料，同时包含干部工作和会议纪要。", taxonomy)

    paths = [item["category_path"] for item in matches]
    confidences = [item["confidence"] for item in matches]
    assert ["学校", "人事师资", "职称"] in paths
    assert ["学校", "党委相关", "干部工作"] in paths
    assert ["学校", "行政综合管理类", "会议纪要"] in paths
    assert len(paths) == len({tuple(path) for path in paths})
    assert confidences == sorted(confidences, reverse=True)


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
            "taxonomy_key": "unified_school_file_classification",
            "taxonomy_version": "2026-07-v2",
        }
    ]


def test_recall_candidates_uses_aliases_and_positive_signals_for_implicit_topic():
    """即使正文未出现标准分类名，也应通过别名和正向信号召回正确候选。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="2025年教师岗位聘期考核工作安排.docx",
        title="教师岗位聘期考核工作安排",
        full_text="请各学院组织专任教师完成岗位续聘材料提交和聘期考核结果确认。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=5)

    assert candidates[0].category_id == "school.hr.appointment-assessment"
    assert candidates[0].category_path == ["学校", "人事师资", "考核聘任"]
    assert {"教师", "岗位", "聘期", "考核", "续聘"} & set(candidates[0].matched_signals)
    assert candidates[0].rule_score > 0
    assert "标题" in candidates[0].candidate_reason or "正文" in candidates[0].candidate_reason


def test_matcher_uses_document_number_department_as_parent_category_signal():
    """文号中的“人事”应召回学校人事师资父分类。"""

    taxonomy = load_default_taxonomy()
    matches = match_document_text(
        "西安理工人事〔2022〕14号\n关于崔杰等21位同志任职资格的通知",
        taxonomy,
    )

    hr_category = next(item for item in matches if item["category_path"] == ["学校", "人事师资"])
    assert "人事" in hr_category["evidence"]


def test_unified_taxonomy_classifies_school_appointment_notice_without_college_duplicate():
    """统一 taxonomy 应识别校级职称材料，并抑制同名学院候选。"""

    taxonomy = load_default_taxonomy()
    matches = match_document_text(
        "工程师资格-西理人事[2022]14号.PDF\n"
        "西安理工人事〔2022〕14号\n"
        "关于崔杰等21位同志任职资格的通知\n"
        "专业技术职务任职资格",
        taxonomy,
    )

    category_ids = {item["category_id"] for item in matches}
    assert "school.hr.title-review" in category_ids
    assert "school.hr" in category_ids
    assert "college.hr.title-review" not in category_ids


def test_recall_candidates_penalizes_negative_signals():
    """负向信号应降低冲突分类分数，避免奖学金文本误归入教师考核聘任。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        filename="学生奖学金志愿服务证明.docx",
        title="学生奖学金志愿服务证明",
        full_text="本材料用于学生奖学金评审和志愿服务时长证明，不涉及教师岗位聘任。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=8)
    appointment = next(item for item in candidates if item.category_id == "school.hr.appointment-assessment")
    student = next(item for item in candidates if item.category_id == "college.student-affairs")

    assert {"奖学金", "志愿服务"} & set(appointment.negative_signals)
    assert student.rule_score > appointment.rule_score


def test_recall_candidates_respects_limit_and_stable_sorting():
    """候选召回应按分数排序并遵守调用方给出的数量上限。"""

    taxonomy = load_default_taxonomy()
    features = DocumentFeatures(
        title="学校学院教师科研财务会议纪要年度总结",
        full_text="材料同时涉及学校、学院、教师、科研、财务、会议纪要、年度总结等多个主题。",
    )

    candidates = recall_category_candidates(features, taxonomy, limit=3)

    assert len(candidates) == 3
    assert [item.rule_score for item in candidates] == sorted(
        [item.rule_score for item in candidates],
        reverse=True,
    )


def test_match_document_text_uses_recall_candidates_for_rule_only_output():
    """兼容入口应基于候选召回生成 rule-only 分类建议。"""

    taxonomy = load_default_taxonomy()

    matches = match_document_text("教师岗位聘期考核和续聘材料。", taxonomy)

    assert matches[0]["category_path"] == ["学校", "人事师资", "考核聘任"]
    assert matches[0]["source"] == "rule"
    assert "聘期" in matches[0]["evidence"] or "续聘" in matches[0]["evidence"]
