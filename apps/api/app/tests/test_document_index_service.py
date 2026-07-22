"""阶段三 DocumentVersion 原文索引测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentPage,
    DocumentVersion,
    EvidenceSpan,
    User,
    Workspace,
)
from app.modules.chunks.service import DocumentIndexService
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.retrieval.chunk_lexical_search import DocumentChunkLexicalSearchService
from app.tests.helpers import clear_overrides, client_with_database


def _settings(**overrides) -> Settings:
    """构造默认关闭 embedding 的 CPU-only 测试配置。"""

    return Settings(database_url="postgresql://unused", **overrides)


def _database() -> Session:
    """创建隔离 SQLite 持久化库，验证模型不依赖 PostgreSQL 才能执行单元测试。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _document_facts(db: Session, *, pages: list[dict], suffix: str = ".pdf") -> tuple[Document, DocumentVersion, DocumentExtractionRun]:
    """写入一个成功解析的不可变内容版本。"""

    user = User(username=f"owner-{suffix}", password_hash="hash", display_name="索引测试")
    db.add(user)
    db.flush()
    workspace = Workspace(name="default", owner_id=user.id, is_default=True)
    db.add(workspace)
    db.flush()
    document = Document(
        user_id=user.id,
        workspace_id=workspace.id,
        original_filename=f"测试文件{suffix}",
        content_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        storage_path=f"working/test{suffix}",
        filename=f"测试文件{suffix}",
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        source_type="IMPORT",
        created_by=user.id,
    )
    db.add(version)
    db.flush()
    extraction = DocumentExtractionRun(
        document_id=document.id,
        document_version_id=version.id,
        status="COMPLETED",
        extractor="test",
        parser_name="deterministic",
        parser_version="1",
        parser_config_hash="parser-config-v1",
    )
    db.add(extraction)
    db.flush()
    for page in pages:
        db.add(
            DocumentPage(
                document_id=document.id,
                extraction_run_id=extraction.id,
                page_number=page.get("page_number"),
                sheet_name=page.get("sheet_name"),
                text_content=page["text"],
                metadata_json=page.get("metadata", {}),
            )
        )
    db.flush()
    return document, version, extraction


def test_pdf_index_is_idempotent_and_preserves_real_pages():
    """同一版本和解析配置必须复用索引，PDF Evidence 必须来自真实页码。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[
                {"page_number": 1, "text": "国家励志奖学金申请条件。申请人须提交成绩单。"},
                {"page_number": 2, "text": "材料提交截止日期为2026年9月10日。"},
            ],
        )
        service = DocumentIndexService(db=db, settings=_settings())
        created = service.build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )
        reused = service.build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        assert created["ok"] is True
        assert created["embedding_status"] == "DISABLED"
        assert reused["reused"] is True
        assert reused["index_run_id"] == created["index_run_id"]
        assert db.query(DocumentIndexRun).count() == 1
        assert db.query(DocumentChunk).count() == 2
        evidence = db.query(EvidenceSpan).order_by(EvidenceSpan.page_number).all()
        assert [item.page_number for item in evidence] == [1, 2]
        assert all(item.quote in db.get(DocumentChunk, item.chunk_id).text_content for item in evidence)
        assert all(item.embedding_status == "DISABLED" and item.embedding is None for item in db.query(DocumentChunk))

        invocation = ToolRegistry(db=db, user_id=document.user_id).invoke(
            "chunk-build", {"document_id": document.id}
        )
        assert invocation.status == "COMPLETED"
        assert invocation.output_json["reused"] is True
        assert "text_content" not in invocation.output_json
    finally:
        db.close()


def test_force_reprocess_preserves_completed_chunk_and_evidence_ids():
    """同一解析事实的强制请求必须复用完成索引，避免破坏历史回答引用。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": "稳定引用不能因重复处理失效。"}],
        )
        service = DocumentIndexService(db=db, settings=_settings())
        first = service.build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )
        chunk_id = db.query(DocumentChunk.id).scalar()
        evidence_id = db.query(EvidenceSpan.id).scalar()

        repeated = service.build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
            force_reprocess=True,
        )

        assert repeated["reused"] is True
        assert repeated["index_run_id"] == first["index_run_id"]
        assert db.query(DocumentChunk.id).scalar() == chunk_id
        assert db.query(EvidenceSpan.id).scalar() == evidence_id
    finally:
        db.close()


