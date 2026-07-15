"""批量向量投影失败隔离测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Document, DocumentExtractionRun, DocumentPage, GraphProjectionRun
from app.modules.knowledge_graph.embedding import DocumentEmbeddingResult
from app.modules.knowledge_graph.embedding_projection_service import GraphEmbeddingProjectionService


class PartiallyFailingEmbeddingService:
    """第二个文件失败的测试服务。"""

    embedding_version = "test-v2"

    def embed_document(self, *, document_id, **kwargs):
        if document_id == "document-failed":
            raise RuntimeError("embedding failed")
        return DocumentEmbeddingResult(status="COMPLETED", vector=(1.0, 0.0))


def test_embedding_projection_isolates_file_failure_and_records_partial_run():
    """一个文件失败时其他文件仍完成，投影运行标记 PARTIAL。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        for document_id in ["document-ok", "document-failed"]:
            document = Document(
                id=document_id,
                user_id="user-embedding",
                original_filename=f"{document_id}.txt",
                size_bytes=10,
                sha256=("a" if document_id == "document-ok" else "b") * 64,
            )
            extraction = DocumentExtractionRun(
                id=f"run-{document_id}",
                document_id=document_id,
                status="COMPLETED",
            )
            page = DocumentPage(
                document_id=document_id,
                extraction_run_id=extraction.id,
                page_number=1,
                text_content="完整正文",
            )
            db.add_all([document, extraction, page])
        db.flush()

        summary = GraphEmbeddingProjectionService(
            embedding_service=PartiallyFailingEmbeddingService()
        ).sync(db=db)

        assert summary.succeeded == 1
        assert summary.failed == 1
        run = db.query(GraphProjectionRun).one()
        assert run.status == "PARTIAL"
        assert run.items_succeeded == 1
        assert run.items_failed == 1
    finally:
        db.close()
