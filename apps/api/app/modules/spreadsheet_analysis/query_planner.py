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
7. 只有用户明确问“多少条记录/有几条/记录数/行数”时使用 count_rows。业务对象的“数量”不是行数：
   “岗位数量/招聘人数/计划人数/预算金额”等必须对对应数值列使用 sum；无法唯一确定数值列时返回 clarification_required，禁止用 count_rows 代替。
8. 你只做计划，不计算结果。
"""

PERSON_COLUMN_TERMS = (
    "姓名",
    "申请人",
    "申报人",
    "教师",
    "人员",
    "负责人",
    "作者",
    "获奖人",
    "学生",
)

JOB_QUANTITY_TERMS = (
    "岗位数量",
    "岗位数",
    "招聘人数",
    "招聘数量",
    "计划人数",
    "计划岗位",
)

JOB_METRIC_COLUMN_TERMS = (
    "招聘计划",
    "岗位数量",
    "岗位数",
    "招聘人数",
    "招聘数量",
    "计划人数",
    "优秀人才",
    "青年人才",
    "预聘制",
    "事业编制",
)

JOB_METRIC_EXCLUDED_TERMS = (
    "现有",
    "当前",
    "在岗",
    "序号",
    "编号",
    "方向",
)


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
        plan = SpreadsheetQueryPlan.model_validate(parsed)
    except ValidationError as exc:
        raise LLMResponseError(f"表格分析计划不符合受控 schema：{exc}") from exc
    if (
        _asks_for_job_quantity(question)
        and plan.metric is not None
        and plan.metric.operation == Aggregation.COUNT_ROWS
    ):
        return SpreadsheetQueryPlan(
            clarification_required=True,
            clarification_question=(
                "“岗位数量”需要汇总岗位数值列，不能按数据行数代替。"
                "请确认要统计的招聘类别，或补充包含岗位数的列名。"
            ),
        )
    return plan


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

    job_quantity_plans = _build_job_quantity_plans(
        question=question,
        profile=profile,
    )
    if job_quantity_plans:
        return job_quantity_plans

    operation = _deterministic_operation(question)
    filter_value = _person_filter_value(question, profile=profile)
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


def _build_job_quantity_plans(
    *,
    question: str,
    profile: WorkbookProfile,
) -> list[SpreadsheetQueryPlan]:
    """把岗位数量解释为招聘计划数值列合计，绝不退化为数据行计数。"""

    if not _asks_for_job_quantity(question):
        return []

    plans: list[SpreadsheetQueryPlan] = []
    for sheet in profile.sheets:
        candidates: list[ColumnProfile] = []
        for column in sheet.columns:
            normalized_name = _normalize_text(column.name)
            if column.value_type != ColumnType.NUMBER:
                continue
            if any(term in normalized_name for term in JOB_METRIC_EXCLUDED_TERMS):
                continue
            if not any(term in normalized_name for term in JOB_METRIC_COLUMN_TERMS):
                continue
            candidates.append(column)

        # 同时存在“合计/总计”列和明细列时只采用合计列，避免重复累计。
        total_columns = [
            column
            for column in candidates
            if any(
                term in _normalize_text(column.name)
                for term in ("合计", "总计", "总数", "总人数")
            )
        ]
        selected_columns = total_columns or candidates
        for column in selected_columns:
            plans.append(
                SpreadsheetQueryPlan(
                    sheet_id=sheet.sheet_id,
                    metric=MetricSpec(
                        operation=Aggregation.SUM,
                        column_id=column.column_id,
                        label="岗位总数",
                    ),
                    sort_direction="desc",
                    limit=50,
                )
            )
    return plans


def _asks_for_job_quantity(question: str) -> bool:
    normalized = _normalize_text(question)
    return any(term in normalized for term in JOB_QUANTITY_TERMS)


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


def _person_filter_value(
    question: str,
    *,
    profile: WorkbookProfile,
) -> str | None:
    """从问题中提取人员名称，并禁止把工作簿名称误当作筛选值。

    Profile 只提供受控的列结构和少量样本。优先使用人员语义列中的精确样本；样本未覆盖时，
    必须先移除真实工作簿名称和范围连接词，再使用严格长度的人名规则降级提取。
    """

    normalized = re.sub(r"[\s，。！？、,.!?:：;；“”\"'（）()]+", "", question)
    normalized = _remove_workbook_scope(normalized, filename=profile.filename)
    known_value = _known_person_sample_value(normalized, profile=profile)
    if known_value is not None:
        return known_value

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
    # “某工作簿中金海燕”的范围连接词属于文件定位，不得进入申请人筛选值。
    name = re.split(r"(?:文件|表格|工作簿)?[中内里]", name)[-1]
    name = re.sub(r"^(?:在|从|于|这个|该|上述|上面)+", "", name)
    return name if _is_plausible_person_name(name) else None


def _known_person_sample_value(
    normalized_question: str,
    *,
    profile: WorkbookProfile,
) -> str | None:
    """从人员语义列的少量样本中解析问题明确提到的人员值。"""

    matches: dict[str, str] = {}
    for sheet in profile.sheets:
        for column in sheet.columns:
            normalized_name = _normalize_text(column.name)
            if not any(term in normalized_name for term in PERSON_COLUMN_TERMS):
                continue
            for sample in column.sample_values:
                display_value = str(sample or "").strip()
                normalized_value = _normalize_text(display_value)
                if (
                    _is_plausible_person_name(display_value)
                    and normalized_value
                    and normalized_value in _normalize_text(normalized_question)
                ):
                    matches[normalized_value] = display_value
    if not matches:
        return None
    # 较长姓名优先，避免短样本恰好是另一完整姓名的子串。
    return sorted(matches.items(), key=lambda item: (-len(item[0]), item[0]))[0][1]


def _remove_workbook_scope(question: str, *, filename: str) -> str:
    """移除问题中真实工作簿文件名，保留其后的人员和统计语义。"""

    normalized_filename = _normalize_text(filename)
    filename_stem = re.sub(r"\.[^.]+$", "", str(filename or ""))
    normalized_stem = _normalize_text(filename_stem)
    result = question
    # 先移除完整文件名，再移除不带扩展名的文件名；按长度排序避免只删掉局部。
    for candidate in sorted(
        {normalized_filename, normalized_stem},
        key=len,
        reverse=True,
    ):
        if candidate:
            result = result.replace(candidate, "")
    return result


def _is_plausible_person_name(value: str) -> bool:
    """限制确定性筛选值为合理姓名，长标题必须交由澄清或 LLM 规划。"""

    normalized = str(value or "").strip()
    if "·" in normalized:
        return bool(
            re.fullmatch(
                r"[\u4e00-\u9fffA-Za-z]+(?:·[\u4e00-\u9fffA-Za-z]+)+",
                normalized,
            )
        )
    return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,6}", normalized))


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
    scored = []
    for column in string_columns:
        normalized_name = _normalize_text(column.name)
        sample_match = any(
            _normalize_text(value) == _normalize_text(filter_value)
            for value in column.sample_values
        )
        semantic_score = sum(1 for term in PERSON_COLUMN_TERMS if term in normalized_name)
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
