"""电子表格分析服务入口：Profile → Plan → Validate → Execute。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.modules.llm.client import (
    LLMConfigurationError,
    LLMResponseError,
    OpenAICompatibleLLMClient,
)

from .executor import execute_query
from .profiler import SUPPORTED_SPREADSHEET_SUFFIXES, profile_workbook
from .query_planner import build_query_plan
from .validator import SpreadsheetPlanValidationError, validate_plan


class SpreadsheetAnalysisService:
    """只读表格分析服务；文件路径仅可来自受控存储层。"""

    def __init__(
        self,
        *,
        settings: Settings | Any | None = None,
        client: OpenAICompatibleLLMClient | Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client

    def analyze(
        self,
        *,
        document_id: str,
        filename: str,
        file_path: Path,
        question: str,
    ) -> dict[str, Any]:
        """分析一个已授权的电子表格原件，返回结构化结果。"""

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_SPREADSHEET_SUFFIXES:
            return _failed(
                code="UNSUPPORTED_FILE_TYPE",
                message="当前表格分析仅支持 .xls、.xlsx、.xlsm、.csv 和 .tsv 文件。",
            )

        try:
            profile = profile_workbook(
                document_id=document_id,
                filename=filename,
                file_path=file_path,
            )
        except Exception as exc:
            return _failed(
                code="SPREADSHEET_PROFILE_FAILED",
                message=f"无法读取表格结构：{exc}",
            )

        try:
            plan = build_query_plan(
                client=self._get_client(),
                question=question,
                profile=profile,
            )
        except (LLMConfigurationError, LLMResponseError) as exc:
            return _failed(
                code="SPREADSHEET_PLAN_FAILED",
                message=f"无法生成表格查询计划：{exc}",
            )

        if plan.clarification_required:
            return {
                "kind": "spreadsheet_analysis",
                "ok": True,
                "status": "NEEDS_CLARIFICATION",
                "message": plan.clarification_question,
                "document_id": document_id,
                "filename": filename,
                "available_sheets": [
                    {
                        "sheet_name": sheet.sheet_name,
                        "columns": [column.name for column in sheet.columns],
                    }
                    for sheet in profile.sheets
                ],
            }

        try:
            validated_plan = validate_plan(profile=profile, plan=plan)
        except SpreadsheetPlanValidationError as exc:
            return {
                "kind": "spreadsheet_analysis",
                "ok": True,
                "status": "NEEDS_CLARIFICATION",
                "message": f"我无法确认要使用的表格字段：{exc}",
                "document_id": document_id,
                "filename": filename,
                "available_sheets": [
                    {
                        "sheet_name": sheet.sheet_name,
                        "columns": [column.name for column in sheet.columns],
                    }
                    for sheet in profile.sheets
                ],
            }

        try:
            result = execute_query(
                file_path=file_path,
                profile=profile,
                plan=validated_plan,
            )
        except Exception as exc:
            return _failed(
                code="SPREADSHEET_EXECUTION_FAILED",
                message=f"表格统计执行失败：{exc}",
            )

        result["document_id"] = document_id
        result["filename"] = filename
        return result

    def _get_client(self) -> OpenAICompatibleLLMClient | Any:
        if self.client is not None:
            return self.client
        if not bool(getattr(self.settings, "llm_enabled", False)):
            raise LLMConfigurationError("表格分析需要启用 LLM（LLM_ENABLED=true）。")
        return OpenAICompatibleLLMClient(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_chat_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
        )


def _failed(*, code: str, message: str) -> dict[str, Any]:
    return {
        "kind": "spreadsheet_analysis",
        "ok": False,
        "status": "FAILED",
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "user_action_required": False,
        },
    }
