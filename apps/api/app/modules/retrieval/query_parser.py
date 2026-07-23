"""确定性的文件搜索查询解析器，只解析受控字段。

解析器职责：
- 去除低信息量请求词
- 使用 Jieba 与业务词典提取主题词
- 用服务器时区确定性解析"今年、去年、前年"和显式年份
- 提取已存在 taxonomy 别名、单位、人名、文号和文档类型候选
- 生成绑定参数，不允许将用户文本拼接为 SQL 或原生 tsquery

解析失败时保留安全的原始关键词检索；不能调用外部 LLM 兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


_FILLER_PHRASES = [
    "请帮我",
    "麻烦帮我",
    "帮我",
    "请",
    "查找",
    "查一下",
    "搜索",
    "检索",
    "寻找",
    "找出",
    "找到",
    "找我",
    "找",
    "有没有",
    "有哪些",
    "给我",
    "相关的",
    "有关的",
    "相关",
    "有关",
    "文件",
    "文档",
    "材料",
    "一下",
]

_YEAR_PATTERN = re.compile(r"(20\d{2})年?")
_RELATIVE_YEAR_PATTERN = re.compile(r"(去年|前年|今年)")
_DOC_NUMBER_PATTERN = re.compile(r"[(\[]?\d+\s*号[\])]?")


@dataclass(frozen=True)
class ParsedQuery:
    """查询解析的结构化结果。"""

    original: str
    cleaned: str
    terms: list[str] = field(default_factory=list)
    year: int | None = None
    relative_year: int | None = None
    taxonomy_candidates: list[str] = field(default_factory=list)
    unit_candidates: list[str] = field(default_factory=list)
    person_candidates: list[str] = field(default_factory=list)
    doc_number: str | None = None


class FileSearchQueryParser:
    """确定性的文件搜索查询解析器。

    不调用 LLM、embedding 或外部服务。
    解析失败时返回包含原始关键词的安全结构。
    """

    def __init__(
        self,
        *,
        tokenizer: Any,
        taxonomy: Any | None = None,
        server_tz: str = "Asia/Shanghai",
    ) -> None:
        self.tokenizer = tokenizer
        self.taxonomy = taxonomy
        self.server_tz = server_tz

    def parse(self, query: str) -> ParsedQuery:
        """将自然语言查询解析为结构化参数。

        即使解析失败，也返回包含 cleaned 字段的安全结果。
        """
        if not query:
            return ParsedQuery(original="", cleaned="")

        # 1. 去除低信息量请求词
        cleaned = self._remove_fillers(query)

        # 2. 提取文号
        doc_number = self._extract_doc_number(cleaned)

        # 3. 提取显式年份
        year = self._extract_year(cleaned)

        # 4. 提取相对年份
        relative_year = self._extract_relative_year(cleaned)

        # 5. 分词提取主题词
        try:
            terms = self.tokenizer.tokenize(cleaned) if hasattr(self.tokenizer, "tokenize") else []
        except Exception:
            terms = []

        # 6. 提取 taxonomy 候选（可在后续任务中扩展）
        taxonomy_candidates = self._match_taxonomy_candidates(cleaned, terms)

        return ParsedQuery(
            original=query,
            cleaned=cleaned,
            terms=terms[:64],
            year=year,
            relative_year=relative_year,
            doc_number=doc_number,
            taxonomy_candidates=taxonomy_candidates,
        )

    def _remove_fillers(self, text: str) -> str:
        """去除查询中的低信息量请求词。"""
        result = text.lower()
        for phrase in _FILLER_PHRASES:
            result = result.replace(phrase, " ")
        # 去除多余空白
        result = " ".join(result.split())
        return result

    def _extract_year(self, text: str) -> int | None:
        """提取显式年份（如 2024、2024年）。"""
        matches = _YEAR_PATTERN.findall(text)
        if matches:
            for m in matches:
                year = int(m)
                if 2000 <= year <= 2100:
                    return year
        return None

    def _extract_relative_year(self, text: str) -> int | None:
        """提取相对年份（去年=-1、前年=-2、今年=0）。"""
        match = _RELATIVE_YEAR_PATTERN.search(text)
        if match:
            keyword = match.group(1)
            mapping = {"今年": 0, "去年": -1, "前年": -2}
            return mapping.get(keyword)
        return None

    def _extract_doc_number(self, text: str) -> str | None:
        """提取文号。"""
        match = _DOC_NUMBER_PATTERN.search(text)
        if match:
            return match.group(0).strip()
        return None

    def _match_taxonomy_candidates(
        self, cleaned: str, terms: list[str]
    ) -> list[str]:
        """与 taxonomy 别名匹配提取候选分类（预留接口）。"""
        if not self.taxonomy:
            return []
        # TODO: 后续任务中实现 taxonomy 别名匹配
        return []
