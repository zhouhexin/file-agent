"""文件模块响应 schema。"""

from __future__ import annotations

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
    filesystem_job_id: str
    archive_status: str
    duplicate_review_status: str


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
