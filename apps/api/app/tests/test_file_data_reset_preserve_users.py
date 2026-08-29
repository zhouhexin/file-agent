"""保留用户的文件域受控重置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.db.models import (
    AgentRun,
    Conversation,
    Document,
    DocumentClassificationRun,
    FilesystemJob,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
    User,
    WorkingCopyRoot,
    Workspace,
)
from app.modules.classification.freshness import current_classification_identity
from app.scripts.reset_file_data_preserve_users import (
    FILE_DOMAIN_TABLES,
    PRESERVED_TABLES,
    run_file_data_reset,
    validate_table_manifest,
)
from app.scripts.reset_managed_root_working_copies import (
    reset_working_copy_materializations,
)
from app.tests.helpers import client_with_database


def _settings(tmp_path: Path) -> Settings:
    """构造所有删除目标都位于隔离临时目录内的测试配置。"""

    return Settings(
        database_url="sqlite+pysqlite:///:memory:",
        file_storage_root=str(tmp_path / "storage"),
        working_copy_storage_root=str(tmp_path / "working-copies"),
        trash_storage_root=str(tmp_path / "trash"),
        managed_root_archive_write_path=str(tmp_path / "internal-originals"),
    )


def _seed_identity_and_file_data(SessionLocal) -> tuple[str, str, str]:
    """写入必须保留的身份事实和必须清除的文件事实。"""

    with SessionLocal() as db:
        user = User(
            id="user-preserved",
            username="classification-tester",
            email="classification@example.test",
            password_hash="stable-password-hash",
            display_name="分类测试员",
            role="admin",
        )
        db.add(user)
        db.flush()
        user_workspace = Workspace(
            id="workspace-user",
            name="default workspace",
            owner_id=user.id,
            is_default=True,
            workspace_type="USER",
        )
        shared_workspace = Workspace(
            id="workspace-shared",
            name="系统共享工作区",
            owner_id=None,
            is_default=False,
            workspace_type="SYSTEM_SHARED",
            system_key="SYSTEM_SHARED",
        )
        db.add_all([user_workspace, shared_workspace])
        db.flush()
        user.default_workspace_id = user_workspace.id
        managed_root = ManagedRoot(
            id="root-preserved",
            root_key="school_files",
            display_name="学校文件",
            container_path="/external/school-files",
            enabled=True,
            read_only=True,
            created_by=user.id,
        )
        conversation = Conversation(
            id="conversation-cleared",
            user_id=user.id,
            workspace_id=user_workspace.id,
            title="旧文件会话",
        )
        document = Document(
            id="document-cleared",
            user_id=user.id,
            workspace_id=user_workspace.id,
            original_filename="旧测试文件.pdf",
            content_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
        )
        db.add_all([managed_root, conversation, document])
        db.commit()
        return user.id, user_workspace.id, managed_root.id


def test_file_domain_manifest_covers_every_orm_table():
    """新增 ORM 表若未归入保留或清理范围，重置必须在开发期立即失败。"""

    _client, SessionLocal = client_with_database()
    engine = SessionLocal.kw["bind"]
    validate_table_manifest(database_engine=engine)
    assert PRESERVED_TABLES.isdisjoint(FILE_DOMAIN_TABLES)


def test_file_domain_reset_preserves_users_and_clears_file_data(tmp_path: Path):
    """文件域重置必须保留账号、工作区和受管根，同时清除文件与旧会话。"""

    _client, SessionLocal = client_with_database()
    user_id, workspace_id, root_id = _seed_identity_and_file_data(SessionLocal)
    settings = _settings(tmp_path)
    for directory in (
        Path(settings.file_storage_root) / "uploads",
        Path(settings.file_storage_root) / "quarantine",
        Path(settings.file_storage_root) / "temp",
        Path(settings.working_copy_storage_root),
        Path(settings.trash_storage_root),
        Path(settings.managed_root_archive_write_path),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "old-file.bin").write_bytes(b"old")

    engine = SessionLocal.kw["bind"]
    with SessionLocal() as db:
        completed = run_file_data_reset(
            settings=settings,
            project_root=tmp_path / "project",
            db=db,
            database_engine=engine,
            external_import_stopped=True,
        )

    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user is not None
        assert user.password_hash == "stable-password-hash"
        assert user.role == "admin"
        assert user.default_workspace_id == workspace_id
        assert db.get(Workspace, workspace_id) is not None
        assert db.get(ManagedRoot, root_id) is not None
        assert db.query(Document).count() == 0
        assert db.query(Conversation).count() == 0

    for directory in (
        Path(settings.file_storage_root) / "uploads",
        Path(settings.file_storage_root) / "quarantine",
        Path(settings.file_storage_root) / "temp",
        Path(settings.working_copy_storage_root),
        Path(settings.trash_storage_root),
        Path(settings.managed_root_archive_write_path),
    ):
        assert directory.is_dir()
        assert list(directory.iterdir()) == []
    assert any(item.startswith("用户身份（保留 1 个）") for item in completed)


def test_file_domain_reset_requires_external_import_to_be_stopped(tmp_path: Path):
    """没有确认停止 watcher 和 worker 时不得触碰数据库或目录。"""

    _client, SessionLocal = client_with_database()
    _seed_identity_and_file_data(SessionLocal)
    settings = _settings(tmp_path)
    engine = SessionLocal.kw["bind"]
    with SessionLocal() as db:
        with pytest.raises(ValueError, match="停止 watcher"):
            run_file_data_reset(
                settings=settings,
                project_root=tmp_path / "project",
                db=db,
                database_engine=engine,
                external_import_stopped=False,
            )
    with SessionLocal() as db:
        assert db.query(User).count() == 1
        assert db.query(Document).count() == 1


@pytest.mark.parametrize("classification_is_current", [True, False])
def test_working_copy_reset_refreshes_stale_classification_before_materialization(
    monkeypatch,
    tmp_path: Path,
    classification_is_current: bool,
):
    """重置必须让当前分类直接物化，让任意旧分类先进入刷新队列。"""

    _client, SessionLocal = client_with_database()
    settings = _settings(tmp_path)
    source_root = tmp_path / "managed-source"
    source_root.mkdir()
    working_target = Path(settings.working_copy_storage_root) / "school-files"
    working_target.mkdir(parents=True)
    (working_target / "old-copy.txt").write_text("old", encoding="utf-8")
    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(source_root))

    with SessionLocal() as db:
        user = User(
            username=f"reset-classification-{classification_is_current}",
            password_hash="hash",
            display_name="重置测试用户",
            role="admin",
        )
        db.add(user)
        db.flush()
        workspace = Workspace(
            name="shared",
            owner_id=user.id,
            is_default=True,
            workspace_type="USER",
        )
        db.add(workspace)
        db.flush()
        root = ManagedRoot(
            root_key="school_files",
            display_name="学校文件",
            container_path=str(source_root.resolve()),
            created_by=user.id,
        )
        source_document = Document(
            user_id=user.id,
            workspace_id=workspace.id,
            original_filename="source.txt",
            content_type="text/plain",
            size_bytes=6,
            sha256="a" * 64,
        )
        agent_run = AgentRun(
            conversation_id="reset-conversation",
            message_id="reset-message",
            user_id=user.id,
        )
        db.add_all([root, source_document, agent_run])
        db.flush()
        db.add(
            WorkingCopyRoot(
                workspace_id=workspace.id,
                managed_root_id=root.id,
                root_key=root.root_key,
                relative_storage_path="school-files",
                status="READY",
            )
        )
        managed_file = ManagedFile(
            root_id=root.id,
            relative_path="source.txt",
            relative_path_hash="reset-source-path",
            filename="source.txt",
            extension=".txt",
            size_bytes=6,
            fingerprint="reset-source-fingerprint",
            status="ACTIVE",
        )
        db.add(managed_file)
        db.flush()
        revision = ManagedFileRevision(
            managed_file_id=managed_file.id,
            revision_number=1,
            size_bytes=6,
            quick_fingerprint=managed_file.fingerprint,
            status="READY",
            analysis_status="READY",
            is_current=True,
            analysis_document_id=source_document.id,
        )
        db.add(revision)
        db.flush()
        identity = current_classification_identity(
            db=db,
            settings=settings,
            user_id=user.id,
        )
        db.add_all(
            [
                DocumentClassificationRun(
                    document_id=source_document.id,
                    agent_run_id=agent_run.id,
                    taxonomy_key=identity.taxonomy_key,
                    taxonomy_version=(
                        identity.taxonomy_version
                        if classification_is_current
                        else "arbitrary-old-version"
                    ),
                    classifier_version=identity.classifier_version,
                    source="managed_source_full_text",
                    status="COMPLETED",
                ),
                FilesystemJob(
                    job_type="MATERIALIZE_WORKING_COPY",
                    queue_name="MATERIALIZE",
                    root_id=root.id,
                    created_by=user.id,
                    status="COMPLETED",
                    payload_json={"managed_file_revision_id": revision.id},
                    result_json={"status": "READY"},
                ),
            ]
        )
        db.commit()

        result = reset_working_copy_materializations(
            db=db,
            settings=settings,
            project_root=project_root,
            root_key="school_files",
        )

        materialization = db.query(FilesystemJob).filter_by(
            job_type="MATERIALIZE_WORKING_COPY"
        ).one()
        refresh_jobs = db.query(FilesystemJob).filter_by(
            job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION"
        ).all()
        if classification_is_current:
            assert materialization.status == "PENDING"
            assert refresh_jobs == []
        else:
            assert materialization.status == "COMPLETED"
            assert materialization.result_json["status"] == (
                "DEFERRED_CLASSIFICATION_REFRESH"
            )
            assert len(refresh_jobs) == 1
            assert refresh_jobs[0].status == "PENDING"
        assert db.query(DocumentClassificationRun).count() == 1
        assert result["classification_freshness"] == {
            "CURRENT" if classification_is_current else "STALE": 1
        }
        assert list(working_target.iterdir()) == []
