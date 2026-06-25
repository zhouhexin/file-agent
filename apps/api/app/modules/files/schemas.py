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


class FileDeleteResponse(BaseModel):
    """文件删除成功后的响应。"""

    deleted: bool
