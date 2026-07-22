"""普通用户可见的原文索引安全响应模型。"""

from __future__ import annotations

from pydantic import BaseModel


class DocumentChunkMetadata(BaseModel):
    """不包含正文、分词词项和向量的 Chunk 定位元数据。"""

    chunk_id: str
    chunk_index: int
    chunk_type: str
    char_count: int
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    evidence_count: int


class DocumentChunksResponse(BaseModel):
    """当前文档版本的安全索引概览。"""

    document_id: str
    document_version_id: str | None = None
    status: str
    embedding_status: str
    chunk_count: int
    evidence_count: int
    chunks: list[DocumentChunkMetadata]

