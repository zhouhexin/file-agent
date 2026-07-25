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
from typing import Any, Literal


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
_PERSON_HONORIFICS = ("老师", "同志", "先生", "女士")
_QUESTION_FILE_SELECTOR_PATTERN = re.compile(
    r"^(?:哪个|哪些|哪份|哪一份|哪几个|哪几份|哪篇|哪几篇|哪张|哪几张)"
    r"\s*(?:文件|文档|文章|材料|证明|表格|报告)?\s*"
)
_CONTENT_RELATION_PATTERN = re.compile(
    r"^(?:(?:正文|内容)?(?:中|里|里面)\s*)?"
    r"(?:有\s*)?"
    r"(?:"
    r"提到(?:了|过)?|提及(?:了|过)?|"
    r"包含(?:了|有)?|含有|"
    r"出现(?:了|过)?|"
    r"写到(?:了|过)?|写有|"
    r"涉及(?:了|过)?"
    r")\s*"
)
_LITERAL_RELATION_PATTERN = re.compile(
    r"(?:提到(?:了|过)?|提及(?:了|过)?|包含(?:了|有)?|含有|出现(?:了|过)?|"
    r"写到(?:了|过)?|写有|涉及(?:了|过)?|原文(?:中)?有)"
)
_RELATED_RELATION_PATTERN = re.compile(r"(?:相关|有关|关于|类似|相近)")


@dataclass(frozen=True)
class ParsedQuery:
    """查询解析的结构化结果。"""

    original: str
    cleaned: str
    relation_mode: Literal["LITERAL", "RELATED", "UNSPECIFIED"] = "UNSPECIFIED"
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
            relation_mode=self._relation_mode(query),
            terms=terms[:64],
            year=year,
            relative_year=relative_year,
            doc_number=doc_number,
            taxonomy_candidates=taxonomy_candidates,
        )

    def _relation_mode(
        self, text: str
    ) -> Literal["LITERAL", "RELATED", "UNSPECIFIED"]:
        """根据用户业务措辞区分原文连续匹配和相关主题检索。

        该模式只决定后端证据门槛，不把内部切词细节暴露给用户。原文关系词优先级更高，
        避免“查找与某主题相关且正文提到……”被错误放宽为摘要命中。
        """

        normalized = str(text or "").strip().lower()
        if _LITERAL_RELATION_PATTERN.search(normalized):
            return "LITERAL"
        if _RELATED_RELATION_PATTERN.search(normalized):
            return "RELATED"
        return "UNSPECIFIED"

    def _remove_fillers(self, text: str) -> str:
        """去除查询中的低信息量请求词。"""
        return normalize_file_search_query(text)

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


def normalize_file_search_query(text: str) -> str:
    """把等价文件检索问法归一为稳定核心查询。

    两阶段检索和摘要降级必须共用该入口，避免“关于主题的文档”“与主题有关的文档”
    因语法连接词不同而产生不同候选。这里只清理检索动作和关系词，不改写业务同义词，
    同义扩展仍由受控短语策略处理。
    """

    result = str(text or "").lower()
    for phrase in _FILLER_PHRASES:
        result = result.replace(phrase, " ")
    result = " ".join(result.split())
    # “哪个文件、哪几份材料”等问句选择词只用于表达检索动作，不属于检索主题。
    # 这里同时移除紧随其后的文件对象，但不能全局删除“报告、通知”等可能的业务主题。
    result = _QUESTION_FILE_SELECTOR_PATTERN.sub("", result)
    # “与某人有关”“关于某人的相关文件”中的关系词只表达检索意图，
    # 不能进入全文词项，否则同一主题的两种说法会产生不同召回结果。
    # “找我的……”先移除“找我”后会留下句首“的”，它同样只是语法连接词。
    result = re.sub(r"^\s*(?:与|和|关于|的)\s*", "", result)
    # 用户说“提到了、包含、出现过”等是在限定正文匹配关系，真正主题位于其后。
    result = _CONTENT_RELATION_PATTERN.sub("", result)
    result = re.sub(r"\s*的\s*$", "", result)
    return " ".join(result.split())


def exact_short_chinese_phrase(text: str) -> str | None:
    """提取需要连续匹配的短中文实体。

    三、四字人名如果按单字 OR 检索会产生大量误召回。这里同时覆盖常见短业务词，
    并去掉“老师、同志”等称谓，使“金海燕老师”可以匹配正文中的“金海燕”。
    长主题仍使用普通多词召回，避免把自然语言整句错误当成精确短语。
    """

    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    for honorific in _PERSON_HONORIFICS:
        if normalized.endswith(honorific):
            candidate = normalized[: -len(honorific)]
            if re.fullmatch(r"[\u3400-\u9fff]{2,4}", candidate):
                normalized = candidate
                break
    if re.fullmatch(r"[\u3400-\u9fff]{2,4}", normalized):
        return normalized
    return None
