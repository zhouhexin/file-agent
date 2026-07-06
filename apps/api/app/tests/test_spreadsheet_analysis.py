"""表格分析链路测试，保护受控查询计划、确定性执行和 Planner 路由边界。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

from app.modules.agent.planner import DeterministicPlanner
from app.modules.spreadsheet_analysis.executor import execute_query
from app.modules.spreadsheet_analysis.profiler import profile_workbook
from app.modules.spreadsheet_analysis.schemas import SpreadsheetQueryPlan
from app.modules.spreadsheet_analysis.service import SpreadsheetAnalysisService
from app.modules.spreadsheet_analysis.validator import SpreadsheetPlanValidationError, validate_plan


class FakeJsonClient:
    """测试用 LLM JSON 客户端，固定返回受控查询计划。"""

    def __init__(self, response: dict) -> None:
        """保存测试指定的模型响应。"""

        self.response = response

    def complete_json(self, *, system_prompt: str, user_payload: dict) -> dict:
        """返回固定 JSON，避免测试依赖真实外部模型。"""

        return self.response


def _make_workbook(tmp_path: Path) -> Path:
    """创建包含人员、论文类型和资助金额的临时工作簿。"""

    path = tmp_path / "科研成果汇总.xlsx"
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "汇总表"
    worksheet.append(["申请人", "论文类型", "资助类别", "资助金额"])
    worksheet.append(["张三", "CCF A类论文", "重点", 1000])
    worksheet.append(["李四", "CCF A类论文", "重点", 2000])
    worksheet.append(["王五", "核心期刊论文", "一般", 500])
    workbook.save(path)
    return path


def _profile(path: Path):
    """读取临时工作簿 Profile，用于后续计划校验和执行。"""

    return profile_workbook(
        document_id="doc-1",
        filename=path.name,
        file_path=path,
    )


def test_grouped_sum_executes_without_business_keyword_rules(tmp_path: Path) -> None:
    """分组求和必须由受控计划和执行器完成，不能依赖业务关键词硬编码。"""

    path = _make_workbook(tmp_path)
    profile = _profile(path)
    plan = SpreadsheetQueryPlan.model_validate(
        {
            "sheet_id": "sheet_1",
            "metric": {
                "operation": "sum",
                "column_id": "sheet_1_col_4",
                "label": "资助金额合计",
            },
            "group_by_column_id": "sheet_1_col_2",
            "filters": [],
            "sort_direction": "desc",
            "limit": 50,
        }
    )

    result = execute_query(
        file_path=path,
        profile=profile,
        plan=validate_plan(profile=profile, plan=plan),
    )

    assert result["status"] == "COMPLETED"
    assert result["rows_scanned"] == 3
    assert result["rows_included"] == 3
    assert result["results"] == [
        {"group": "CCF A类论文", "value": "3000"},
        {"group": "核心期刊论文", "value": "500"},
    ]


def test_count_rows_with_filter(tmp_path: Path) -> None:
    """带筛选条件的计数必须只扫描受控列和受控操作。"""

    path = _make_workbook(tmp_path)
    profile = _profile(path)
    plan = SpreadsheetQueryPlan.model_validate(
        {
            "sheet_id": "sheet_1",
            "metric": {"operation": "count_rows", "label": "论文数量"},
            "filters": [
                {
                    "column_id": "sheet_1_col_2",
                    "operator": "equals",
                    "value": "CCF A类论文",
                }
            ],
        }
    )

    result = execute_query(
        file_path=path,
        profile=profile,
        plan=validate_plan(profile=profile, plan=plan),
    )

    assert result["results"] == [{"group": "全部", "value": "2"}]
    assert result["rows_matched"] == 2


def test_tsv_sum_executes_with_same_query_pipeline(tmp_path: Path) -> None:
    """TSV 必须复用统一表格分析链路，而不是退回普通文本读取。"""

    path = tmp_path / "资助汇总.tsv"
    path.write_text("教师\t资助金额\n张三\t100\n李四\t200\n", encoding="utf-8")
    profile = _profile(path)
    plan = SpreadsheetQueryPlan.model_validate(
        {
            "sheet_id": "sheet_1",
            "metric": {
                "operation": "sum",
                "column_id": "sheet_1_col_2",
                "label": "资助金额合计",
            },
        }
    )

    result = execute_query(
        file_path=path,
        profile=profile,
        plan=validate_plan(profile=profile, plan=plan),
    )

    assert result["status"] == "COMPLETED"
    assert result["results"] == [{"group": "全部", "value": "300"}]


def test_validator_rejects_hallucinated_column_id(tmp_path: Path) -> None:
    """校验器必须拒绝不存在的 column_id，防止 LLM 编造字段。"""

    path = _make_workbook(tmp_path)
    profile = _profile(path)
    plan = SpreadsheetQueryPlan.model_validate(
        {
            "sheet_id": "sheet_1",
            "metric": {"operation": "sum", "column_id": "sheet_1_col_999"},
        }
    )

    with pytest.raises(SpreadsheetPlanValidationError):
        validate_plan(profile=profile, plan=plan)


def test_service_returns_clarification_without_executing(tmp_path: Path) -> None:
    """LLM 要求澄清时服务只能返回可选字段，不能继续执行表格查询。"""

    path = _make_workbook(tmp_path)
    service = SpreadsheetAnalysisService(
        settings=SimpleNamespace(llm_enabled=True),
        client=FakeJsonClient(
            {
                "clarification_required": True,
                "clarification_question": "你希望按哪一列汇总？",
            }
        ),
    )

    result = service.analyze(
        document_id="doc-1",
        filename=path.name,
        file_path=path,
        question="汇总成果",
    )

    assert result["ok"] is True
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "论文类型" in result["available_sheets"][0]["columns"]


def test_deterministic_planner_routes_uploaded_xlsx_to_spreadsheet_tool() -> None:
    """上传 Excel 后的统计请求必须路由到只读表格分析 Tool。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-1",
        user_id="user-1",
        message_id="message-1",
        message="按论文类型统计成果数量",
        attachments=[{"document_id": "doc-1", "filename": "科研成果汇总.xlsx"}],
    )

    assert plan.intent == "ANALYZE_SPREADSHEET"
    assert plan.steps[0].tool_name == "analyze-spreadsheet"