def test_index_failure_hides_document_text_and_internal_path():
    """索引异常不得把正文、SQL 参数或服务器路径写入 Tool 结果和审计字段。"""

    class LeakingTokenizer:
        """模拟把敏感输入拼进异常消息的第三方分词器。"""

        name = "leaking-test"
        version = "1"

        def tokenize(self, text: str) -> list[str]:
            """抛出包含正文和路径的异常，验证服务会统一脱敏。"""

            raise RuntimeError(f"敏感正文={text}; path=/srv/private/source.pdf")

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": "不得泄漏的学生个人信息"}],
        )
        result = DocumentIndexService(
            db=db,
            settings=_settings(),
            tokenizer=LeakingTokenizer(),
        ).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        run = db.query(DocumentIndexRun).one()
        serialized = f"{result!r} {run.error_message}"
        assert result["ok"] is False
        assert "不得泄漏" not in serialized
        assert "/srv/private" not in serialized
        assert run.error_message == "原文索引建立失败，内部异常详情已隐藏。"
        assert db.query(DocumentChunk).count() == 0
        assert db.query(EvidenceSpan).count() == 0
    finally:
        db.close()


def test_excel_index_preserves_sheet_and_cell_ranges():
    """表格 Chunk 必须保存真实 Sheet 和行坐标，不能只返回模糊页码。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            suffix=".xlsx",
            pages=[
                {
                    "page_number": 1,
                    "sheet_name": "获奖名单",
                    "text": "姓名\t奖项\t金额\n张三\t国家奖学金\t8000\n李四\t励志奖学金\t5000",
                    "metadata": {
                        "line_cell_ranges": [
                            {"line_index": 0, "cell_range": "A1:C1"},
                            {"line_index": 1, "cell_range": "A2:C2"},
                            {"line_index": 2, "cell_range": "A3:C3"},
                        ]
                    },
                }
            ],
        )
        result = DocumentIndexService(db=db, settings=_settings()).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        assert result["ok"] is True
        chunk = db.query(DocumentChunk).one()
        evidence = db.query(EvidenceSpan).one()
        assert chunk.sheet_name == "获奖名单"
        assert chunk.cell_range == "A1:C3"
        assert evidence.evidence_type == "table_cell_range"
        assert evidence.sheet_name == "获奖名单"
        assert evidence.cell_range == "A1:C3"

        results = DocumentChunkLexicalSearchService(db=db, user_id=document.user_id).search(
            query="励志奖学金金额",
            document_version_ids=[version.id],
        )
        assert results[0]["chunk_id"] == chunk.id
        assert results[0]["sheet_name"] == "获奖名单"
        assert "text" not in results[0]
    finally:
        db.close()


def test_rename_reuses_index_but_new_content_version_gets_new_run():
    """路径变化不得重复索引；新内容版本和新解析事实必须建立独立索引。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": "第一版正文"}],
        )
        service = DocumentIndexService(db=db, settings=_settings())
        first = service.build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )
        version.filename = "重命名后的文件.pdf"
        version.storage_path = "working/renamed.pdf"
        renamed = service.build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )
        second_version = DocumentVersion(
            document_id=document.id,
            version_number=2,
            parent_version_id=version.id,
            storage_path="working/v2.pdf",
            filename="第二版.pdf",
            content_type=document.content_type,
            size_bytes=120,
            sha256="b" * 64,
            source_type="CONTENT_UPDATE",
            created_by=document.user_id,
        )
        db.add(second_version)
        db.flush()
        second_extraction = DocumentExtractionRun(
            document_id=document.id,
            document_version_id=second_version.id,
            status="COMPLETED",
            extractor="test",
            parser_name="deterministic",
            parser_version="1",
            parser_config_hash="parser-config-v1",
        )
        db.add(second_extraction)
        db.flush()
        db.add(
            DocumentPage(
                document_id=document.id,
                extraction_run_id=second_extraction.id,
                page_number=1,
                text_content="第二版新增正文",
                metadata_json={},
            )
        )
        db.flush()
        second = service.build(
            document_id=document.id,
            document_version_id=second_version.id,
            extraction_run_id=second_extraction.id,
        )

        assert renamed["index_run_id"] == first["index_run_id"]
        assert renamed["reused"] is True
        assert second["index_run_id"] != first["index_run_id"]
        assert db.query(DocumentIndexRun).count() == 2
    finally:
        db.close()


