"""按文件批次生成文档向量的可重试投影服务。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentExtractionRun, DocumentPage
from app.modules.knowledge_graph.embedding import DocumentEmbeddingService
from app.modules.knowledge_graph.projection_runs import GraphProjectionRunRepository


@dataclass(frozen=True, slots=True)
class EmbeddingProjectionSummary:
    """批量向量投影结果。"""

    succeeded: int
    failed: int
    reused: int
    skipped: int


class GraphEmbeddingProjectionService:
    """从 PostgreSQL 完整正文批量生成 Neo4j 文档向量。"""

    def __init__(
        self,
        *,
        embedding_service: DocumentEmbeddingService,
        query_batch_size: int = 500,
    ) -> None:
        self.embedding_service = embedding_service
        self.query_batch_size = max(1, min(5000, query_batch_size))

    def sync(
        self,
        *,
        db: Session,
        document_ids: list[str] | None = None,
        limit: int = 1000,
    ) -> EmbeddingProjectionSummary:
        """逐文件隔离失败，避免一个损坏文件阻断整个批次。"""

        run_repository = GraphProjectionRunRepository(db)
        run = run_repository.create(
            projection_type="DOCUMENT_EMBEDDING",
            scope_type="DOCUMENTS" if document_ids else "PILOT",
            scope_id=",".join(document_ids[:20]) if document_ids else None,
            projection_version=self.embedding_service.embedding_version,
        )
        try:
            summary = self._sync_items(db=db, document_ids=document_ids, limit=limit)
        except Exception as exc:
            run_repository.fail(run, error=exc)
            raise
        run_repository.complete(
            run,
            nodes_written=summary.succeeded - summary.reused,
            relationships_written=0,
            items_succeeded=summary.succeeded,
            items_failed=summary.failed,
        )
        return summary

    def _sync_items(
        self,
        *,
        db: Session,
        document_ids: list[str] | None,
        limit: int,
    ) -> EmbeddingProjectionSummary:
        """选择最新成功解析并执行逐文件失败隔离。"""

        query = (
            db.query(DocumentExtractionRun, Document)
            .join(Document, DocumentExtractionRun.document_id == Document.id)
            .filter(DocumentExtractionRun.status == "COMPLETED")
            .order_by(DocumentExtractionRun.created_at.desc())
        )
        if document_ids:
            query = query.filter(Document.id.in_(document_ids))

        selected: list[tuple[DocumentExtractionRun, Document]] = []
        seen_documents: set[str] = set()
        for extraction_run, document in query.yield_per(self.query_batch_size):
            if document.id in seen_documents:
                continue
            selected.append((extraction_run, document))
            seen_documents.add(document.id)
            if len(selected) >= max(1, limit):
                break

        succeeded = 0
        failed = 0
        reused = 0
        skipped = 0
        for extraction_run, document in selected:
            pages = (
                db.query(DocumentPage)
                .filter(DocumentPage.extraction_run_id == extraction_run.id)
                .order_by(DocumentPage.page_number.asc().nullslast(), DocumentPage.created_at.asc())
                .all()
            )
            full_text = "\n".join(page.text_content for page in pages if page.text_content)
            if not full_text.strip():
                skipped += 1
                continue
            try:
                result = self.embedding_service.embed_document(
                    document_id=document.id,
                    document_version_id=document.id,
                    sha256=document.sha256,
                    filename=document.original_filename,
                    full_text=full_text,
                )
            except Exception:
                failed += 1
                continue
            if result.status == "COMPLETED":
                succeeded += 1
                reused += int(result.reused)
            else:
                failed += 1

        return EmbeddingProjectionSummary(
            succeeded=succeeded,
            failed=failed,
            reused=reused,
            skipped=skipped,
        )
