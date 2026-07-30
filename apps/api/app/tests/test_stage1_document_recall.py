"""Stage1DocumentRecallService 测试。

测试目标：
1. Service 可导入
2. SQLite deterministic 降级不抛异常
3. 精确文件名匹配优先
4. 候选上限有效
5. 跨用户隔离
6. 候选收敛后通过有界批量查询补齐显示字段
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentCategorySuggestion,
    DocumentSearchProfile,
    DocumentSummary,
    DocumentVersion,
    WorkingCopy,
)
from app.modules.retrieval.stage1_document_recall import Stage1DocumentRecallService


@dataclass
class _FakeParsedQuery:
    cleaned: str = ""
    terms: list[str] = field(default_factory=list)
    year: int | None = None
    relative_year: int | None = None


@dataclass
class _FakeScope:
    strict_document_ids: tuple = field(default_factory=tuple)
    conversation_document_ids: tuple = field(default_factory=tuple)
    include_workspace: bool = True
    scope_mode: str = "global"


@dataclass
class _FakeConfig:
    retrieval_document_candidate_limit: int = 30
    retrieval_document_detail_limit: int = 12
    retrieval_chunk_limit_per_document: int = 3
    retrieval_chunk_global_limit: int = 24
    retrieval_query_max_chars: int = 500
    retrieval_preview_max_chars: int = 240
    retrieval_filename_trgm_min_chars: int = 4
    retrieval_filename_trgm_candidate_limit: int = 20
    retrieval_filename_trgm_similarity_threshold: float = 0.25


def _db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _setup_profile(db, *, suffix, user_id, filename, summary_text="",
                   category_path=None):
    """创建 Document + WorkingCopy + DocumentSummary + DocumentSearchProfile。"""
    doc = Document(
        id=f"doc-{suffix}",
        user_id=user_id,
        workspace_id=f"ws-{user_id}",
        original_filename=f"src-{suffix}.docx",
        content_type="application/vnd.openxmlformats",
        size_bytes=100,
        sha256=suffix * 64,
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
        workspace_id=doc.workspace_id, managed_file_id=f"mf-{suffix}",
        document_id=doc.id, current_version_id=ver.id,
        relative_path=filename, relative_path_hash=suffix * 64,
        filename=filename, extension="docx",
        size_bytes=doc.size_bytes, content_sha256=doc.sha256,
        imported_source_sha256=doc.sha256, status="ACTIVE",
    )
    db.add(wc)
    if summary_text:
        db.add(DocumentSummary(
            id=f"sum-{suffix}", document_id=doc.id,
            document_version_id=ver.id, extraction_run_id=f"ext-{suffix}",
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
    db.flush()

    # 创建 DocumentSearchProfile
    profile = DocumentSearchProfile(
        id=f"prof-{suffix}",
        user_id=user_id, workspace_id=doc.workspace_id,
        working_copy_id=wc.id, document_id=doc.id,
        document_version_id=ver.id, status="ACTIVE",
        normalized_filename=filename.lower().replace(" ", ""),
        filename_search_text=" ".join(filename.lower().split()),
        category_search_text=" ".join(category_path) if category_path else "",
        summary_search_text=summary_text,
        combined_search_text=f"{filename} {summary_text}",
    )
    db.add(profile)
    db.flush()
    return doc


def test_service_importable():
    """Stage1DocumentRecallService 可导入。"""
    from app.modules.retrieval.stage1_document_recall import Stage1DocumentRecallService
    assert Stage1DocumentRecallService is not None


def test_sqlite_deterministic_returns_candidates():
    """SQLite deterministic 降级返回正确候选。"""

    db = _db_session()
    try:
        _setup_profile(db, suffix="a", user_id="user1", filename="奖学金.docx",
                       summary_text="国家励志奖学金申请材料", category_path=["奖助学金"])
        db.commit()

        service = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=_FakeConfig(),
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned="奖学金"),
            scope=_FakeScope(),
        )
        assert len(result) >= 1
        assert any("奖学金" in r.get("filename", "") for r in result)
    finally:
        db.close()


def test_exact_filename_returns_highest_score():
    """精确文件名匹配优先返回。"""

    db = _db_session()
    try:
        _setup_profile(db, suffix="exact", user_id="user1", filename="国家励志奖学金.docx",
                       summary_text="普通文件", category_path=["奖助学金"])
        _setup_profile(db, suffix="fuzzy", user_id="user1", filename="普通文件.docx",
                       summary_text="国家励志奖学金申请材料", category_path=["奖助学金"])
        db.commit()

        service = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=_FakeConfig(),
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned="国家励志奖学金"),
            scope=_FakeScope(),
        )
        assert len(result) >= 1
        # 精确文件名匹配的应该排前面
        top = result[0]
        assert "国家励志奖学金" in top.get("filename", "")
    finally:
        db.close()


def test_candidate_limit_is_enforced():
    """候选上限有效。"""

    db = _db_session()
    try:
        for i in range(5):
            _setup_profile(db, suffix=f"lim-{i}", user_id="user1",
                           filename=f"文件{i}.docx", summary_text="测试文件")
        db.commit()

        config = _FakeConfig()
        config.retrieval_document_candidate_limit = 3

        service = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=config,
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned="文件"),
            scope=_FakeScope(),
        )
        assert len(result) <= 3
    finally:
        db.close()


def test_cross_user_isolation():
    """其他用户的文件不会出现。"""

    db = _db_session()
    try:
        _setup_profile(db, suffix="ua", user_id="user-a", filename="奖学金.docx",
                       summary_text="国家励志奖学金")
        _setup_profile(db, suffix="ub", user_id="user-b", filename="奖学金.docx",
                       summary_text="国家励志奖学金")
        db.commit()

        service = Stage1DocumentRecallService(
            db=db, user_id="user-a", workspace_id="ws-user-a", config=_FakeConfig(),
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned="奖学金"),
            scope=_FakeScope(),
        )
        assert all(r.get("working_copy_id", "").startswith("wc-ua") for r in result)
        assert all("user-b" not in str(r) for r in result)
    finally:
        db.close()


def test_strict_scope_never_expands_to_other_active_documents():
    """“这些文件”范围只能返回后端确认的附件，不能因同词命中扩展到工作区。"""

    db = _db_session()
    try:
        selected = _setup_profile(
            db, suffix="strict-selected", user_id="user1",
            filename="奖学金申请.docx", summary_text="国家励志奖学金材料",
        )
        _setup_profile(
            db, suffix="strict-other", user_id="user1",
            filename="另一份奖学金申请.docx", summary_text="国家励志奖学金材料",
        )
        db.commit()

        scope = _FakeScope(
            strict_document_ids=(selected.id,),
            include_workspace=False,
            scope_mode="strict",
        )
        result = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=_FakeConfig(),
        ).recall(parsed_query=_FakeParsedQuery(cleaned="奖学金"), scope=scope)

        assert [item["document_id"] for item in result] == [selected.id]
    finally:
        db.close()


def test_enrich_includes_display_fields():
    """候选收敛后通过有界批量查询补齐显示字段。"""

    db = _db_session()
    try:
        _setup_profile(db, suffix="rich", user_id="user1",
                       filename="奖学金.docx",
                       summary_text="国家励志奖学金申请材料",
                       category_path=["学校", "学生工作", "奖助学金"])
        db.commit()

        service = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=_FakeConfig(),
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned="奖学金"),
            scope=_FakeScope(),
        )
        assert len(result) >= 1
        item = result[0]
        assert item.get("filename") == "奖学金.docx"
        assert len(item.get("category_path", [])) >= 1
    finally:
        db.close()


def test_enrich_reads_one_to_many_details_in_bounded_batch_queries():
    """摘要和分类建议必须分批读取，不能通过一对多 JOIN 产生笛卡尔膨胀。"""

    db = _db_session()
    try:
        document = _setup_profile(
            db,
            suffix="bounded-enrich",
            user_id="user1",
            filename="工作总结.docx",
            summary_text="2025年度工作总结",
            category_path=["学校", "行政管理", "年度总结"],
        )
        db.add(
            DocumentCategorySuggestion(
                id="sug-bounded-enrich-secondary",
                classification_run_id="cr-bounded-enrich-secondary",
                document_id=document.id,
                document_version_id="ver-bounded-enrich",
                category_id="cat-bounded-enrich-secondary",
                category_name="其他",
                category_path_json=["其他"],
                taxonomy_key="school_file_classification",
                taxonomy_version="v1",
                confidence=0.2,
                status="SUGGESTED",
                evidence_json=[],
                rank=2,
            )
        )
        db.commit()
        statements: list[str] = []

        def _record_statement(_conn, _cursor, statement, *_args):
            statements.append(statement)

        bind = db.get_bind()
        event.listen(bind, "before_cursor_execute", _record_statement)
        try:
            result = Stage1DocumentRecallService(
                db=db,
                user_id="user1",
                workspace_id="ws-user1",
                config=_FakeConfig(),
            )._enrich(
                [
                    {
                        "working_copy_id": "wc-bounded-enrich",
                        "document_id": "doc-bounded-enrich",
                        "document_version_id": "ver-bounded-enrich",
                        "_score": 1.0,
                        "_hit_source": "test",
                    }
                ],
                scope=_FakeScope(),
            )
        finally:
            event.remove(bind, "before_cursor_execute", _record_statement)

        assert len(statements) == 3
        assert len(result) == 1
        assert result[0]["category_path"] == [
            "学校",
            "行政管理",
            "年度总结",
        ]
    finally:
        db.close()


def test_inactive_profile_not_returned():
    """INACTIVE 状态的工作副本不出现在结果中。"""

    db = _db_session()
    try:
        _setup_profile(db, suffix="act", user_id="user1", filename="活跃文件.docx",
                       summary_text="这是活跃文件")
        _setup_profile(db, suffix="ina", user_id="user1", filename="非活跃文件.docx",
                       summary_text="这是非活跃文件")

        # 把第二个 profile 标记为 INACTIVE
        profile = db.query(DocumentSearchProfile).filter(
            DocumentSearchProfile.working_copy_id == "wc-ina"
        ).first()
        if profile:
            profile.status = "INACTIVE"
        db.commit()

        service = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=_FakeConfig(),
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned="文件"),
            scope=_FakeScope(),
        )
        wc_ids = [r.get("working_copy_id") for r in result]
        assert "wc-act" in wc_ids
        assert "wc-ina" not in wc_ids, "INACTIVE 文件不应被召回"
    finally:
        db.close()


def test_empty_query_returns_no_results():
    """空查询返回空结果。"""

    db = _db_session()
    try:
        _setup_profile(db, suffix="empty", user_id="user1", filename="奖学金.docx",
                       summary_text="奖学金")
        db.commit()

        service = Stage1DocumentRecallService(
            db=db, user_id="user1", workspace_id="ws-user1", config=_FakeConfig(),
        )
        result = service.recall(
            parsed_query=_FakeParsedQuery(cleaned=""),
            scope=_FakeScope(),
        )
        assert len(result) == 0
    finally:
        db.close()
