"""文件模块响应 schema。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class FileUploadResponse(BaseModel):
    """文件上传成功后的响应。"""

    document_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    ingest_status: str
    deduplicated: bool = False
    upload_document_version_id: str
    duplicate_review_id: str
    filesystem_job_id: str | None = None
    archive_status: str
    duplicate_review_status: str
    relative_path: str | None = None


class FileDeleteResponse(BaseModel):
    """文件删除成功后的响应。"""

    deleted: bool
    cleanup_job_id: str | None = None


class FilePreviewSection(BaseModel):
    """文件预览中的一个可定位页面或工作表文本区段。"""

    page_number: int | None = None
    sheet_name: str | None = None
    text: str


class FilePreviewResponse(BaseModel):
    """经过权限校验和长度限制的文件正文预览。"""

    document_id: str
    filename: str
    content_type: str
    sections: list[FilePreviewSection] = Field(default_factory=list)
    truncated: bool = False


class SpreadsheetSheetSummary(BaseModel):
    """结构化工作簿中的工作表摘要。"""

    name: str
    row_count: int = 0
    column_count: int = 0
    hidden: bool = False


class SpreadsheetCellPreview(BaseModel):
    """一个带真实坐标的 Excel 单元格事实。"""

    row: int
    column: int
    address: str
    raw_value: Any = None
    display_value: str = ""
    value_type: str
    formula: str | None = None
    cached_result: Any = None
    cached_result_available: bool = False
    number_format: str | None = None
    merge_range: str | None = None


class SpreadsheetSelectedSheet(BaseModel):
    """当前分页区域及工作表结构。"""

    name: str
    row_count: int = 0
    column_count: int = 0
    row_offset: int = 0
    row_limit: int = 100
    column_offset: int = 0
    column_limit: int = 50
    merged_ranges: list[str] = Field(default_factory=list)
    hidden_rows: list[int] = Field(default_factory=list)
    hidden_columns: list[str] = Field(default_factory=list)
    freeze_panes: str | None = None
    structure_complete: bool = True
    cells: list[SpreadsheetCellPreview] = Field(default_factory=list)


class SpreadsheetPreviewResponse(BaseModel):
    """经过权限校验的 Excel 结构化分页预览。"""

    document_id: str
    filename: str
    sheets: list[SpreadsheetSheetSummary] = Field(default_factory=list)
    selected_sheet: SpreadsheetSelectedSheet
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
