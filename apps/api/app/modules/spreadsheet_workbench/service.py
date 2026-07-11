"""表格工作台只读服务。

本模块只接收已经过 Tool handler 权限校验的本地原件路径，执行 Profile 和校验。
它不写文件、不保存数据库、不执行宏，后续编辑和重算必须通过单独 Tool 与 OperationPlan。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.cell.cell import Cell

from app.modules.spreadsheet_analysis.conversion import prepared_spreadsheet_path
from app.modules.spreadsheet_analysis.profiler import (
    SUPPORTED_SPREADSHEET_SUFFIXES,
    profile_workbook,
)

from .schemas import FormulaError, SpreadsheetProfileResult, SpreadsheetValidationResult, SpreadsheetWarning


FORMULA_ERROR_VALUES = {
    "#REF!",
    "#DIV/0!",
    "#VALUE!",
    "#NAME?",
    "#N/A",
    "#NUM!",
    "#NULL!",
}


class SpreadsheetWorkbenchService:
    """表格工作台只读能力实现；路径只能来自受控存储解析。"""

    def profile(
        self,
        *,
        document_id: str,
        filename: str,
        file_path: Path,
    ) -> dict[str, Any]:
        """返回工作簿或分隔文本表格的安全结构摘要。"""

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_SPREADSHEET_SUFFIXES:
            return _failed(
                kind="spreadsheet_profile",
                document_id=document_id,
                filename=filename,
                file_type=suffix,
                code="UNSUPPORTED_FILE_TYPE",
                message="当前表格工作台仅支持 .xls、.xlsx、.xlsm、.csv 和 .tsv 文件。",
            )

        try:
            profile = profile_workbook(
                document_id=document_id,
                filename=filename,
                file_path=file_path,
            )
        except Exception as exc:
            return _failed(
                kind="spreadsheet_profile",
                document_id=document_id,
                filename=filename,
                file_type=suffix,
                code="SPREADSHEET_PROFILE_FAILED",
                message=f"无法读取表格结构：{exc}",
            )

        warnings = _macro_warnings(filename=filename, suffix=suffix)
        return SpreadsheetProfileResult(
            document_id=document_id,
            filename=filename,
            file_type=suffix,
            sheets=[
                {
                    "sheet_id": sheet.sheet_id,
                    "sheet_name": sheet.sheet_name,
                    "header_row": sheet.header_row,
                    "row_count": sheet.row_count,
                    "columns": [column.model_dump() for column in sheet.columns],
                }
                for sheet in profile.sheets
            ],
            warnings=warnings,
        ).model_dump()

    def validate(
        self,
        *,
        document_id: str,
        filename: str,
        file_path: Path,
    ) -> dict[str, Any]:
        """扫描公式错误、宏风险和基础结构问题；不重算公式、不保存文件。"""

        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_SPREADSHEET_SUFFIXES:
            return _failed(
                kind="spreadsheet_validation",
                document_id=document_id,
                filename=filename,
                file_type=suffix,
                code="UNSUPPORTED_FILE_TYPE",
                message="当前表格校验仅支持 .xls、.xlsx、.xlsm、.csv 和 .tsv 文件。",
            )

        profile_result = self.profile(
            document_id=document_id,
            filename=filename,
            file_path=file_path,
        )
        if not profile_result.get("ok"):
            return _failed(
                kind="spreadsheet_validation",
                document_id=document_id,
                filename=filename,
                file_type=suffix,
                code="SPREADSHEET_PROFILE_FAILED",
                message=str(profile_result.get("error", {}).get("message") or "无法读取表格结构。"),
            )

        formula_errors: list[FormulaError] = []
        warnings = [
            SpreadsheetWarning.model_validate(item)
            for item in profile_result.get("warnings", [])
            if isinstance(item, dict)
        ]

        if suffix in {".xls", ".xlsx", ".xlsm"}:
            try:
                formula_errors.extend(_scan_excel_formula_errors(file_path=file_path))
            except Exception as exc:
                return _failed(
                    kind="spreadsheet_validation",
                    document_id=document_id,
                    filename=filename,
                    file_type=suffix,
                    code="SPREADSHEET_VALIDATION_FAILED",
                    message=f"无法校验公式错误：{exc}",
                )
        else:
            warnings.append(
                SpreadsheetWarning(
                    code="NO_FORMULA_MODEL",
                    message="CSV/TSV 不保留公式和样式，仅执行结构 Profile 检查。",
                )
            )

        status = "NEEDS_REVIEW" if formula_errors or warnings else "COMPLETED"
        return SpreadsheetValidationResult(
            ok=True,
            status=status,
            document_id=document_id,
            filename=filename,
            file_type=suffix,
            formula_errors=formula_errors,
            warnings=warnings,
            summary={
                "sheet_count": len(profile_result.get("sheets", [])),
                "formula_error_count": len(formula_errors),
                "warning_count": len(warnings),
            },
        ).model_dump()


def _scan_excel_formula_errors(*, file_path: Path) -> list[FormulaError]:
    """用 data_only=False 读取公式文本，避免保存时丢失公式。"""

    with prepared_spreadsheet_path(file_path=file_path) as readable_path:
        workbook = openpyxl.load_workbook(
            filename=readable_path,
            read_only=True,
            data_only=False,
            keep_links=True,
        )
        try:
            errors: list[FormulaError] = []
            for worksheet in workbook.worksheets:
                for row in worksheet.iter_rows():
                    for cell in row:
                        error = _formula_error_from_cell(cell)
                        if error is None:
                            continue
                        errors.append(
                            FormulaError(
                                sheet_name=worksheet.title,
                                cell=cell.coordinate,
                                error=error,
                                formula=str(cell.value or ""),
                            )
                        )
            return errors
        finally:
            workbook.close()


def _formula_error_from_cell(cell: Cell) -> str | None:
    """识别显式错误值，或公式文本中已经包含的错误字面量。"""

    value = cell.value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in FORMULA_ERROR_VALUES:
            return stripped
        if stripped.startswith("="):
            for item in FORMULA_ERROR_VALUES:
                if item in stripped:
                    return item
    return None


def _macro_warnings(*, filename: str, suffix: str) -> list[SpreadsheetWarning]:
    """对 xlsm 文件标记宏风险，但绝不执行宏。"""

    if suffix != ".xlsm":
        return []
    return [
        SpreadsheetWarning(
            code="MACRO_FILE_DETECTED",
            message=f"《{filename}》是含宏工作簿，系统只做只读检查，不执行宏。",
        )
    ]


def _failed(
    *,
    kind: str,
    document_id: str,
    filename: str,
    file_type: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    """构造表格工作台结构化失败输出。"""

    return {
        "kind": kind,
        "ok": False,
        "status": "FAILED",
        "document_id": document_id,
        "filename": filename,
        "file_type": file_type,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "user_action_required": False,
        },
    }
