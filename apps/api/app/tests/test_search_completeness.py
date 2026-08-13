"""检索完整性评估测试。

这些用例保护普通用户不被“找到若干结果”误导为“当前范围已经找全”：只有活动
工作副本的当前版本均有检索资料、检索未降级且未触及候选上限时才能返回 COMPLETE。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentSearchProfile,
    DocumentVersion,
    WorkingCopy,
)
from app.modules.retrieval.completeness import SearchCompletenessService


@dataclass
class _Scope:
    """为完整性服务提供与真实范围解析器一致的最小只读范围。"""

    scope_mode: str = "global"
    strict_document_ids: tuple[str, ...] = ()


def _db_session():
    """创建独立内存库，保证状态判断不依赖开发机真实索引。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _add_working_copy(
    db,
    *,
    suffix: str,
    profile_ready: bool,
    index_ready: bool,
    extraction_status: str = "COMPLETED",
) -> tuple[str, str]:
    """创建当前版本明确的活动工作副本及可选检索派生数据。"""

    document = Document(
        id=f"doc-{suffix}",
        user_id="user-1",
        workspace_id="workspace-1",
        original_filename=f"{suffix}.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1,
        sha256=(suffix * 64)[:64],
    )
    version = DocumentVersion(
        id=f"version-{suffix}",
        document_id=document.id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path=f"working/{suffix}.docx",
        filename=f"{suffix}.docx",
        content_type=document.content_type,
        size_bytes=1,
        sha256=document.sha256,
        source_type="IMPORT",
    )
    copy = WorkingCopy(
        id=f"copy-{suffix}",
        working_copy_root_id=f"root-{suffix}",
        workspace_id="workspace-1",
        managed_file_id=f"managed-{suffix}",
        document_id=document.id,
        current_version_id=version.id,
        relative_path=f"{suffix}.docx",
        relative_path_hash=(f"path-{suffix}" * 64)[:64],
        filename=f"{suffix}.docx",
        extension="docx",
        size_bytes=1,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        status="ACTIVE",
    )
    extraction = DocumentExtractionRun(
        id=f"extraction-{suffix}",
        document_id=document.id,
        document_version_id=version.id,
        status=extraction_status,
        extractor="test",
        parser_name="test",
        parser_version="v1",
        parser_config_hash="test",
    )
    db.add_all([document, version, copy, extraction])
    if profile_ready:
        db.add(
            DocumentSearchProfile(
                id=f"profile-{suffix}",
                user_id="user-1",
                workspace_id="workspace-1",
                working_copy_id=copy.id,
                document_id=document.id,
                document_version_id=version.id,
                status="ACTIVE",
            )
        )
    if index_ready:
        db.add(
            DocumentIndexRun(
                id=f"index-{suffix}",
                document_id=document.id,
                document_version_id=version.id,
                extraction_run_id=extraction.id,
                index_version="document-chunk-index-v2",
                tokenizer="jieba",
                tokenizer_version="test",
                config_hash=f"config-{suffix}",
                status="COMPLETED",
            )
        )
    db.flush()
    return document.id, copy.id


def test_complete_only_when_all_active_files_are_currently_searchable():
    """所有活动文件均有当前 profile 与 index 时才允许对用户说“已找全”。"""

    db = _db_session()
    try:
        _add_working_copy(db, suffix="a", profile_ready=True, index_ready=True)
        db.commit()
        payload = SearchCompletenessService(
            db=db, workspace_id="workspace-1"
        ).assess(scope=_Scope(), result={"ok": True, "partial": False})
        assert payload["status"] == "COMPLETE"
        assert payload["can_claim_complete"] is True
        assert payload["eligible_file_count"] == 1
    finally:
        db.close()


def test_processing_and_failed_files_never_claim_search_is_complete():
    """待索引与确定性解析失败必须分别展示，二者都不能被空结果掩盖。"""

    db = _db_session()
    try:
        _add_working_copy(db, suffix="pending", profile_ready=False, index_ready=False)
        db.commit()
        service = SearchCompletenessService(db=db, workspace_id="workspace-1")
        processing = service.assess(scope=_Scope(), result={"ok": True})
        assert processing["status"] == "PROCESSING"
        assert processing["pending_file_count"] == 1

        _add_working_copy(
            db,
            suffix="failed",
            profile_ready=False,
            index_ready=False,
            extraction_status="FAILED",
        )
        db.commit()
        partial = service.assess(scope=_Scope(), result={"ok": True})
        assert partial["status"] == "PARTIAL"
        assert partial["failed_file_count"] == 1
        assert partial["can_claim_complete"] is False
    finally:
        db.close()


def test_candidate_limit_or_unresolved_strict_scope_is_not_verifiable():
    """候选上限和未映射附件均必须阻止“已找全”这一用户承诺。"""

    db = _db_session()
    try:
        document_id, _ = _add_working_copy(
            db, suffix="limit", profile_ready=True, index_ready=True
        )
        db.commit()
        service = SearchCompletenessService(db=db, workspace_id="workspace-1")
        limited = service.assess(
            scope=_Scope(),
            result={"ok": True, "candidate_limit_reached": True},
        )
        assert limited["status"] == "PARTIAL"
        assert limited["candidate_limit_reached"] is True

        unresolved = service.assess(
            scope=_Scope(
                scope_mode="strict",
                strict_document_ids=(document_id,),
            ),
            result={"ok": True},
            unresolved_document_count=1,
        )
        assert unresolved["status"] == "UNVERIFIABLE"
    finally:
        db.close()
