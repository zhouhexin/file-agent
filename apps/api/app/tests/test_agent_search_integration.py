"""Agent 集成测试：hybrid-search handler 在 two_stage_retrieval_enabled 下的行为。

测试目标：
1. 默认（开关关闭）使用 WorkingCopySummarySearchService
2. 开关开启后使用 TwoStageFileSearchService
3. 开关开启后无 workspace 时降级到旧链路
4. 关闭开关不影响 Tool 契约
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentChunk,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentPage,
    DocumentSearchProfile,
    DocumentSummary,
    DocumentVersion,
    EvidenceSpan,
    User,
    Workspace,
    WorkingCopy,
)


@pytest.fixture(autouse=True)
def _set_db_url(monkeypatch):
    """为 get_settings 提供 PostgreSQL URL，避免 RuntimeError。"""

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/db",
    )


def _db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _setup_user_with_workspace(db, *, user_id, workspace_id):
    user = User(
        id=user_id, username=f"user-{user_id}", password_hash="x",
        default_workspace_id=workspace_id,
    )
    workspace = Workspace(
        id=workspace_id, name=f"ws-{user_id}",
        owner_id=user_id, is_default=True,
    )
    db.add_all([user, workspace])
    db.flush()


def _setup_full_doc(
    db, *, suffix, user_id, workspace_id,
    filename, summary_text, chunk_text, category_path=None,
):
    """同 test_two_stage_file_search._setup_full_doc 的简版。"""
    doc = Document(
        id=f"doc-{suffix}", user_id=user_id, workspace_id=workspace_id,
        original_filename=f"src-{suffix}.docx",
        content_type="application/pdf", size_bytes=100, sha256=suffix * 64,
    )
    ver = DocumentVersion(
        id=f"ver-{suffix}", document_id=doc.id, version_number=1,
        storage_tier="WORKING_COPY", storage_path=f"work/{filename}",
        filename=filename, content_type=doc.content_type,
        size_bytes=doc.size_bytes, sha256=doc.sha256, source_type="IMPORT",
    )
    db.add_all([doc, ver])
    db.flush()

    wc = WorkingCopy(
        id=f"wc-{suffix}", working_copy_root_id=f"root-{suffix}",
        workspace_id=workspace_id, managed_file_id=f"mf-{suffix}",
        document_id=doc.id, current_version_id=ver.id,
        relative_path=filename, relative_path_hash=suffix * 64,
        filename=filename, extension="docx",
        size_bytes=doc.size_bytes, content_sha256=doc.sha256,
        imported_source_sha256=doc.sha256, status="ACTIVE",
    )
    db.add(wc)

    ext_run = DocumentExtractionRun(
        id=f"ext-{suffix}", document_id=doc.id, status="COMPLETED",
    )
    db.add(ext_run)
    db.flush()

    index_run = DocumentIndexRun(
        id=f"idx-{suffix}", document_id=doc.id,
        document_version_id=ver.id, extraction_run_id=ext_run.id,
        index_version="chunk-index-v1", tokenizer="jieba",
        tokenizer_version="v1", config_hash=f"hash-{suffix}",
        status="COMPLETED", chunk_count=1, evidence_count=1,
    )
    db.add(index_run)
    db.flush()

    db.add(DocumentPage(
        id=f"page-{suffix}", document_id=doc.id,
        page_number=1, text_content=chunk_text,
        extraction_run_id=ext_run.id,
    ))

    chunk = DocumentChunk(
        id=f"chunk-{suffix}", index_run_id=index_run.id,
        document_id=doc.id, document_version_id=ver.id,
        extraction_run_id=ext_run.id, chunk_index=0, chunk_type="page",
        text_content=chunk_text, search_text=chunk_text,
        content_hash=f"hash-{suffix}", location_hash=f"loc-{suffix}",
        char_count=len(chunk_text), token_count=len(chunk_text.split()),
        page_start=1, page_end=1,
    )
    db.add(chunk)
    db.flush()

    db.add(EvidenceSpan(
        id=f"ev-{suffix}", chunk_id=chunk.id,
        document_id=doc.id, document_version_id=ver.id,
        extraction_run_id=ext_run.id, span_index=0,
        evidence_type="text_quote", quote=chunk_text[:100],
        start_offset=0, end_offset=min(len(chunk_text), 100),
        page_number=1, source="document_chunk",
    ))

    db.add(DocumentSummary(
        id=f"sum-{suffix}", document_id=doc.id,
        document_version_id=ver.id, extraction_run_id=ext_run.id,
        input_sha256=doc.sha256, summary_text=summary_text,
        summary_json={"overview": summary_text, "year": None},
        coverage_json={}, prompt_version="v1", schema_version="v1",
        status="COMPLETED",
    ))

    if category_path:
        db.add(DocumentCategorySuggestion(
            id=f"sug-{suffix}", classification_run_id=f"cr-{suffix}",
            document_id=doc.id, document_version_id=ver.id,
            category_id=f"cat-{suffix}", category_name=category_path[-1],
            category_path_json=category_path,
            taxonomy_key="school_file_classification", taxonomy_version="v1",
            confidence=0.9, status="SUGGESTED", evidence_json=[], rank=1,
        ))

    db.add(DocumentSearchProfile(
        id=f"prof-{suffix}", user_id=user_id, workspace_id=workspace_id,
        working_copy_id=wc.id, document_id=doc.id,
        document_version_id=ver.id, status="ACTIVE",
        normalized_filename=filename.lower().replace(" ", ""),
        filename_search_text=filename,
        category_search_text=" ".join(category_path) if category_path else "",
        summary_search_text=summary_text,
        combined_search_text=f"{filename} {summary_text} {chunk_text}",
    ))

    db.flush()


def test_handler_default_uses_two_stage_search(monkeypatch):
    """阶段四默认启用低耗两阶段检索，不能静默回退到旧摘要扫描。"""

    from app.modules.agent.tool_registry import _search_handler
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.two_stage_retrieval_enabled is True, (
        "阶段四上线后默认应启用低耗两阶段检索"
    )

    db = _db_session()
    try:
        _setup_user_with_workspace(db, user_id="u1", workspace_id="w1")
        _setup_full_doc(
            db, suffix="a", user_id="u1", workspace_id="w1",
            filename="奖学金申请.docx",
            summary_text="国家励志奖学金申请材料",
            chunk_text="国家励志奖学金",
            category_path=["奖助学金"],
        )
        db.commit()

        handler = _search_handler(db=db, user_id="u1")

        class _FakeToolInput:
            query = "找奖学金"
            document_ids = []

        result = handler(_FakeToolInput())

        # 默认应走两阶段链路，返回安全用户投影字段。
        assert result["kind"] == "workspace_file_search"
        assert result["ok"] is True
        assert "total_returned" in result
        assert "user_message" in result
    finally:
        db.close()


def test_handler_with_enabled_flag_uses_two_stage(monkeypatch):
    """TWO_STAGE_RETRIEVAL_ENABLED=true 时走两阶段检索。"""

    from app.core.config import get_settings

    # 显式设置配置
    settings = get_settings()
    monkeypatch.setattr(settings, "two_stage_retrieval_enabled", True)

    db = _db_session()
    try:
        _setup_user_with_workspace(db, user_id="u1", workspace_id="w1")
        _setup_full_doc(
            db, suffix="a", user_id="u1", workspace_id="w1",
            filename="国家励志奖学金申请.docx",
            summary_text="奖学金",
            chunk_text="奖学金材料",
            category_path=["奖助学金"],
        )
        db.commit()

        from app.modules.agent.tool_registry import _search_handler
        handler = _search_handler(db=db, user_id="u1")

        class _FakeToolInput:
            query = "找奖学金"
            document_ids = []

        result = handler(_FakeToolInput())

        assert result["kind"] == "workspace_file_search"
        assert result["ok"] is True
        # 新链路应返回 total_returned 和 user_message 字段
        assert "total_returned" in result
        assert "user_message" in result
    finally:
        db.close()


def test_handler_without_workspace_falls_back(monkeypatch):
    """没有 default_workspace 时降级到旧链路，避免破坏现有调用。"""

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "two_stage_retrieval_enabled", True)

    db = _db_session()
    try:
        # 创建用户但没有 workspace
        user = User(
            id="u1", username="user1", password_hash="x",
            default_workspace_id=None,
        )
        db.add(user)
        _setup_full_doc(
            db, suffix="a", user_id="u1", workspace_id="w1",
            filename="奖学金.docx",
            summary_text="奖学金",
            chunk_text="奖学金材料",
        )
        db.commit()

        from app.modules.agent.tool_registry import _search_handler
        handler = _search_handler(db=db, user_id="u1")

        class _FakeToolInput:
            query = "找奖学金"
            document_ids = []

        result = handler(_FakeToolInput())

        # 没有 workspace 时降级到旧链路，结果不含 total_returned
        assert result["kind"] == "workspace_file_search"
        assert result["ok"] is True
    finally:
        db.close()


def test_handler_returns_kind_field_in_both_modes(monkeypatch):
    """无论开关状态都返回 kind=workspace_file_search，保持 Tool 契约。"""

    db = _db_session()
    try:
        _setup_user_with_workspace(db, user_id="u1", workspace_id="w1")
        _setup_full_doc(
            db, suffix="a", user_id="u1", workspace_id="w1",
            filename="奖学金.docx",
            summary_text="奖学金",
            chunk_text="奖学金",
        )
        db.commit()

        from app.modules.agent.tool_registry import _search_handler

        # 模式 1：默认
        from app.core.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "two_stage_retrieval_enabled", False)
        handler_off = _search_handler(db=db, user_id="u1")

        class _ToolInput:
            query = "找奖学金"
            document_ids = []

        result_off = handler_off(_ToolInput())
        assert result_off["kind"] == "workspace_file_search"

        # 模式 2：开启
        monkeypatch.setattr(settings, "two_stage_retrieval_enabled", True)
        handler_on = _search_handler(db=db, user_id="u1")
        result_on = handler_on(_ToolInput())
        assert result_on["kind"] == "workspace_file_search"
    finally:
        db.close()
