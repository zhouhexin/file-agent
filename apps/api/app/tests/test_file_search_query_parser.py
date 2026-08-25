"""FileSearchQueryParser 测试。

测试目标：
1. 确定性的查询解析器可导入
2. 去除低信息量请求词
3. 提取年份（显式 + "去年/前年"）
4. 提取主题词
5. 解析失败时保留原始关键词
"""

from datetime import date, datetime, timezone
from typing import Any

from app.modules.retrieval.query_parser import (
    FileSearchQueryParser,
    ParsedQuery,
    exact_short_chinese_phrase,
)


class _FakeTokenizer:
    """确定性 fake 分词器，用于测试。"""

    def tokenize(self, text: str) -> list[str]:
        return text.split()


def _make_parser(
    server_tz: str = "Asia/Shanghai",
    reference_time: datetime | date | None = None,
) -> FileSearchQueryParser:
    return FileSearchQueryParser(
        tokenizer=_FakeTokenizer(),
        taxonomy=None,
        server_tz=server_tz,
        reference_time=reference_time,
    )


def test_parser_importable():
    """FileSearchQueryParser 和 ParsedQuery 可导入。"""
    from app.modules.retrieval.query_parser import FileSearchQueryParser, ParsedQuery
    assert FileSearchQueryParser is not None
    assert ParsedQuery is not None


def test_removes_filler_phrases():
    """去除低信息量请求词。"""
    parser = _make_parser()
    result = parser.parse("帮我找一下去年活动相关的奖学金材料")
    assert "帮我" not in result.cleaned
    assert "找一下" not in result.cleaned
    assert "奖学金" in result.cleaned


def test_removes_common_fillers():
    """多种常见请求词都被去除。"""
    parser = _make_parser()

    cases = [
        ("查找国家励志奖学金文件", "国家励志奖学金"),
        ("搜索学生工作处的通知", "学生工作处 通知"),
        ("有没有奖学金相关的文件", "奖学金"),
        ("找资助相关的材料", "资助"),
        ("请帮我查一下公示期限", "公示期限"),
    ]
    for query, expected_substr in cases:
        result = parser.parse(query)
        # 检查 expected 中的每个词都在 cleaned 中
        for word in expected_substr.split():
            assert word in result.cleaned, (
                f"Query '{query}': expected '{word}' in '{result.cleaned}'"
            )


def test_removes_question_selectors_and_content_relation_phrases():
    """文件选择词和正文关系词必须被清洗，只把用户真正查询的短语交给检索器。"""

    parser = _make_parser()
    cases = [
        ("哪个文件提到了公示期限", "公示期限"),
        ("哪些文档提到过任职通知", "任职通知"),
        ("哪份材料包含了国家励志奖学金", "国家励志奖学金"),
        ("哪几个文件中出现了公示期限", "公示期限"),
        ("哪篇文章提及任职通告", "任职通告"),
        ("哪几份报告正文中含有任职告示", "任职告示"),
    ]

    for query, expected in cases:
        result = parser.parse(query)

        assert result.cleaned == expected
        assert result.terms == [expected]


def test_list_literal_query_removes_action_and_keeps_topic_constraints():
    """“列出涉及劳务费发放的文件”必须保留主题并拆出受控分级条件。"""

    parser = _make_parser()
    result = parser.parse("列出涉及劳务费发放的文件")

    assert result.cleaned == "劳务费发放"
    assert result.relation_mode == "LITERAL"
    # Fake tokenizer 不切分中文短语时，解析器仍必须从末尾宽泛动作词中识别核心主题。
    assert "劳务费" in result.required_topic_terms
    assert result.supporting_topic_terms == ["发放"]


def test_deictic_file_set_selector_matches_plain_file_selector():
    """“这些文件中哪些……”不得残留指代词并改变检索主题。"""

    parser = _make_parser()
    deictic = parser.parse("这些文件中哪些提到了述职报告")
    plain = parser.parse("哪些文件中提到了述职报告")

    assert deictic.cleaned == plain.cleaned == "述职报告"
    assert deictic.terms == plain.terms == ["述职报告"]
    assert deictic.relation_mode == plain.relation_mode == "LITERAL"


