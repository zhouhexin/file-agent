"""确定性的文件搜索查询解析器，只解析受控字段。

解析器职责：
- 去除低信息量请求词
- 使用 Jieba 与业务词典提取主题词
- 用服务器时区确定性解析"今年、去年、前年、昨天、前天"和显式年份
- 提取已存在 taxonomy 别名、单位、人名、文号和文档类型候选
- 生成绑定参数，不允许将用户文本拼接为 SQL 或原生 tsquery

解析失败时保留安全的原始关键词检索；不能调用外部 LLM 兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo


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
    "列出",
    "列一下",
    "罗列",
    "展示",
    "显示",
    "相关的",
    "有关的",
    "相关",
    "有关",
    "文件",
    "文档",
    "材料",
    "全部",
    "一下",
]

# 这类词常用于描述文件动作、工作流程或目录管理，本身通常不足以证明用户的
# 具体业务主题。它们只用于把“已验证相关”和“可能相关”分开，不能作为分类或
# 事实回答的依据。
_BROAD_ACTION_TERMS = (
    "发放",
    "管理",
    "工作",
    "处理",
    "安排",
    "通知",
    "申请",
    "审批",
    "统计",
    "汇总",
)

_YEAR_PATTERN = re.compile(r"(20\d{2})年?")
_YEAR_SUFFIX_PATTERN = re.compile(r"((?:19|20)\d{2})\s*年(?:度)?")
_MONTH_PATTERN = re.compile(r"(?<!\d)(1[0-2]|0?[1-9])\s*月")
_RELATIVE_TIME_PATTERN = re.compile(r"(前年|去年|今年|前天|昨天|今天)")
_DOC_NUMBER_PATTERN = re.compile(r"[(\[]?\d+\s*号[\])]?")
_PERSON_HONORIFICS = ("老师", "同志", "先生", "女士")
_QUESTION_FILE_SELECTOR_PATTERN = re.compile(
    r"^(?:哪个|哪些|哪份|哪一份|哪几个|哪几份|哪篇|哪几篇|哪张|哪几张)"
    r"\s*(?:文件|文档|文章|材料|证明|表格|报告)?\s*"
)
_DEICTIC_FILE_SET_SELECTOR_PATTERN = re.compile(
    r"^\s*(?:这些|上述|前述)\s*(?:文件|文档|材料)"
    r"\s*(?:中|里|里面)?\s*"
    r"(?:哪个|哪些|哪份|哪一份|哪几个|哪几份|哪篇|哪几篇|哪张|哪几张)"
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
    month: int | None = None
    relative_year: int | None = None
    taxonomy_candidates: list[str] = field(default_factory=list)
    unit_candidates: list[str] = field(default_factory=list)
    person_candidates: list[str] = field(default_factory=list)
    doc_number: str | None = None
    required_topic_terms: list[str] = field(default_factory=list)
    supporting_topic_terms: list[str] = field(default_factory=list)
    is_fact_question: bool = False
    fact_anchor_phrases: list[str] = field(default_factory=list)
    fact_entity_phrases: list[str] = field(default_factory=list)
    requested_fact_fields: list[str] = field(default_factory=list)


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
        reference_time: datetime | date | None = None,
    ) -> None:
        self.tokenizer = tokenizer
        self.taxonomy = taxonomy
        self.server_tz = server_tz
        self.reference_time = reference_time

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
        month = self._extract_month(cleaned)

        # 4. 相对时间先按服务器当前日期换算为实际年份，供后续硬过滤使用。
        resolved_relative_time = self._resolve_relative_time(cleaned)
        relative_year = resolved_relative_time[1] if resolved_relative_time else None
        if year is None and resolved_relative_time is not None:
            year = resolved_relative_time[0]
            cleaned = _RELATIVE_TIME_PATTERN.sub(str(year), cleaned, count=1)
        elif resolved_relative_time is not None:
            # 显式年份优先，但相对时间不能遗留为正文查询词。
            cleaned = _RELATIVE_TIME_PATTERN.sub(" ", cleaned, count=1)

        # 年份是结构化过滤条件，“2020”“2020年”“2020年度”必须等价。
        # 组合查询中把年份从主题短语移除，避免搜索不存在的连续短语
        # “2020年的述职报告”；纯年份查询则保留规范化数字作为正文条件。
        cleaned = _strip_explicit_year_filter(cleaned, year=year)
        cleaned = _strip_explicit_month_filter(cleaned, month=month)

        # 5. 分词提取主题词
        try:
            terms = self.tokenizer.tokenize(cleaned) if hasattr(self.tokenizer, "tokenize") else []
        except Exception:
            terms = []

        # 6. 提取 taxonomy 候选（可在后续任务中扩展）
        taxonomy_candidates = self._match_taxonomy_candidates(cleaned, terms)

        required_topic_terms, supporting_topic_terms = _extract_topic_constraints(
            cleaned=cleaned,
            terms=terms,
        )
        from app.modules.retrieval.fact_query import build_fact_search_plan

        relation_mode = self._relation_mode(query)
        fact_plan = build_fact_search_plan(
            query=query,
            cleaned=cleaned,
            relation_mode=relation_mode,
        )

        return ParsedQuery(
            original=query,
            cleaned=cleaned,
            relation_mode=relation_mode,
            terms=terms[:64],
            year=year,
            month=month,
            relative_year=relative_year,
            doc_number=doc_number,
            taxonomy_candidates=taxonomy_candidates,
            required_topic_terms=required_topic_terms,
            supporting_topic_terms=supporting_topic_terms,
            is_fact_question=fact_plan.is_fact_question,
            fact_anchor_phrases=list(fact_plan.anchor_phrases),
            fact_entity_phrases=list(fact_plan.entity_phrases),
            requested_fact_fields=list(fact_plan.requested_fields),
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

    def _extract_month(self, text: str) -> int | None:
        """提取显式月份（如 6月、06 月），供正文日期硬过滤使用。"""

        match = _MONTH_PATTERN.search(text)
        return int(match.group(1)) if match is not None else None

    def _resolve_relative_time(self, text: str) -> tuple[int, int] | None:
        """将相对日期解析为目标年份与相对年份差。"""

        match = _RELATIVE_TIME_PATTERN.search(text)
        if match is None:
            return None
        today = self._current_date()
        keyword = match.group(1)
        if keyword == "前年":
            target = _shift_calendar_year(today, years=-2)
        elif keyword == "去年":
            target = _shift_calendar_year(today, years=-1)
        elif keyword == "前天":
            target = today - timedelta(days=2)
        elif keyword == "昨天":
            target = today - timedelta(days=1)
        else:
            target = today
        return target.year, target.year - today.year

    def _current_date(self) -> date:
        """按服务器时区读取当前日期；测试可传入固定时间。"""

        if isinstance(self.reference_time, datetime):
            if self.reference_time.tzinfo is None:
                return self.reference_time.date()
            return self.reference_time.astimezone(ZoneInfo(self.server_tz)).date()
        if isinstance(self.reference_time, date):
            return self.reference_time
        return datetime.now(ZoneInfo(self.server_tz)).date()

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
    # “这些文件中哪些提到了……”与“哪些文件中提到了……”都是文件集合选择问句。
    # 必须在全局删除“文件”等 filler 之前整体清理，否则会残留“这些 中哪些”噪声。
    result = _DEICTIC_FILE_SET_SELECTOR_PATTERN.sub("", result)
    for phrase in _FILLER_PHRASES:
        result = result.replace(phrase, " ")
    # 句末问号、句号和枚举分隔符只属于自然语言语法。若残留到 exact phrase，
    # PostgreSQL 会把“大数据联合实验室授牌。”当成与正文不同的连续短语。
    result = re.sub(r"[，,。！？?；;：:]+", " ", result)
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
    # “年/年度”在文件检索中是年份语法后缀，不应改变正文检索词。
    # 年份后必须保留分隔，避免“2017年6月”被拼成“20176月”，导致年份硬过滤
    # 无法从主题短语中移除。
    result = _YEAR_SUFFIX_PATTERN.sub(r"\1 ", result)
    return " ".join(result.split())


def is_file_set_selector_question(text: str) -> bool:
    """判断“这些文件中哪些……”是否只是文件检索问句前缀。"""

    return _DEICTIC_FILE_SET_SELECTOR_PATTERN.search(
        str(text or "").lower()
    ) is not None


def _shift_calendar_year(value: date, *, years: int) -> date:
    """按日历年平移日期；闰日落到非闰年时安全降为 2 月 28 日。"""

    target_year = value.year + years
    try:
        return value.replace(year=target_year)
    except ValueError:
        return date(target_year, 2, 28)


def _strip_explicit_year_filter(text: str, *, year: int | None) -> str:
    """从组合主题中移除显式年份，并为纯年份查询保留统一数字条件。"""

    if year is None:
        return str(text or "").strip()
    year_text = str(year)
    stripped = re.sub(
        rf"(?<!\d){re.escape(year_text)}(?!\d)\s*(?:的)?",
        " ",
        str(text or ""),
    )
    normalized = " ".join(stripped.split()).strip()
    return normalized or year_text


def _strip_explicit_month_filter(text: str, *, month: int | None) -> str:
    """组合查询中移除月份，纯月份查询仍保留规范化的“n月”。"""

    if month is None:
        return str(text or "").strip()
    stripped = re.sub(
        rf"(?<!\d)0?{month}\s*月\s*(?:的)?",
        " ",
        str(text or ""),
        count=1,
    )
    normalized = " ".join(stripped.split()).strip()
    return normalized or f"{month}月"


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


def _extract_topic_constraints(
    *, cleaned: str, terms: list[str]
) -> tuple[list[str], list[str]]:
    """从完整主题中抽取可验证的核心词与宽泛动作词。

    例如“劳务费发放”中的“劳务费”是用户查询的核心业务主题，“发放”是
    常见业务动作。检索阶段可据此把仅命中“发放”的文件降为可能相关，避免
    误称其已经涉及劳务费。这里不调用 LLM，也不改变原始查询短语。
    """

    compact = re.sub(r"\s+", "", str(cleaned or ""))
    normalized_terms: list[str] = []
    for value in terms:
        term = re.sub(r"\s+", "", str(value or ""))
        if term and term not in normalized_terms:
            normalized_terms.append(term)

    # 末尾动作词的结构优先级高于分词结果。Jieba 的搜索模式可能同时返回
    # “劳务费发放”“劳务”“务费”等重叠词；这些片段不能被分别当成多个核心
    # 条件，否则会放大 SQL 调用并降低“劳务费”这一完整主题的可解释性。
    for action in _BROAD_ACTION_TERMS:
        if compact.endswith(action) and len(compact) - len(action) >= 2:
            return [compact[: -len(action)]], [action]

    supporting = [term for term in normalized_terms if term in _BROAD_ACTION_TERMS]
    required = [
        term
        for term in normalized_terms
        if term not in _BROAD_ACTION_TERMS and len(term) >= 2
    ]

    # 只有同时存在核心主题和宽泛动作时才提供分级约束；其它短语继续按完整
    # 短语检索，避免把“任职通知”等正常业务术语拆得过细。
    if not required or not supporting:
        return [], []
    return required[:4], supporting[:4]
