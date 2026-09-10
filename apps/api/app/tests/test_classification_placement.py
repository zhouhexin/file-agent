"""D4/T20-T21 分类、目录与审计同提交测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core import config
from app.db.base import Base
from app.db.models import (
    ChangeItem,
    ClassificationPlacementOperation,
    Document,
    DocumentCategory,
    DocumentVersion,
    FileObject,
    FilesystemJob,
    ManagedFile,
    ManagedRoot,
    OperationConfirmation,
    OperationPlan,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyRoot,
    Workspace,
)
from app.modules.classification.placement_authorization import PlacementAuthorizationService
from app.modules.classification.placement_schemas import PlacementCommand
from app.modules.classification.placement_service import ClassificationPlacementService
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.file_lifecycle.service import FileLifecycleJobProcessor
from app.modules.managed_files.jobs import FilesystemJobQueue


USER_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
COPY_ID = "33333333-3333-4333-8333-333333333333"
DOCUMENT_ID = "44444444-4444-4444-8444-444444444444"
VERSION_ID = "55555555-5555-4555-8555-555555555555"


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed(db, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    config.get_settings.cache_clear()
    storage = FileLifecycleStorageService(config.get_settings())
    user = User(id=USER_ID, username="placement-user")
    workspace = Workspace(id=WORKSPACE_ID, name="共享", workspace_type="SYSTEM_SHARED")
    document = Document(
        id=DOCUMENT_ID,
        user_id=user.id,
        workspace_id=workspace.id,
        original_filename="财务材料.txt",
        size_bytes=11,
        sha256=hashlib.sha256(b"finance-file").hexdigest(),
    )
    version = DocumentVersion(
        id=VERSION_ID,
        document_id=document.id,
        version_number=1,
        storage_tier="WORKING_COPY",
        storage_path="shared/working/其他/财务材料.txt",
        filename=document.original_filename,
        content_type="text/plain",
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        source_type="WORKING_COPY",
        created_by=user.id,
    )
    managed_root = ManagedRoot(
        id="66666666-6666-4666-8666-666666666666",
        root_key="placement-source",
        display_name="原件",
        container_path="/managed/source",
    )
    managed_file = ManagedFile(
        id="77777777-7777-4777-8777-777777777777",
        root_id=managed_root.id,
        relative_path="财务材料.txt",
        relative_path_hash="a" * 64,
        filename=document.original_filename,
        extension=".txt",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
    )
    root = WorkingCopyRoot(
        id="88888888-8888-4888-8888-888888888888",
        workspace_id=workspace.id,
        managed_root_id=managed_root.id,
        root_key="placement-working",
        relative_storage_path="shared/working",
        status="ACTIVE",
    )
    copy = WorkingCopy(
        id=COPY_ID,
        working_copy_root_id=root.id,
        workspace_id=workspace.id,
        managed_file_id=managed_file.id,
        document_id=document.id,
        current_version_id=version.id,
        relative_path="其他/财务材料.txt",
        relative_path_hash="b" * 64,
        filename=document.original_filename,
        extension=".txt",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        status="ACTIVE",
        revision=1,
    )
    file_object = FileObject(
        document_id=document.id,
        storage_backend="working_copy_local",
        storage_path=version.storage_path,
        size_bytes=document.size_bytes,
        sha256=document.sha256,
    )
    db.add_all([user, workspace, document, version, managed_root, managed_file, root, copy, file_object])
    db.flush()
    db.add(
        DocumentCategory(
            working_copy_id=copy.id,
            document_id=document.id,
            document_version_id=version.id,
            category_id="system.other",
            category_path_json=["其他"],
            relation_role="PRIMARY",
            status="AUTO_APPLIED",
            taxonomy_key="unified_school_file_classification",
            taxonomy_version="2026-09-v10",
            classifier_version="fixture",
            source="fixture",
            evidence_json=[],
        )
    )
    source = storage.working_copy_path(version.storage_path)
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"finance-file")
    db.commit()
    return user, copy, storage


def _command(*, revision: int, key: str, action: str = "SET_PRIMARY", **target):
    data = {
        "working_copy_id": UUID(COPY_ID),
        "action": action,
        "expected_revision": revision,
        "expected_document_version_id": UUID(VERSION_ID),
        "taxonomy_version": "2026-09-v10",
        "idempotency_key": key,
        **target,
    }
    return PlacementCommand(**data)


def _submit(db, user, command, storage):
    authorization = PlacementAuthorizationService(db).authorize_structured_request(
        command=command,
        current_user=user,
        workspace_id=WORKSPACE_ID,
        client_id="test-api",
        request_id=f"request:{command.idempotency_key}",
        source_event_ref=f"test:{command.idempotency_key}",
    )
    service = ClassificationPlacementService(db, storage=storage)
    receipt = service.submit(command=command, authorization=authorization)
    db.commit()
    return service, receipt


def test_t20_set_primary_and_directory_move_commit_together_without_confirmation(monkeypatch, tmp_path):
    db = _session()
    try:
        user, _copy, storage = _seed(db, monkeypatch, tmp_path)
        command = _command(
            revision=1,
            key="set-primary-1",
            target_category_id="college.finance",
        )
        service, receipt = _submit(db, user, command, storage)
        assert receipt.status == "PREPARED"
        assert db.query(OperationConfirmation).count() == 0
        assert db.query(OperationPlan).one().status == "AUTHORIZED"

        result = service.execute(operation_id=receipt.operation_id, execution_token="worker-1")
        copy = db.get(WorkingCopy, COPY_ID)
        active_primary = db.query(DocumentCategory).filter(
            DocumentCategory.working_copy_id == COPY_ID,
            DocumentCategory.status.in_({"AUTO_APPLIED", "CONFIRMED"}),
            DocumentCategory.relation_role == "PRIMARY",
        ).one()

        assert result["status"] == "COMMITTED"
        assert copy.revision == 2
        assert copy.placement_status == "IN_SYNC"
        assert copy.relative_path == "学院/财务管理/财务材料.txt"
        assert active_primary.category_id == "college.finance"
        assert active_primary.status == "CONFIRMED"
        assert db.query(WorkingCopyPathRecord).count() == 1
        assert db.query(OperationConfirmation).count() == 0
        assert storage.working_copy_path("shared/working/其他/财务材料.txt").exists() is False
        assert storage.working_copy_path("shared/working/学院/财务管理/财务材料.txt").read_bytes() == b"finance-file"
    finally:
        db.close()


def test_t20_same_class_container_move_keeps_primary_and_preserves_filename(monkeypatch, tmp_path):
    db = _session()
    try:
        user, _copy, storage = _seed(db, monkeypatch, tmp_path)
        service, first = _submit(
            db,
            user,
            _command(revision=1, key="set-primary-2", target_category_id="college.finance"),
            storage,
        )
        service.execute(operation_id=first.operation_id, execution_token="worker-1")

        service, second = _submit(
            db,
            user,
            _command(
                revision=2,
                key="container-move-1",
                action="MOVE",
                target_root_key="placement-working",
                target_directory_segments=["学院", "财务管理", "2026材料"],
            ),
            storage,
        )
        result = service.execute(operation_id=second.operation_id, execution_token="worker-2")
        active = db.query(DocumentCategory).filter(
            DocumentCategory.working_copy_id == COPY_ID,
            DocumentCategory.status.in_({"AUTO_APPLIED", "CONFIRMED"}),
            DocumentCategory.relation_role == "PRIMARY",
        ).one()

        assert result["file_position_changed"] is True
        assert active.category_id == "college.finance"
        assert db.get(WorkingCopy, COPY_ID).relative_path == "学院/财务管理/2026材料/财务材料.txt"
        assert db.get(WorkingCopy, COPY_ID).filename == "财务材料.txt"
    finally:
        db.close()


def test_t21_noop_and_idempotency_replay_do_not_increment_revision_or_duplicate_audit(monkeypatch, tmp_path):
    db = _session()
    try:
        user, _copy, storage = _seed(db, monkeypatch, tmp_path)
        service, first = _submit(
            db,
            user,
            _command(revision=1, key="set-primary-3", target_category_id="college.finance"),
            storage,
        )
        service.execute(operation_id=first.operation_id, execution_token="worker-1")
        initial_item_count = db.query(ChangeItem).count()
        initial_revision = db.get(WorkingCopy, COPY_ID).revision

        service, noop = _submit(
            db,
            user,
            _command(revision=initial_revision, key="noop-1", target_category_id="college.finance"),
            storage,
        )
        result = service.execute(operation_id=noop.operation_id, execution_token="worker-2")
        replay_service, replay = _submit(
            db,
            user,
            _command(revision=initial_revision, key="noop-1", target_category_id="college.finance"),
            storage,
        )

        assert result["file_position_changed"] is False
        assert db.get(WorkingCopy, COPY_ID).revision == initial_revision
        assert db.query(ChangeItem).count() == initial_item_count
        assert replay.created is False
        assert replay.operation_id == noop.operation_id
        assert db.query(ClassificationPlacementOperation).count() == 2
        assert replay_service is not None
    finally:
        db.close()


def test_d4_worker_executes_frozen_placement_operation(monkeypatch, tmp_path):
    """队列负载仅携带 operation ID；worker 必须从冻结记录重建执行事实。"""

    db = _session()
    try:
        user, _copy, storage = _seed(db, monkeypatch, tmp_path)
        _service, receipt = _submit(
            db,
            user,
            _command(revision=1, key="worker-placement-1", target_category_id="college.finance"),
            storage,
        )
        job = db.query(FilesystemJob).one()
        assert job.payload_json == {"placement_operation_id": receipt.operation_id}
        claimed = FilesystemJobQueue(db).claim_next(worker_id="placement-worker")
        assert claimed is not None
        assert claimed.execution_token
        db.commit()

        assert FileLifecycleJobProcessor(db, settings=storage.settings).process(claimed) is True
        db.commit()

        assert db.get(FilesystemJob, job.id).status == "COMPLETED"
        assert db.get(ClassificationPlacementOperation, receipt.operation_id).state == "COMMITTED"
        assert db.get(WorkingCopy, COPY_ID).placement_status == "IN_SYNC"
    finally:
        db.close()
