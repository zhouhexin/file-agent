"""按 Chunk ID 读取已持久化 Evidence 并校验权限。

只返回位置和受限短预览，不返回完整正文。
阶段四的"为什么推荐这个文件"短预览由本服务提供，不代表阶段五的正式事实回答。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentChunk, DocumentIndexRun, EvidenceSpan, WorkingCopy


class SearchEvidenceProjector:
    """按 Chunk ID 投影 Evidence 位置和短预览。

    不返回完整正文，只返回：
    - PDF：页码
    - Word/TXT/MD：页或段落定位
    - Excel：Sheet 和单元格范围
    - 受限预览（最长 RETRIEVAL_PREVIEW_MAX_CHARS 字符）
    """

    def __init__(self, *, db: Session, user_id: str, workspace_id: str | None = None) -> None:
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id

    def project(
        self,
        *,
        chunk_ids: list[str],
        max_preview_chars: int = 240,
    ) -> dict[str, dict[str, Any]]:
        """读取 EvidenceSpan，返回 {chunk_id: {page_number, sheet_name, cell_range, preview}}。

        跨用户隔离：只返回当前用户 Document 的 Evidence。
        """
        if not chunk_ids:
            return {}

        # 一次批量查询，包含用户权限校验
        rows = (
            self.db.query(EvidenceSpan)
            .join(Document, Document.id == EvidenceSpan.document_id)
            .join(DocumentChunk, DocumentChunk.id == EvidenceSpan.chunk_id)
            .join(DocumentIndexRun, DocumentIndexRun.id == DocumentChunk.index_run_id)
            .join(
                WorkingCopy,
                (WorkingCopy.document_id == EvidenceSpan.document_id)
                & (WorkingCopy.current_version_id == EvidenceSpan.document_version_id),
            )
            .filter(
                EvidenceSpan.chunk_id.in_(chunk_ids),
                Document.user_id == self.user_id,
                WorkingCopy.status == "ACTIVE",
                DocumentIndexRun.status == "COMPLETED",
            )
            .filter(
                WorkingCopy.workspace_id == self.workspace_id
                if self.workspace_id is not None
                else True
            )
            .order_by(
                EvidenceSpan.chunk_id.asc(),
                EvidenceSpan.span_index.asc(),
            )
            .all()
        )

        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row.chunk_id in result:
                continue
            result[row.chunk_id] = {
                "page_number": row.page_number,
                "sheet_name": row.sheet_name,
                "cell_range": row.cell_range,
                "preview": (row.quote or "")[:max_preview_chars],
                "evidence_type": row.evidence_type,
            }

        return result