def test_build_latest_for_user_selects_extraction_bound_to_latest_version():
    """最新版本只能使用绑定该版本的解析运行，不能按更新时间误取旧正文。"""

    db = _database()
    try:
        document, first_version, first_extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": "第一版正文"}],
        )
        second_version = DocumentVersion(
            document_id=document.id,
            version_number=2,
            parent_version_id=first_version.id,
            storage_path="working/latest-v2.pdf",
            filename="第二版.pdf",
            content_type=document.content_type,
            size_bytes=120,
            sha256="d" * 64,
            source_type="CONTENT_UPDATE",
            created_by=document.user_id,
        )
        db.add(second_version)
        db.flush()
        second_extraction = DocumentExtractionRun(
            document_id=document.id,
            document_version_id=second_version.id,
            status="COMPLETED",
            extractor="test",
            parser_name="deterministic",
            parser_version="1",
            parser_config_hash="parser-config-v2",
        )
        db.add(second_extraction)
        db.flush()
        db.add(
            DocumentPage(
                document_id=document.id,
                extraction_run_id=second_extraction.id,
                page_number=1,
                text_content="第二版正文",
                metadata_json={},
            )
        )
        # 故意让旧解析更新时间更晚，保护选择逻辑必须先按版本过滤。
        first_extraction.updated_at = datetime.now(timezone.utc) + timedelta(days=1)
        db.flush()

        result = DocumentIndexService(db=db, settings=_settings()).build_latest_for_user(
            document_id=document.id,
            user_id=document.user_id,
        )

        assert result["ok"] is True
        assert result["document_version_id"] == second_version.id
        assert result["extraction_run_id"] == second_extraction.id
    finally:
        db.close()


def test_chunk_search_rejects_cross_user_candidate_version_ids():
    """即使调用方传入其他用户版本 ID，底层 Chunk 检索也必须返回空结果。"""

    db = _database()
    try:
        owner_document, _, _ = _document_facts(
            db,
            suffix="-owner.pdf",
            pages=[{"page_number": 1, "text": "当前用户材料"}],
        )
        other_document, other_version, other_extraction = _document_facts(
            db,
            suffix="-other.pdf",
            pages=[{"page_number": 1, "text": "跨用户秘密奖学金材料"}],
        )
        DocumentIndexService(db=db, settings=_settings()).build(
            document_id=other_document.id,
            document_version_id=other_version.id,
            extraction_run_id=other_extraction.id,
        )

        results = DocumentChunkLexicalSearchService(
            db=db,
            user_id=owner_document.user_id,
        ).search(
            query="秘密奖学金",
            document_version_ids=[other_version.id],
        )

        assert results == []
    finally:
        db.close()


def test_index_ignores_pages_with_inconsistent_document_ownership():
    """异常关联到其他文档的页面不得进入当前索引，避免跨用户正文污染。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            suffix="-scope-owner.pdf",
            pages=[{"page_number": 1, "text": "不应跨边界复制的正文"}],
        )
        other_document, _, _ = _document_facts(
            db,
            suffix="-scope-other.pdf",
            pages=[{"page_number": 1, "text": "其他用户正文"}],
        )
        page = (
            db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id == extraction.id)
            .one()
        )
        page.document_id = other_document.id
        db.flush()

        result = DocumentIndexService(db=db, settings=_settings()).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "EMPTY_EXTRACTION"
        assert db.query(DocumentChunk).count() == 0
    finally:
        db.close()


def test_cpu_tokenizer_supports_chinese_without_gpu_or_model_service():
    """中文词项生成只依赖 CPU，不得触发 embedding 或外部模型。"""

    tokenizer = ChineseLexicalTokenizer(["国家励志奖学金", "学生工作处"])
    search_text = tokenizer.search_text("学生工作处发布国家励志奖学金评审通知")

    assert "学生工作处" in search_text or "学生" in search_text
    assert "奖学金" in search_text or "奖学" in search_text


def test_empty_extraction_fails_without_partial_chunks():
    """空解析结果必须显式失败并清理派生数据，不能伪造可检索状态。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": ""}],
        )
        result = DocumentIndexService(db=db, settings=_settings()).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        assert result["ok"] is False
        assert result["status"] == "FAILED"
        assert db.query(DocumentChunk).count() == 0
        assert db.query(EvidenceSpan).count() == 0
    finally:
        db.close()


