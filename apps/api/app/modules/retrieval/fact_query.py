"""事实问句的确定性检索计划。

LLM 仍负责选择 ``hybrid-search`` 和 ``evidence-answer``。本模块只把已经进入
检索 Tool 的自然语言问题拆成“用于找文件的锚点”和“需要从证据回答的字段”，
避免把“来自哪个单位、费用多少”等问句骨架误当成正文连续短语。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_QUESTION_MARKER_PATTERN = re.compile(
    r"(?:是什么|分别是|哪个|哪些|哪家|哪里|何处|何地|几天|多少|多少钱|"
    r"何时|什么时候|哪一天|为何|为什么|如何|怎么|是否|有无|谁)"
)
_CLAUSE_SPLIT_PATTERN = re.compile(r"[，,；;。！？?、\n]+")
_CONTEXT_PATTERN = re.compile(
    r"(?:^|[，,；;。！？?\s])([^，,；;。！？?]{2,40}?)(?:里面|中|里)"
    r"(?=[，,；;。！？?\s]|$)"
)
_SUBJECT_PREDICATE_PATTERN = re.compile(
    r"^(.{2,30}?)(?:来自|隶属(?:于)?|属于|由|负责|参加|担任|"
    r"何时|什么时候|哪一天|在哪里|在何处|为何|为什么|如何|怎么|是否|是)"
)
_POSSESSIVE_FACT_PATTERN = re.compile(
    r"^(.{2,30}?)的(?:报告)?(?:题目|单位|机构|部门|日期|时间|地点|"
    r"费用|金额|天数|身份|职务|文号|编号|名称|结果|结论|要求|条件)"
)
_YEAR_MONTH_PATTERN = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*年(?:度)?|(?<!\d)(?:1[0-2]|0?[1-9])\s*月"
)
_LEADING_REQUEST_PATTERN = re.compile(
    r"^(?:请帮我|麻烦帮我|帮我|请|查一下|查询|查找|搜索|检索|"
    r"找出|找到|找|告诉我|我想知道)\s*"
)
_PRONOUNS = {"他", "她", "它", "他们", "她们", "其", "该", "这个", "这份"}
_GENERIC_SUBJECTS = {
    "仪式",
    "会议",
    "活动",
    "报告",
    "文件",
    "文档",
    "材料",
    "表格",
    "清单",
    "费用",
    "金额",
    "单位",
    "题目",
    "时间",
    "日期",
    "地点",
}
_DOCUMENT_HINTS = (
    "清单",
    "名单",
    "通知",
    "报告",
    "表格",
    "文件",
    "文档",
    "材料",
    "办法",
    "规定",
    "方案",
    "汇总",
)
_FIELD_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("单位或机构", ("哪个单位", "哪家单位", "单位", "机构", "部门", "来自哪里")),
    ("人员", ("谁", "姓名", "人员")),
    ("日期或时间", ("何时", "什么时候", "哪一天", "日期", "时间")),
    ("地点", ("在哪里", "哪里举行", "何处", "何地", "地点")),
    ("住宿天数", ("住宿几天", "几天", "住宿天数")),
    ("费用或金额", ("费用多少", "金额", "多少钱", "费用")),
    ("报告题目", ("报告题目", "题目")),
    ("公司", ("哪家公司", "哪个公司", "公司")),
    ("文号或编号", ("文号", "文件号", "编号", "发文字号")),
    ("要求或条件", ("要求", "条件")),
    ("原因", ("为什么", "为何", "原因")),
    ("方法", ("如何", "怎么")),
)


@dataclass(frozen=True)
class FactSearchPlan:
    """后端可验证的事实检索锚点，不包含任何推测答案。"""

    is_fact_question: bool = False
    anchor_phrases: tuple[str, ...] = ()
    entity_phrases: tuple[str, ...] = ()
    requested_fields: tuple[str, ...] = ()


def build_fact_search_plan(
    *, query: str, cleaned: str, relation_mode: str
) -> FactSearchPlan:
    """从普通事实问句提取有限锚点，显式原文检索保持既有严格语义。"""

    original = str(query or "").strip()
    if not original or str(relation_mode or "") == "LITERAL":
        return FactSearchPlan()
    if _QUESTION_MARKER_PATTERN.search(original) is None:
        return FactSearchPlan()

    normalized = _normalize_source(original)
    anchors: list[str] = []
    entity_anchors: list[str] = []

    # “某清单中，张三……”中的清单名称是文件范围线索；它与人名都要由
    # 后端实际索引验证，不能让模型自行认定目标文件。
    for match in _CONTEXT_PATTERN.finditer(normalized):
        candidate = _clean_anchor(match.group(1))
        if _valid_anchor(candidate):
            anchors.append(candidate)

    for raw_clause in _CLAUSE_SPLIT_PATTERN.split(normalized):
        clause = raw_clause.strip()
        if not clause:
            continue
        # 同一分句中已有“……中”时，只分析其后的事实主体；目录/文件锚点已
        # 由上面的上下文规则单独保存。
        clause = re.sub(r"^.*?(?:里面|中|里)\s*", "", clause, count=1)
        if not clause:
            continue
        if re.match(r"^(?:他|她|它|他们|她们|其|该|这个|这份)的", clause):
            # 代词只复用前一分句已解析的主体，不能把“他的报告题目”当成
            # 新实体，也不能在没有会话消解证据时自行猜测指代对象。
            continue
        match = _POSSESSIVE_FACT_PATTERN.search(clause)
        if match is None:
            match = _SUBJECT_PREDICATE_PATTERN.search(clause)
        if match is None:
            continue
        candidate = _clean_anchor(match.group(1))
        if not _valid_anchor(candidate):
            continue
        anchors.append(candidate)
        if _looks_like_short_entity(candidate):
            entity_anchors.append(candidate)

    unique_anchors = tuple(dict.fromkeys(anchors))[:6]
    if not unique_anchors:
        return FactSearchPlan()
    requested_fields = tuple(
        field_name
        for field_name, markers in _FIELD_MARKERS
        if any(marker in original for marker in markers)
    )[:12]
    return FactSearchPlan(
        is_fact_question=True,
        anchor_phrases=unique_anchors,
        entity_phrases=tuple(dict.fromkeys(entity_anchors))[:4],
        requested_fields=requested_fields,
    )


def _normalize_source(value: str) -> str:
    """移除时间过滤语法和检索请求前缀，但保留事实分句边界。"""

    result = _LEADING_REQUEST_PATTERN.sub("", str(value or "").strip())
    result = _YEAR_MONTH_PATTERN.sub(" ", result)
    result = re.sub(r"(?:前年|去年|今年)\s*", " ", result)
    return re.sub(r"\s+", "", result)


def _clean_anchor(value: str) -> str:
    """清理锚点两端的语法连接词，不重写业务同义词。"""

    result = _YEAR_MONTH_PATTERN.sub(" ", str(value or ""))
    result = _LEADING_REQUEST_PATTERN.sub("", result.strip())
    result = re.sub(r"^(?:关于|与|和|的|在)+|(?:相关|有关|的)$", "", result)
    return re.sub(r"\s+", "", result).strip()


def _valid_anchor(value: str) -> bool:
    """拒绝代词、纯字段名和过长句子，防止问句骨架再次进入检索。"""

    if not value or value in _PRONOUNS or value in _GENERIC_SUBJECTS:
        return False
    if _QUESTION_MARKER_PATTERN.search(value):
        return False
    return 2 <= len(value) <= 30


def _looks_like_short_entity(value: str) -> bool:
    """标记可做一字纠错提示的短中文实体，文件主题不进入纠错。"""

    if not re.fullmatch(r"[\u3400-\u9fff]{2,4}", value):
        return False
    return not any(marker in value for marker in _DOCUMENT_HINTS)
