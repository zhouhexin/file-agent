"""把自然语言问题转换为受控 SpreadsheetQueryPlan。"""

from __future__ import annotations

import re

from pydantic import ValidationError

from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient

from .schemas import (
    Aggregation,
    ColumnProfile,
    ColumnType,
    FilterOperator,
    MetricSpec,
    SheetProfile,
    SpreadsheetFilter,
    SpreadsheetQueryPlan,
    WorkbookProfile,
)


SPREADSHEET_QUERY_PLAN_PROMPT = """你是 File Agent 的受控表格分析规划器。

根据用户问题和 workbook_profile，返回严格 JSON；不要解释，不要计算。

安全规则：
1. 只能使用 workbook_profile 中已有的 sheet_id 和 column_id。
2. 禁止输出 SQL、Python、公式、文件路径、单元格地址、命令或未出现的字段。
3. 只能使用 operation：count_rows、sum、avg、min、max。
4. count_rows 的 metric.column_id 必须为 null；sum、avg、min、max 必须选择 value_type=number 的 column_id。
5. 第一版最多一个 group_by_column_id、最多三个 filters；sort_direction 只能是 asc 或 desc；limit 范围 1 到 100。
6. 用户问题语义不明确，或者当前列不足以确认请求时，返回：
   {"clarification_required": true, "clarification_question": "..."}
   此时不要输出 sheet_id、metric、group_by_column_id 或 filters。
7. 用户明确问“多少条/有几条/数量”时使用 count_rows；问“合计/总和”时选择 sum；问“平均”选择 avg；问“最大/最小”选择 max/min。
8. 你只做计划，不计算结果。
"""


def build_query_plan(
    *,
    client: OpenAICompatibleLLMClient | None,
    question: str,
    profile: WorkbookProfile,
) -> SpreadsheetQueryPlan:
    """优先生成确定性简单查询计划，复杂问题再调用 LLM 并执行严格校验。"""

    deterministic_plans = build_deterministic_query_plans(
        question=question,
        profile=profile,
    )
    if deterministic_plans:
        return deterministic_plans[0]
    if client is None:
        return SpreadsheetQueryPlan(
            clarification_required=True,
            clarification_question="请明确要统计的数值字段、筛选对象或分组方式。",
        )

    payload = {
        "question": question,
        "workbook_profile": _safe_profile_payload(profile),
        "output_schema": SpreadsheetQueryPlan.model_json_schema(),
    }
    parsed = client.complete_json(
        system_prompt=SPREADSHEET_QUERY_PLAN_PROMPT,
        user_payload=payload,
    )
    try:
        return SpreadsheetQueryPlan.model_validate(parsed)
    except ValidationError as exc:
        raise LLMResponseError(f"表格分析计划不符合受控 schema：{exc}") from exc


def build_query_plans(
    *,
    client: OpenAICompatibleLLMClient | None,
    question: str,
    profile: WorkbookProfile,
) -> list[SpreadsheetQueryPlan]:
    """为一次工作簿分析返回一个或多个受控计划。

    “某人的总金额”可以在多个结构兼容的 Sheet 中出现，因此确定性路径会逐 Sheet 生成计划，
    执行层再展示分 Sheet 明细和总计。复杂问题仍只接受一个经过 schema 校验的 LLM 计划。
    """

    deterministic_plans = build_deterministic_query_plans(
        question=question,
        profile=profile,
    )
    if deterministic_plans:
        return deterministic_plans
    return [build_query_plan(client=client, question=question, profile=profile)]


def build_deterministic_query_plans(
    *,
    question: str,
    profile: WorkbookProfile,
) -> list[SpreadsheetQueryPlan]:
    """识别“按明确对象筛选并求和”的低耗确定性计划。

    规则只引用真实 Profile 中的 Sheet/列 ID。无法唯一确认数值列、筛选列或筛选值时返回空列表，
    让 LLM 规划器或澄清流程接管，不能猜测字段。
    """

    operation = _deterministic_operation(question)
    filter_value = _person_filter_value(question)
    if operation is None or filter_value is None:
        return []

    plans: list[SpreadsheetQueryPlan] = []
    for sheet in profile.sheets:
        metric_column = _select_metric_column(
            sheet=sheet,
            question=question,
            operation=operation,
        )
        filter_column = _select_person_filter_column(
            sheet=sheet,
            filter_value=filter_value,
        )
        if metric_column is None or filter_column is None:
            continue
        plans.append(
            SpreadsheetQueryPlan(
                sheet_id=sheet.sheet_id,
                metric=MetricSpec(
                    operation=operation,
                    column_id=metric_column.column_id,
                    label=f"{metric_column.name}合计",
                ),
                filters=[
                    SpreadsheetFilter(
                        column_id=filter_column.column_id,
                        operator=FilterOperator.EQUALS,
                        value=filter_value,
                    )
                ],
                sort_direction="desc",
                limit=50,
            )
        )
    # Profile 只保留每列前五个样本，样本未出现不能证明目标不存在。
    # 必须扫描全部结构兼容 Sheet，再由执行器以 rows_matched=0 排除未命中 Sheet。
    return plans


