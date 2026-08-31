"""电子表格分析服务入口：Profile → Plan → Validate → Execute。"""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import Settings, get_settings
from app.modules.llm.client import (
    LLMConfigurationError,
    LLMResponseError,
    OpenAICompatibleLLMClient,
)

from .executor import execute_query
from .profiler import SUPPORTED_SPREADSHEET_SUFFIXES, profile_workbook
from .query_planner import build_query_plans
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
            plans = build_query_plans(
                client=self._get_client(),
                question=question,
                profile=profile,
            )
        except (LLMConfigurationError, LLMResponseError) as exc:
            return _failed(
                code="SPREADSHEET_PLAN_FAILED",
                message=f"无法生成表格查询计划：{exc}",
            )

        if len(plans) == 1 and plans[0].clarification_required:
            return {
                "kind": "spreadsheet_analysis",
                "ok": True,
                "status": "NEEDS_CLARIFICATION",
                "message": plans[0].clarification_question,
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
            validated_plans = [
                validate_plan(profile=profile, plan=plan)
                for plan in plans
            ]
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
            sheet_results = [
                execute_query(
                    file_path=file_path,
                    profile=profile,
                    plan=validated_plan,
                )
                for validated_plan in validated_plans
            ]
            result = _combine_sheet_results(sheet_results)
            result = _normalize_requested_remediation_statuses(
                result=result,
                question=question,
            )
        except Exception as exc:
            return _failed(
                code="SPREADSHEET_EXECUTION_FAILED",
                message=f"表格统计执行失败：{exc}",
            )

        result["document_id"] = document_id
        result["filename"] = filename
        return result

    def _get_client(self) -> OpenAICompatibleLLMClient | Any | None:
        """返回可选 LLM 客户端；简单确定性查询在关闭 LLM 时仍可执行。"""

        if self.client is not None:
            return self.client
        if not bool(getattr(self.settings, "llm_enabled", False)):
            return None
        return OpenAICompatibleLLMClient(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_chat_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
        )


def _combine_sheet_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """合并多个结构兼容 Sheet 的确定性求和结果，并保留逐 Sheet 依据。"""

    if len(results) == 1:
        return results[0]
    if not results:
        raise ValueError("没有可执行的工作表查询计划。")

    first = results[0]
    operation = str((first.get("metric") or {}).get("operation") or "")
    if operation != "sum":
        raise ValueError("当前多工作表合并仅支持求和。")

    total = Decimal("0")
    sheet_breakdown: list[dict[str, Any]] = []
    for result in results:
        rows = [
            item for item in result.get("results", [])
            if isinstance(item, dict)
        ]
        value = _decimal_value(rows[0].get("value")) if rows else Decimal("0")
        total += value
        sheet_breakdown.append(
            {
                "sheet_name": result.get("sheet_name"),
                "metric_column_name": (result.get("metric") or {}).get("column_name"),
                "value": _format_decimal(value),
                "rows_matched": int(result.get("rows_matched") or 0),
                "evidence_items": list(result.get("evidence_items") or []),
            }
        )

    sheet_names = list(
        dict.fromkeys(
            str(item.get("sheet_name") or "")
            for item in results
            if str(item.get("sheet_name") or "").strip()
        )
    )
    return {
        "kind": "spreadsheet_analysis",
        "ok": True,
        "status": "COMPLETED",
        "sheet_name": sheet_names[0] if len(sheet_names) == 1 else "多个工作表",
        "metric": dict(first.get("metric") or {}),
        "group_by": None,
        "filters": list(first.get("filters") or []),
        "rows_scanned": sum(int(item.get("rows_scanned") or 0) for item in results),
        "rows_matched": sum(int(item.get("rows_matched") or 0) for item in results),
        "rows_included": sum(int(item.get("rows_included") or 0) for item in results),
        "rows_ignored": sum(int(item.get("rows_ignored") or 0) for item in results),
        "results": (
            [{"group": "全部", "value": _format_decimal(total)}]
            if any(int(item.get("rows_matched") or 0) for item in results)
            else []
        ),
        "sheet_breakdown": sheet_breakdown,
        "evidence_items": [
            evidence
            for item in results
            for evidence in item.get("evidence_items", [])
            if isinstance(evidence, dict)
        ],
        "warnings": list(
            dict.fromkeys(
                warning
                for item in results
                for warning in item.get("warnings", [])
                if str(warning).strip()
            )
        ),
    }


def _decimal_value(value: Any) -> Decimal:
    """把执行器的格式化数值恢复为 Decimal，供多 Sheet 精确汇总。"""

    try:
        return Decimal(str(value or "0").replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("工作表聚合结果不是有效数值。") from exc


def _format_decimal(value: Decimal) -> str:
    """使用稳定十进制文本输出合并结果。"""

    if value == value.to_integral_value():
        return str(int(value))
    return format(value.normalize(), "f")


def _normalize_requested_remediation_statuses(
    *,
    result: dict[str, Any],
    question: str,
) -> dict[str, Any]:
    """按用户明确要求补齐审计整改状态，并统一业务同义口径。"""

    requested_aliases = [
        ("已整改", ("已整改", "整改完成")),
        ("未整改", ("未整改",)),
        ("持续整改", ("持续整改", "正在整改", "整改中")),
    ]
    requested = [
        (label, aliases)
        for label, aliases in requested_aliases
        if label in question or any(alias in question for alias in aliases)
    ]
    group_by = result.get("group_by") if isinstance(result.get("group_by"), dict) else {}
    group_column_name = str(group_by.get("column_name") or "")
    metric = result.get("metric") if isinstance(result.get("metric"), dict) else {}
    if (
        len(requested) < 2
        or "整改" not in question
        or "整改" not in group_column_name
        or str(metric.get("operation") or "") != "count_rows"
    ):
        return result

    source_rows = [
        item for item in result.get("results", [])
        if isinstance(item, dict)
    ]
    normalized_rows: list[dict[str, str]] = []
    matched_indexes: set[int] = set()
    for label, aliases in requested:
        total = Decimal("0")
        for index, item in enumerate(source_rows):
            group = "".join(str(item.get("group") or "").split())
            if any(group.startswith("".join(alias.split())) for alias in aliases):
                total += _decimal_value(item.get("value"))
                matched_indexes.add(index)
        normalized_rows.append({"group": label, "value": _format_decimal(total)})

    normalized_rows.extend(
        {
            "group": str(item.get("group") or ""),
            "value": _format_decimal(_decimal_value(item.get("value"))),
        }
        for index, item in enumerate(source_rows)
        if index not in matched_indexes
    )
    normalized_result = {**result, "results": normalized_rows}
    if any(label == "持续整改" for label, _aliases in requested) and (
        "正在整改" in group_column_name or "整改中" in group_column_name
    ):
        normalized_result["warnings"] = list(
            dict.fromkeys(
                [
                    *list(result.get("warnings") or []),
                    "“持续整改”按表内“正在整改/整改中”口径统计。",
                ]
            )
        )
    return normalized_result


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
