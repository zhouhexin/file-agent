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