def test_index_resource_budget_fails_safely_without_deleting_source_facts():
    """超出部署索引预算时只清理派生记录，解析正文和文件版本必须保留。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": "超过测试预算但必须保留的正文"}],
        )
        result = DocumentIndexService(
            db=db,
            settings=_settings(document_index_max_chars=5),
        ).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "INDEX_SOURCE_TOO_LARGE"
        assert db.get(DocumentVersion, version.id) is not None
        assert db.get(DocumentPage, db.query(DocumentPage.id).scalar()).text_content
        assert db.query(DocumentChunk).count() == 0
        assert db.query(EvidenceSpan).count() == 0
    finally:
        db.close()


def test_duplicate_paragraphs_on_same_page_remain_separate_valid_chunks():
    """同页重复正文是合法事实，不能被内容哈希唯一约束误判为重复任务。"""

    db = _database()
    try:
        document, version, extraction = _document_facts(
            db,
            pages=[{"page_number": 1, "text": "重复段落\n\n重复段落"}],
        )
        result = DocumentIndexService(
            db=db,
            settings=_settings(document_chunk_max_chars=5, document_chunk_overlap_chars=0),
        ).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

        assert result["ok"] is True
        assert db.query(DocumentChunk).count() == 2
    finally:
        db.close()


def test_embedding_tool_reports_disabled_instead_of_faking_completion(monkeypatch):
    """embedding 关闭时 ToolInvocation 必须失败，不能返回伪造的已写入结果。"""

    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    from app.core import config

    config.get_settings.cache_clear()
    invocation = ToolRegistry().invoke("embedding-generate", {"document_id": "document-1"})

    assert invocation.status == "FAILED"
    assert invocation.output_json["ok"] is False
    assert invocation.output_json["error"]["code"] == "EMBEDDING_DISABLED"


def test_chunks_api_enforces_owner_and_never_returns_text_or_embedding():
    """普通用户索引 API 只能返回自己的定位元数据，响应中不得出现正文或向量字段。"""

    client, SessionLocal = client_with_database()
    try:
        client.post("/api/auth/register", json={"username": "chunk-owner", "password": "password123"})
        owner_token = client.post(
            "/api/auth/login", json={"username": "chunk-owner", "password": "password123"}
        ).json()["access_token"]
        client.post("/api/auth/register", json={"username": "chunk-other", "password": "password123"})
        other_token = client.post(
            "/api/auth/login", json={"username": "chunk-other", "password": "password123"}
        ).json()["access_token"]
        db = SessionLocal()
        owner = db.query(User).filter(User.username == "chunk-owner").one()
        document, version, extraction = _document_facts_for_user(
            db,
            user=owner,
            text="只允许通过持久化证据访问的敏感测试正文",
        )
        DocumentIndexService(db=db, settings=_settings()).build(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )
        document_id = document.id
        db.commit()
        db.close()

        response = client.get(
            f"/api/documents/{document_id}/chunks",
            headers={"Authorization": f"Bearer {owner_token}"},
        )
        assert response.status_code == 200
        serialized = response.text
        assert "敏感测试正文" not in serialized
        assert "text_content" not in serialized
        assert "search_text" not in serialized
        assert '"embedding"' not in serialized
        assert response.json()["embedding_status"] == "DISABLED"

        forbidden = client.get(
            f"/api/documents/{document_id}/chunks",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert forbidden.status_code == 404
    finally:
        clear_overrides()


def _document_facts_for_user(
    db: Session,
    *,
    user: User,
    text: str,
) -> tuple[Document, DocumentVersion, DocumentExtractionRun]:
    """为 API 所有权测试复用已认证用户创建索引事实。"""

    document = Document(
        user_id=user.id,
        workspace_id=user.default_workspace_id,
        original_filename="证据.pdf",
        content_type="application/pdf",
        size_bytes=len(text.encode("utf-8")),
        sha256="c" * 64,
    )
    db.add(document)
    db.flush()
    version = DocumentVersion(
        document_id=document.id,
        version_number=1,
        storage_path="working/evidence.pdf",
        filename="证据.pdf",
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        source_type="IMPORT",
        created_by=user.id,
    )
    db.add(version)
    db.flush()
    extraction = DocumentExtractionRun(
        document_id=document.id,
        document_version_id=version.id,
        status="COMPLETED",
        extractor="test",
        parser_name="deterministic",
        parser_version="1",
        parser_config_hash="parser-config-v1",
    )
    db.add(extraction)
    db.flush()
    db.add(
        DocumentPage(
            document_id=document.id,
            extraction_run_id=extraction.id,
            page_number=1,
            text_content=text,
            metadata_json={},
        )
    )
    db.flush()
    return document, version, extraction
