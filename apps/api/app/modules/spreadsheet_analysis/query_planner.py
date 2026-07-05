"""把自然语言问题转换为受控 SpreadsheetQueryPlan。"""

from __future__ import annotations

from pydantic import ValidationError

from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient

from .schemas import SpreadsheetQueryPlan, WorkbookProfile


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
    client: OpenAICompatibleLLMClient,
    question: str,
    profile: WorkbookProfile,
) -> SpreadsheetQueryPlan:
    """调用 LLM 并将输出严格校验为受控查询计划。"""

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
