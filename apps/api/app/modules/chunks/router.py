"""文档 Chunk 安全查询 API。

普通用户只能查看自己的定位元数据和计数；正文、search_text、search_vector 与 embedding 永不返回。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import Document, DocumentChunk, DocumentIndexRun, DocumentVersion, EvidenceSpan, User
from app.modules.auth.dependencies import get_current_user
from app.modules.chunks.schemas import DocumentChunkMetadata, DocumentChunksResponse


router = APIRouter(prefix="/api/documents", tags=["document-index"])


@router.get("/{document_id}/chunks", response_model=DocumentChunksResponse)
def list_document_chunks(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentChunksResponse:
    """返回当前用户文档最新内容版本的索引定位概览，不暴露任何正文派生内容。"""

    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.user_id == current_user.id)
        .one_or_none()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    version = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.version_number.desc(), DocumentVersion.created_at.desc())
        .first()
    )
    if version is None:
        return DocumentChunksResponse(
            document_id=document.id,
            status="NOT_INDEXED",
            embedding_status="DISABLED",
            chunk_count=0,
            evidence_count=0,
            chunks=[],
        )
    index_run = (
        db.query(DocumentIndexRun)
        .filter(
            DocumentIndexRun.document_version_id == version.id,
            DocumentIndexRun.status == "COMPLETED",
        )
        .order_by(DocumentIndexRun.updated_at.desc())
        .first()
    )
    if index_run is None:
        return DocumentChunksResponse(
            document_id=document.id,
            document_version_id=version.id,
            status="NOT_INDEXED",
            embedding_status="DISABLED",
            chunk_count=0,
            evidence_count=0,
            chunks=[],
        )
    evidence_counts = dict(
        db.query(EvidenceSpan.chunk_id, func.count(EvidenceSpan.id))
        .join(DocumentChunk, DocumentChunk.id == EvidenceSpan.chunk_id)
        .filter(DocumentChunk.index_run_id == index_run.id)
        .group_by(EvidenceSpan.chunk_id)
        .all()
    )
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.index_run_id == index_run.id)
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    return DocumentChunksResponse(
        document_id=document.id,
        document_version_id=version.id,
        status=index_run.status,
        embedding_status=index_run.embedding_status,
        chunk_count=index_run.chunk_count,
        evidence_count=index_run.evidence_count,
        chunks=[
            DocumentChunkMetadata(
                chunk_id=chunk.id,
                chunk_index=chunk.chunk_index,
                chunk_type=chunk.chunk_type,
                char_count=chunk.char_count,
                token_count=chunk.token_count,
                page_start=chunk.page_start,
                page_end=chunk.page_end,
                sheet_name=chunk.sheet_name,
                cell_range=chunk.cell_range,
                evidence_count=int(evidence_counts.get(chunk.id, 0)),
            )
            for chunk in chunks
        ],
    )

