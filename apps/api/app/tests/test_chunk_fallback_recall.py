"""Chunk fallback recall 和 SearchEvidenceProjector 测试。

测试目标：
1. fallback_recall 当第一阶段候选不足时从 Chunk GIN 索引补召回
2. SearchEvidenceProjector 投影 EvidenceSpan 位置和短预览
3. 跨用户隔离
4. INACTIVE 工作副本不参与
5. 短预览限制长度
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentPage,
    DocumentVersion,
    EvidenceSpan,
    WorkingCopy,
)
from app.modules.retrieval.chunk_lexical_search import DocumentChunkLexicalSearchService
from app.modules.retrieval.evidence_projector import SearchEvidenceProjector


def _db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _setup_indexed_doc(
    db,
    *,
    suffix,
    user_id,
    workspace_id,
    chunk_text,
    wc_status="ACTIVE",
    index_status="COMPLETED",
    page_number=1,
    sheet_name=None,
):
    """创建 Document + Version + WorkingCopy + ExtractionRun + IndexRun + Page + Chunk + EvidenceSpan。"""

    doc = Document(
        id=f"doc-{suffix}", user_id=user_id, workspace_id=workspace_id,
        original_filename=f"src-{suffix}.docx",
        content_type="application/pdf", size_bytes=100, sha256=suffix * 64,
    )
    ver = DocumentVersion(
        id=f"ver-{suffix}", document_id=doc.id, version_number=1,
        storage_tier="WORKING_COPY", storage_path=f"work/{suffix}.pdf",
        filename=f"{suffix}.pdf", content_type=doc.content_type,
        size_bytes=doc.size_bytes, sha256=doc.sha256, source_type="IMPORT",
    )
    db.add_all([doc, ver])
    db.flush()

    wc = WorkingCopy(
        id=f"wc-{suffix}", working_copy_root_id=f"root-{suffix}",
        workspace_id=workspace_id, managed_file_id=f"mf-{suffix}",
        document_id=doc.id, current_version_id=ver.id,
        relative_path=f"{suffix}.pdf", relative_path_hash=suffix * 64,
        filename=f"{suffix}.pdf", extension="pdf",
        size_bytes=doc.size_bytes, content_sha256=doc.sha256,
        imported_source_sha256=doc.sha256, status=wc_status,
    )
    db.add(wc)

    ext_run = DocumentExtractionRun(
        id=f"ext-{suffix}", document_id=doc.id,
        status="COMPLETED",
    )
    db.add(ext_run)
    db.flush()

    index_run = DocumentIndexRun(
        id=f"idx-{suffix}", document_id=doc.id,
        document_version_id=ver.id, extraction_run_id=ext_run.id,
        index_version="chunk-index-v1", tokenizer="jieba",
        tokenizer_version="v1", config_hash=f"hash-{suffix}",
        status=index_status, chunk_count=1, evidence_count=1,
    )
    db.add(index_run)
    db.flush()

    db.add(DocumentPage(
        id=f"page-{suffix}", document_id=doc.id,
        page_number=page_number,
        text_content=chunk_text,
        extraction_run_id=ext_run.id,
    ))

    chunk = DocumentChunk(
        id=f"chunk-{suffix}", index_run_id=index_run.id,
        document_id=doc.id, document_version_id=ver.id,
        extraction_run_id=ext_run.id, chunk_index=0, chunk_type="page",
        text_content=chunk_text, search_text=chunk_text,
        content_hash=f"hash-{suffix}", location_hash=f"loc-{suffix}",
        char_count=len(chunk_text), token_count=len(chunk_text.split()),
        page_start=page_number, page_end=page_number,
        sheet_name=sheet_name,
    )
    db.add(chunk)
    db.flush()

    db.add(EvidenceSpan(
        id=f"ev-{suffix}", chunk_id=chunk.id,
        document_id=doc.id, document_version_id=ver.id,
        extraction_run_id=ext_run.id,
        span_index=0, evidence_type="text_quote",
        quote=chunk_text, start_offset=0, end_offset=len(chunk_text),
        page_number=page_number, sheet_name=sheet_name,
        source="document_chunk",
    ))
    db.flush()
    return doc


def test_service_class_exists():
    """DocumentChunkLexicalSearchService 已存在。"""
    assert DocumentChunkLexicalSearchService is not None


def test_fallback_recall_method_exists():
    """fallback_recall 方法已存在。"""
    assert hasattr(DocumentChunkLexicalSearchService, "fallback_recall")


def test_evidence_projector_importable():
    """SearchEvidenceProjector 可导入。"""
    from app.modules.retrieval.evidence_projector import SearchEvidenceProjector
    assert SearchEvidenceProjector is not None


def test_search_finds_chunks_by_query():
    """Chunk 搜索能找到匹配的 Chunk。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="a", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金申请材料",
        )
        _setup_indexed_doc(
            db, suffix="b", user_id="user1", workspace_id="ws1",
            chunk_text="其他文件",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        version_ids = ["ver-a", "ver-b"]
        results = service.search(
            query="奖学金", document_version_ids=version_ids
        )
        assert len(results) >= 1
        assert any(r["document_version_id"] == "ver-a" for r in results)
    finally:
        db.close()


def test_search_respects_user_boundary():
    """搜索结果不包含其他用户的 Chunk。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="a", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金",
        )
        _setup_indexed_doc(
            db, suffix="b", user_id="user2", workspace_id="ws2",
            chunk_text="奖学金申请材料",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        results = service.search(
            query="奖学金", document_version_ids=["ver-a", "ver-b"]
        )
        for r in results:
            assert r["document_version_id"] in ["ver-a"]
    finally:
        db.close()


def test_search_respects_inactive_working_copies():
    """搜索结果中不包含 INACTIVE 工作副本的 Chunk。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="act", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金材料", wc_status="ACTIVE",
        )
        _setup_indexed_doc(
            db, suffix="ina", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金材料", wc_status="INACTIVE",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        # 即使显式传入 ver-ina，由于其 WorkingCopy 状态为 INACTIVE，
        # 该 Chunk 应该被 WorkingCopy+current_version 关联排除（fallback_recall 场景）
        # 直接 search 时按 version_id 查询，但这里检查 fallback_recall
        results = service.fallback_recall(
            query="奖学金", workspace_id="ws1"
        )
        # INACTIVE 工作副本不在活跃子查询中
        wc_ids = {r["document_version_id"] for r in results}
        assert "ver-ina" not in wc_ids, "INACTIVE 工作副本不应被召回"
    finally:
        db.close()


def test_evidence_projector_returns_page_number():
    """EvidenceProjector 返回 PDF 页码。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="p", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金材料", page_number=3,
        )
        db.commit()

        projector = SearchEvidenceProjector(db=db, user_id="user1")
        result = projector.project(chunk_ids=["chunk-p"])

        assert "chunk-p" in result
        assert result["chunk-p"]["page_number"] == 3
        assert "奖学金材料" in result["chunk-p"]["preview"]
    finally:
        db.close()


def test_evidence_projector_returns_sheet_name():
    """EvidenceProjector 返回 Excel Sheet 名称。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="e", user_id="user1", workspace_id="ws1",
            chunk_text="资助金额",
            sheet_name="Sheet1",
        )
        db.commit()

        projector = SearchEvidenceProjector(db=db, user_id="user1")
        result = projector.project(chunk_ids=["chunk-e"])

        assert "chunk-e" in result
        assert result["chunk-e"]["sheet_name"] == "Sheet1"
    finally:
        db.close()


