"""workdata 版本化分类规则测试。"""

from app.modules.classification.rule_policy import (
    evaluate_rule_policy,
    load_rule_policy,
)


def test_rule_policy_is_strict_versioned_and_bounded():
    """规则文件必须使用固定模式和候选上限，不能成为动态表达式入口。"""

    policy = load_rule_policy()

    assert policy.policy_id == "workdata-classification"
    assert policy.version == "workdata-v1"
    assert policy.policy_mode == "conservative_rules"
    assert policy.candidate_limit == 8
    assert policy.resume_window_paragraphs == 12
    assert policy.resume_window_characters == 3000


def test_union_health_check_and_office_tutorial_have_specific_rules():
    """T06：工会体检和会议写作教程应成立，且不混成教学或真实会议纪要。"""

    union_matches = evaluate_rule_policy(
        filename="2024年女职工专项健康检查通知安排.docx",
        title="女职工专项健康检查通知",
        body_text="校工会组织女职工开展专项健康检查，请按安排参加体检。",
        organization_root="学校",
    )
    tutorial_matches = evaluate_rule_policy(
        filename="如何做好会议记录和纪要.docx",
        title="如何做好会议记录和纪要",
        body_text="本文介绍会议记录的写作方法和会议纪要撰写方法。",
        organization_root=None,
    )

    assert union_matches[0].category_id == "school.party.union"
    assert {item.category_id for item in union_matches}.isdisjoint(
        {"college.teaching", "school.undergraduate-teaching"}
    )
    assert tutorial_matches[0].category_id == "reference.office-guides"
    assert all(item.category_id != "school.admin.meeting-minutes" for item in tutorial_matches)


def test_finance_rules_ignore_source_directory_and_other_word():
    """T07：财务正文不受信息化来源目录或“其他”字段污染。"""

    matches = evaluate_rule_policy(
        filename="关于修订学校收入管理办法的征求意见.docx",
        title="关于修订学校收入管理办法的征求意见",
        body_text="财务处就收入分配和经费收支管理办法征求意见，其他说明见附件。",
        organization_root="学校",
    )

    assert matches[0].category_id == "school.finance"
    assert all(not item.category_id.endswith((".other", ".issued")) for item in matches)
    assert all("party" not in item.category_id for item in matches)
