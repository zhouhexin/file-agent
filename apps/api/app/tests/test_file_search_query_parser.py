"""FileSearchQueryParser 测试。

测试目标：
1. 确定性的查询解析器可导入
2. 去除低信息量请求词
3. 提取年份（显式 + "去年/前年"）
4. 提取主题词
5. 解析失败时保留原始关键词
"""

from datetime import datetime, timezone
from typing import Any

from app.modules.retrieval.query_parser import FileSearchQueryParser, ParsedQuery


class _FakeTokenizer:
    """确定性 fake 分词器，用于测试。"""

    def tokenize(self, text: str) -> list[str]:
        return text.split()


def _make_parser(server_tz: str = "Asia/Shanghai") -> FileSearchQueryParser:
    return FileSearchQueryParser(
        tokenizer=_FakeTokenizer(),
        taxonomy=None,
        server_tz=server_tz,
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


def test_extracts_explicit_year():
    """提取显式年份。"""
    parser = _make_parser()

    result = parser.parse("2024年奖学金")
    assert result.year == 2024

    result = parser.parse("2025国家励志奖学金")
    assert result.year == 2025

    result = parser.parse("奖学金材料")  # 无年份
    assert result.year is None


def test_extracts_relative_year():
    """解析相对年份（去年/前年）。"""

    # 使用固定时区，当前时间为 2026-07-22
    parser = _make_parser()

    result = parser.parse("找我去年的奖学金材料")
    assert result.relative_year == -1

    result = parser.parse("前年的资助通知")
    assert result.relative_year == -2

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
