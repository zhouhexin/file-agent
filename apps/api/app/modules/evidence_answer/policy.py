"""阶段五证据问题的确定性分类和回答策略。

该模块只根据用户文字选择证据处理规则，不调用 LLM，也不负责解析文件范围。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


EvidenceQuestionType = Literal[
    "FILE_FACT",
    "DATE_FACT",
    "PERSON_OR_ORG",
    "DOCUMENT_NUMBER",
    "CLAUSE",
    "SUMMARY",
    "COMPARE",
    "TABLE_CALCULATION",
    "UNSUPPORTED",
]


@dataclass(frozen=True)
class EvidenceQuestionDecision:
    """一个问题对应的确定性证据策略。"""

    question_type: EvidenceQuestionType
    answer_mode: Literal["FOCUSED", "FULL_SUMMARY"]
    require_original_evidence: bool = True
    deterministic_calculation_required: bool = False


class EvidenceQuestionPolicy:
    """用稳定中文规则区分事实、总结、比较和确定性计算问题。"""

    _FULL_SUMMARY_MARKERS = (
        "完整总结",
        "全文总结",
        "全面总结",
        "详细总结",
        "概括全文",
        "总结一下",
        "帮我总结",
        "请总结",
        "总结这份",
        "总结这个",
        "总结文档",
        "总结文件",
        "覆盖每个章节",
        "覆盖全部章节",
        "覆盖所有章节",
        "每个章节",
        "各个章节",
        "所有章节",
        "全文",
    )
    # 用户明确说“简要”时才收窄为聚焦回答。没有这个限定时，“总结某文件”
    # 的合理默认含义是完整阅读后的全文总结，不能因为措辞不同而漏掉章节。
    _FOCUSED_SUMMARY_MARKERS = (
        "简要总结",
        "简单总结",
        "简短总结",
        "一句话总结",
        "只要要点",
        "几个要点",
        "摘要",
    )
    _TABLE_MARKERS = (
        "汇总表格",
        "统计表格",
        "工作表",
        "各学院",
        "总金额",
        "合计金额",
        "求和",
        "排名",
        "人数",
    )
    _DATE_MARKERS = ("日期", "时间", "截止", "何时", "什么时候", "哪一天")
    _PERSON_OR_ORG_MARKERS = (
        "谁",
        "人员",
        "姓名",
        "申请人",
        "负责人",
        "老师",
        "学院",
        "部门",
        "单位",
        "机构",
    )
    _DOCUMENT_NUMBER_MARKERS = ("文号", "文件号", "编号", "发文字号")
    _CLAUSE_MARKERS = ("第几条", "条款", "规定", "办法", "第六条", "第五条", "第四条")
    _COMPARE_MARKERS = ("比较", "对比", "区别", "差异", "不同")
    _UNSUPPORTED_MARKERS = ("实时天气", "实时股价", "联网搜索", "网上查", "互联网")

    def decide(
        self,
        *,
        question: str,
        requested_mode: str = "AUTO",
    ) -> EvidenceQuestionDecision:
        """返回问题类型与回答模式，显式模式优先于自动总结判断。"""

        normalized = "".join(str(question or "").split()).casefold()
        if requested_mode in {"FOCUSED", "FULL_SUMMARY"}:
            answer_mode = requested_mode
        else:
            is_summary_request = "总结" in normalized or "概括" in normalized
            answer_mode = (
                "FOCUSED"
                if is_summary_request
                and any(marker in normalized for marker in self._FOCUSED_SUMMARY_MARKERS)
                else "FULL_SUMMARY"
                if is_summary_request
                or any(marker in normalized for marker in self._FULL_SUMMARY_MARKERS)
                else "FOCUSED"
            )

        if any(marker in normalized for marker in self._UNSUPPORTED_MARKERS):
            question_type: EvidenceQuestionType = "UNSUPPORTED"
        elif any(marker in normalized for marker in self._TABLE_MARKERS):
            question_type = "TABLE_CALCULATION"
        elif any(marker in normalized for marker in self._COMPARE_MARKERS):
            question_type = "COMPARE"
        elif answer_mode == "FULL_SUMMARY" or "总结" in normalized or "概括" in normalized:
            question_type = "SUMMARY"
        elif any(marker in normalized for marker in self._DOCUMENT_NUMBER_MARKERS):
            question_type = "DOCUMENT_NUMBER"
        elif any(marker in normalized for marker in self._DATE_MARKERS):
            question_type = "DATE_FACT"
        elif any(marker in normalized for marker in self._CLAUSE_MARKERS):
            question_type = "CLAUSE"
        elif any(marker in normalized for marker in self._PERSON_OR_ORG_MARKERS):
            question_type = "PERSON_OR_ORG"
        else:
            question_type = "FILE_FACT"

        return EvidenceQuestionDecision(
            question_type=question_type,
            answer_mode=answer_mode,
            require_original_evidence=question_type != "UNSUPPORTED",
            deterministic_calculation_required=question_type == "TABLE_CALCULATION",
        )
