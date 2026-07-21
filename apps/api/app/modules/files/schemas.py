"""文件模块响应 schema。"""

from __future__ import annotations

from pydantic import BaseModel


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