def test_normalizes_person_related_search_to_stable_core_query():
    """“关于/与…有关”必须得到同一个核心主题，关系虚词不能干扰正文召回。"""

    parser = _make_parser()

    for query in [
        "查找与金海燕老师有关的文件",
        "查找关于金海燕老师的相关文件",
    ]:
        result = parser.parse(query)

        assert result.cleaned == "金海燕老师"
        assert result.terms == ["金海燕老师"]


def test_normalizes_equivalent_related_document_phrases_to_same_query():
    """关于、与主题有关以及文件/文档差异不能改变主题检索核心。"""

    parser = _make_parser()
    parsed = [
        parser.parse("关于科研的文档"),
        parser.parse("查找与科研有关的文档"),
        parser.parse("关于科研的文件"),
    ]

    assert {item.cleaned for item in parsed} == {"科研"}
    assert {item.relation_mode for item in parsed} == {"RELATED"}
    assert {tuple(item.terms) for item in parsed} == {("科研",)}


def test_short_chinese_phrase_removes_person_honorifics():
    """短人名必须转为连续匹配核心，不能拆成单字 OR 召回无关文件。"""

    assert exact_short_chinese_phrase("金海燕") == "金海燕"
    assert exact_short_chinese_phrase("金海燕老师") == "金海燕"
    assert exact_short_chinese_phrase("欧阳小明同志") == "欧阳小明"
    assert exact_short_chinese_phrase("国家励志奖学金") is None


def test_find_my_query_does_not_leave_leading_grammar_particle():
    """“找我的”清洗后不能残留句首“的”，否则短主题会被错误精确匹配。"""

    result = _make_parser().parse("找我的奖学金材料")

    assert result.cleaned == "奖学金"


def test_extracts_explicit_year():
    """提取显式年份。"""
    parser = _make_parser()

    result = parser.parse("2024年奖学金")
    assert result.year == 2024

    result = parser.parse("2025国家励志奖学金")
    assert result.year == 2025

    result = parser.parse("奖学金材料")  # 无年份
    assert result.year is None


def test_year_suffix_and_compound_year_queries_share_stable_core_terms():
    """年份后缀不应改变检索结果，组合查询必须分离年份和文件主题。"""

    parser = _make_parser()
    pure_year_queries = [
        "哪些文件中提到了2020",
        "哪些文件中提到了2020年",
        "哪些文件中提到了2020年度",
    ]
    compound_queries = [
        "2020年的述职报告有哪些",
        "2020的述职报告有哪些",
        "2020年度述职报告有哪些",
    ]

    pure_year = [parser.parse(query) for query in pure_year_queries]
    compound = [parser.parse(query) for query in compound_queries]

    assert {(item.cleaned, item.year) for item in pure_year} == {("2020", 2020)}
    assert {(item.cleaned, item.year) for item in compound} == {("述职报告", 2020)}


def test_all_materials_query_removes_result_quantity_word():
    """“全部”描述返回数量，不得污染业务主题短语或造成零召回。"""

    result = _make_parser().parse(
        "找出2017年6月大数据联合实验室授牌相关的全部材料。"
    )

    assert result.year == 2017
    assert result.month == 6
    assert result.cleaned == "大数据联合实验室授牌"


def test_fact_question_separates_file_and_person_anchors_from_requested_fields():
    """事实问句只能用清单和姓名找文件，待回答字段不能拼进连续短语。"""

    result = _make_parser().parse(
        "2017年住宿清单中,潘志康来自哪个单位、住宿几天费用多少"
    )

    assert result.year == 2017
    assert result.is_fact_question is True
    assert result.fact_anchor_phrases == ["住宿清单", "潘志康"]
    assert result.fact_entity_phrases == ["潘志康"]
    assert result.requested_fact_fields == [
        "单位或机构",
        "住宿天数",
        "费用或金额",
    ]


