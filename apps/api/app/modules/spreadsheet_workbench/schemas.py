"""表格工作台的结构化输出 schema。

这些 schema 只描述 Tool 可返回的安全摘要，不包含本地路径、密钥或完整表格正文。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StrictWorkbenchModel(BaseModel):
    """表格工作台内部 schema 基类，拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class FormulaError(StrictWorkbenchModel):
    """公式或单元格错误的定位信息。"""

    sheet_name: str = Field(min_length=1)
    cell: str = Field(min_length=1)
    error: str = Field(min_length=1)
    formula: str = ""


class SpreadsheetWarning(StrictWorkbenchModel):
    """表格质量检查中的非阻断告警。"""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    sheet_name: str | None = None
    cell: str | None = None


class SpreadsheetProfileResult(StrictWorkbenchModel):
    """profile-spreadsheet Tool 的输出。"""

    kind: str = "spreadsheet_profile"
    ok: bool = True
    status: str = "COMPLETED"
    document_id: str
    filename: str
    file_type: str
    sheets: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[SpreadsheetWarning] = Field(default_factory=list)


class SpreadsheetValidationResult(StrictWorkbenchModel):
    """validate-spreadsheet Tool 的输出。"""

    kind: str = "spreadsheet_validation"
    ok: bool = True
    status: str = "COMPLETED"
    document_id: str
    filename: str
    file_type: str
    formula_errors: list[FormulaError] = Field(default_factory=list)
    warnings: list[SpreadsheetWarning] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
