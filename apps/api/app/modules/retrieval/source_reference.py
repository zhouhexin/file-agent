"""校验显式来源约束：模型选择业务分支，后端拒绝越界的时间过滤与拼造精确短语。"""

from __future__ import annotations

import re
import unicodedata

from app.modules.chunks.tokenizer import ChineseLexicalTokenizer
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.modules.retrieval.query_parser import FileSearchQueryParser
from app.modules.retrieval.semantic_plan import FileSearchSemanticPlan


class EvidenceSourcePlanError(LLMResponseError):
    """仅携带固定原因码，供一次模型参数纠错或受控降级，不携带用户正文。"""

    def __init__(self, code: str) -> None:
        """拒绝未经校验的来源条件，不能把无效计划执行成文件零结果。"""

        self.code = code
        super().__init__(
            "证据检索计划不符合用户指定的来源约束。",
            diagnostic=make_diagnostic(category="SCHEMA_INVALID", stage="INTENT_PLAN_VALIDATION"),
        )


def extract_source_reference(message: str) -> str | None:
    """提取明确来源段，仅用于范围校验或模型失败后的只读兜底，不选择业务分支。"""

    match = re.match(
        r"^\s*(?:请\s*)?(?:只|仅)?(?:根据|依据)\s*"
        r"(?P<reference>[^，,。！？\r\n]{4,160})[，,]\s*"
        r"(?:请\s*)?(?:说明|列出|回答|解释|总结|概括|告诉我|提取|核实)",
        message,
    )
    if not match:
        return None
    reference = match.group("reference").strip().strip("《》“”\"'")
    file_markers = ("通知", "报告", "文件", "文档", "材料", "办法", "规定", "制度", "表格", "流程")
    return reference if any(marker in reference for marker in file_markers) else None


def _compact(value: str) -> str:
    """只统一 Unicode、大小写与空白，不删业务字、不作语义扩展。"""

    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def validate_source_search_plan(
    *, message: str, search_query: str | None,
    semantic_plan: FileSearchSemanticPlan | None,
) -> None:
    """精确主题须来自来源原话；回答中的今年/日期不能改写来源文件的硬过滤。

    不放宽 EXACT_PHRASE，不把词拆为 OR，不修改模型的业务分支；条件不合法时交回规划器。
    非显式来源请求继续使用原有检索协议。可选偏好不作为必需来源主题校验。
    """

    reference = extract_source_reference(message)
    if not reference:
        return
    source = _compact(reference)
    required = semantic_plan.protected_phrases() if semantic_plan is not None else []
    if any(_compact(phrase) not in source for phrase in required):
        raise EvidenceSourcePlanError("SOURCE_EXACT_PHRASE_NOT_GROUNDED")
    if not search_query and not required:
        raise EvidenceSourcePlanError("SOURCE_QUERY_REQUIRED")
    # 使用与检索相同的年月解析；这里不需要分词，不调用模型或读取文件/数据库。
    parser = FileSearchQueryParser(tokenizer=None)
    source_dates = parser.parse(reference)
    query_dates = parser.parse(search_query or message)
    if (query_dates.year, query_dates.month) != (source_dates.year, source_dates.month):
        raise EvidenceSourcePlanError("SOURCE_TIME_FILTER_MISMATCH")
    if search_query:
        # 仅校验明确的来源检索词是否有原话支撑，不把查询词当成文件事实或文件 ID。
        terms = ChineseLexicalTokenizer().tokenize(query_dates.cleaned)
        if not terms or any(_compact(term) not in source for term in terms):
            raise EvidenceSourcePlanError("SOURCE_QUERY_NOT_GROUNDED")
        if not required and _compact(query_dates.cleaned) not in source:
            # 没有结构化主题时，现有策略会把整个 cleaned query 当作精确短语；
            # 不能通过删除 semantic_plan 绕过连续性校验后再次执行必然落空的拼接查询。
            raise EvidenceSourcePlanError("SOURCE_QUERY_NOT_GROUNDED")