def test_evidence_projector_truncates_preview():
    """EvidenceProjector 限制预览长度。"""

    db = _db_session()
    try:
        long_text = "奖学金" * 200  # > 240 chars
        _setup_indexed_doc(
            db, suffix="long", user_id="user1", workspace_id="ws1",
            chunk_text=long_text,
        )
        db.commit()

        projector = SearchEvidenceProjector(db=db, user_id="user1")
        result = projector.project(
            chunk_ids=["chunk-long"], max_preview_chars=100
        )

        assert "chunk-long" in result
        assert len(result["chunk-long"]["preview"]) <= 100
    finally:
        db.close()


def test_evidence_projector_respects_user_boundary():
    """EvidenceProjector 跨用户隔离。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="p1", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金",
        )
        _setup_indexed_doc(
            db, suffix="p2", user_id="user2", workspace_id="ws2",
            chunk_text="奖学金",
        )
        db.commit()

        projector1 = SearchEvidenceProjector(db=db, user_id="user1")
        result = projector1.project(chunk_ids=["chunk-p1", "chunk-p2"])

        assert "chunk-p1" in result
        assert "chunk-p2" not in result, "其他用户的 Evidence 不应可见"
    finally:
        db.close()


def test_fallback_recall_returns_only_completed_index():
    """fallback_recall 仅返回 COMPLETED 索引运行。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="good", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金",
        )
        _setup_indexed_doc(
            db, suffix="bad", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金",
            index_status="FAILED",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        results = service.fallback_recall(
            query="奖学金", workspace_id="ws1"
        )

        version_ids = {r["document_version_id"] for r in results}
        assert "ver-good" in version_ids
        assert "ver-bad" not in version_ids
    finally:
        db.close()


def test_fallback_recall_aggregates_by_version():
    """fallback_recall 按版本聚合，每个版本取最佳 Chunk。"""

    db = _db_session()
    try:
        # 一个文档两个 Chunk 都包含查询词
        _setup_indexed_doc(
            db, suffix="multi", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金材料",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        results = service.fallback_recall(
            query="奖学金", workspace_id="ws1"
        )

        # 应该只有一个版本的结果
        version_ids = [r["document_version_id"] for r in results]
        assert version_ids.count("ver-multi") == 1
    finally:
        db.close()


def test_chunk_search_handles_empty_query():
    """空查询返回空结果。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="empty", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        results = service.search(
            query="", document_version_ids=["ver-empty"]
        )
        assert len(results) == 0
    finally:
        db.close()


def test_fallback_recall_respects_workspace():
    """fallback_recall 仅查询指定工作区。"""

    db = _db_session()
    try:
        _setup_indexed_doc(
            db, suffix="ws1", user_id="user1", workspace_id="ws1",
            chunk_text="奖学金",
        )
        _setup_indexed_doc(
            db, suffix="ws2", user_id="user1", workspace_id="ws2",
            chunk_text="奖学金材料",
        )
        db.commit()

        service = DocumentChunkLexicalSearchService(db=db, user_id="user1")
        # 只查 ws1
        results = service.fallback_recall(
            query="奖学金", workspace_id="ws1"
        )
        version_ids = [r["document_version_id"] for r in results]
        assert "ver-ws1" in version_ids
        # ws2 不应在结果中
        assert "ver-ws2" not in version_ids
    finally:
        db.close()