def _deterministic_operation(question: str) -> Aggregation | None:
    """从明确聚合词识别只读操作；“总金额”固定为求和。"""

    normalized = _normalize_text(question)
    if any(keyword in normalized for keyword in ["总金额", "总额", "合计", "总和", "求和"]):
        return Aggregation.SUM
    if any(keyword in normalized for keyword in ["平均", "均值"]):
        return Aggregation.AVG
    if "最大" in normalized or "最高" in normalized:
        return Aggregation.MAX
    if "最小" in normalized or "最低" in normalized:
        return Aggregation.MIN
    return None


def _person_filter_value(question: str) -> str | None:
    """提取“金海燕的资助总金额”等表达中的人员名称。"""

    normalized = re.sub(r"[\s，。！？、,.!?:：;；“”\"'（）()]+", "", question)
    match = re.search(
        r"(?:请问|查询|查找|统计|计算|帮我|看看|想知道)*"
        r"(?P<name>[\u4e00-\u9fff·]{2,12}?)(?:老师|同志)?的"
        r"(?:科研|成果|论文|项目|奖励|奖学金|资助)*"
        r"(?:总金额|总额|金额合计|经费合计|金额|经费)",
        normalized,
    )
    if not match:
        return None
    name = match.group("name")
    # 前置礼貌语可能被非贪婪表达保留，统一剥离，不修改人名主体。
    name = re.sub(r"^(?:请问|查询|查找|统计|计算|帮我|看看|想知道)+", "", name)
    return name or None


def _select_metric_column(
    *,
    sheet: SheetProfile,
    question: str,
    operation: Aggregation,
) -> ColumnProfile | None:
    """从真实数值列中唯一选择与问题最相关的指标列。"""

    if operation == Aggregation.COUNT_ROWS:
        return None
    numeric_columns = [
        column for column in sheet.columns if column.value_type == ColumnType.NUMBER
    ]
    if not numeric_columns:
        return None
    normalized_question = _normalize_text(question).replace("总金额", "金额")
    terms = ["资助金额", "金额", "经费", "额度", "人数", "数量", "分数"]
    scored = []
    for column in numeric_columns:
        normalized_name = _normalize_text(column.name)
        score = 0
        if normalized_name and normalized_name in normalized_question:
            score += 10
        score += sum(
            3
            for term in terms
            if term in normalized_question and term in normalized_name
        )
        scored.append((score, column.column_index, column))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored or scored[0][0] <= 0:
        return numeric_columns[0] if len(numeric_columns) == 1 else None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2]


def _select_person_filter_column(
    *,
    sheet: SheetProfile,
    filter_value: str,
) -> ColumnProfile | None:
    """从人员语义列中唯一选择筛选列，优先采用样本精确命中的列。"""

    string_columns = [
        column for column in sheet.columns if column.value_type == ColumnType.STRING
    ]
    if not string_columns:
        return None
    person_terms = ["姓名", "申请人", "申报人", "教师", "人员", "负责人", "作者", "获奖人", "学生"]
    scored = []
    for column in string_columns:
        normalized_name = _normalize_text(column.name)
        sample_match = any(
            _normalize_text(value) == _normalize_text(filter_value)
            for value in column.sample_values
        )
        semantic_score = sum(1 for term in person_terms if term in normalized_name)
        score = (20 if sample_match else 0) + semantic_score * 3
        scored.append((score, column.column_index, column))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if not scored or scored[0][0] <= 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2]


def _normalize_text(value: object) -> str:
    """统一字段名、样本值和问题中的空白及常见标点。"""

    return re.sub(r"[\s，。！？、,.!?:：;；“”\"'（）()]+", "", str(value or "")).lower()


def _safe_profile_payload(profile: WorkbookProfile) -> dict:
    """只将结构、类型和少量样本提供给规划模型，不发送整张表数据。"""

    return {
        "document_id": profile.document_id,
        "filename": profile.filename,
        "sheets": [
            {
                "sheet_id": sheet.sheet_id,
                "sheet_name": sheet.sheet_name,
                "header_row": sheet.header_row,
                "row_count": sheet.row_count,
                "columns": [
                    {
                        "column_id": column.column_id,
                        "name": column.name,
                        "value_type": column.value_type.value,
                        "non_empty_count": column.non_empty_count,
                        "sample_values": column.sample_values,
                    }
                    for column in sheet.columns
                ],
            }
            for sheet in profile.sheets
        ],
    }