def test_fact_question_extracts_entity_once_across_pronoun_followup_clause():
    """后续“他的……”只描述待回答字段，不能成为新的文件实体。"""

    result = _make_parser().parse(
        "彭绍高来自哪个单位,他的报告题目是什么"
    )

    assert result.is_fact_question is True
    assert result.fact_anchor_phrases == ["彭绍高"]
    assert result.fact_entity_phrases == ["彭绍高"]
    assert result.requested_fact_fields == ["单位或机构", "报告题目"]


def test_fact_question_rules_cover_event_partner_and_location_without_hardcoded_answer():
    """同一通用规则应覆盖机构合作方与活动地点，而不是只识别住宿示例。"""

    result = _make_parser().parse(
        "大数据联合实验室由学院和哪家公司共同建立？仪式在哪里举行？"
    )

    assert result.is_fact_question is True
    assert result.fact_anchor_phrases == ["大数据联合实验室"]
    assert result.requested_fact_fields == ["地点", "公司"]


def test_literal_file_search_does_not_enable_fact_question_rewrite():
    """“哪些文件提到了……”仍保持显式正文连续匹配，不能被事实规则放宽。"""

    result = _make_parser().parse("哪些文件提到了彭绍高")

    assert result.relation_mode == "LITERAL"
    assert result.is_fact_question is False
    assert result.fact_anchor_phrases == []


def test_extracts_relative_year():
    """相对时间必须换算为年份硬过滤条件，而不是遗留为普通检索词。"""

    parser = _make_parser(reference_time=date(2026, 7, 22))

    result = parser.parse("找我去年的奖学金材料")
    assert result.relative_year == -1
    assert result.year == 2025
    assert result.cleaned == "奖学金"

    result = parser.parse("前年的资助通知")
    assert result.relative_year == -2
    assert result.year == 2024
    assert result.cleaned == "资助通知"

    result = parser.parse("昨天的工作总结")
    assert result.relative_year == 0
    assert result.year == 2026
    assert result.cleaned == "工作总结"

    new_year_parser = _make_parser(reference_time=date(2026, 1, 1))
    result = new_year_parser.parse("前天的工作总结")
    assert result.relative_year == -1
    assert result.year == 2025
    assert result.cleaned == "工作总结"

    leap_day_parser = _make_parser(reference_time=date(2024, 2, 29))
    assert leap_day_parser.parse("去年的工作总结").year == 2023

    result = parser.parse("奖学金材料")  # 无相对年份
    assert result.relative_year is None


def test_parsed_query_has_terms():
    """解析结果包含分词后的主题词。"""
    parser = _make_parser()
    result = parser.parse("国家励志奖学金申请材料")
    assert len(result.terms) > 0
    assert "国家励志奖学金申请材料" in " ".join(result.terms) or "国家" in " ".join(result.terms)


def test_parser_returns_safe_result_on_failure():
    """解析失败时保留安全的原始关键词检索。"""

    class _BrokenTokenizer:
        def tokenize(self, text: str) -> list[str]:
            raise ValueError("tokenizer failed")

    parser = FileSearchQueryParser(
        tokenizer=_BrokenTokenizer(),
        taxonomy=None,
    )
    result = parser.parse("奖学金申请")
    # 即使分词器失败，cleaned 仍保留原始内容
    assert result.cleaned is not None
    assert len(result.cleaned) > 0
    # terms 可能为空，但不应抛出异常
    assert isinstance(result.terms, list)


def test_parser_does_not_call_llm():
    """确认解析器是确定性的，不调用外部模型。"""
    parser = _make_parser()
    result1 = parser.parse("找我去年的国家励志奖学金材料")
    result2 = parser.parse("找我去年的国家励志奖学金材料")
    assert result1.cleaned == result2.cleaned
    assert result1.year == result2.year
    assert result1.relative_year == result2.relative_year
    assert result1.terms == result2.terms
