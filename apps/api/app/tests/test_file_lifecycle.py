"""受管原始目录、工作副本目录和回收站目录完整生命周期测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import (
    ChangeItem,
    Document,
    DocumentVersion,
    ManagedFile,
    TrashEntry,
    UploadArchiveRecord,
    UploadDuplicateReview,
    WorkingCopy,
    WorkingCopyPathRecord,
)
from app.modules.file_rename.uploaded_suggestion_service import UploadedRenameSuggestionService
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


def _configure(monkeypatch, tmp_path) -> None:
    """配置测试专用三层目录。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    monkeypatch.setenv("MANAGED_ROOT_RECONCILE_ON_STARTUP", "false")
    config.get_settings.cache_clear()


def _auth(client, username: str) -> dict[str, str]:
    """注册测试用户并返回认证头。"""

    client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    token = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _upload(client, headers, filename: str = "2024年度通知.txt", content: bytes = b"annual notice") -> dict:
    """上传一个测试附件。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, content, "text/plain")},
    )
    assert response.status_code == 202
    return response.json()


def _drain(SessionLocal, maximum: int = 30) -> list[str]:
    """在测试进程中驱动独立 worker 逻辑直至当前队列为空。"""

    job_ids: list[str] = []
    for _ in range(maximum):
        job_id = process_next_filesystem_job(session_factory=SessionLocal, worker_id="lifecycle-test")
        if job_id is None:
            break
        job_ids.append(job_id)
    return job_ids


def test_upload_is_archived_then_imported_by_separate_jobs(monkeypatch, tmp_path):
    """查重、归档和导入必须串联为三个持久化任务并建立完整追溯关系。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "pipeline-owner")
    upload = _upload(client, headers)

    processed = _drain(SessionLocal)

    assert len(processed) == 3
    status = client.get(
        f"/api/uploads/{upload['upload_document_version_id']}/archive-status",
        headers=headers,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "ARCHIVED"
    assert status.json()["managed_file_id"]
    assert status.json()["working_copy_id"]
    db = SessionLocal()
    try:
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        original = db.get(ManagedFile, archive.managed_file_id)
        working_copy = db.get(WorkingCopy, status.json()["working_copy_id"])
        version = db.get(DocumentVersion, working_copy.current_version_id)
        original_path = tmp_path / "originals" / original.relative_path
        working_path = tmp_path / "working" / version.storage_path
        assert original.source_type == "UPLOAD_ARCHIVE"
        assert original.source_upload_version_id == upload["upload_document_version_id"]
        assert original_path.read_bytes() == b"annual notice"
        assert working_path.read_bytes() == b"annual notice"
        assert original_path != working_path
        assert working_copy.managed_file_id == original.id
        assert version.source_managed_file_id == original.id
        source_document = db.get(Document, upload["document_id"])
        resolved = UploadedRenameSuggestionService(
            db=db,
            user_id=source_document.user_id,
        )._resolve_working_copy(source_document=source_document)
        # 上传附件重命名必须先穿过归档关系，最终只得到活动工作副本。
        assert resolved is not None
        assert resolved.id == working_copy.id
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "ORIGINAL_FILE_ARCHIVED").count() == 1
        assert db.query(ChangeItem).filter(ChangeItem.change_type == "WORKING_COPY_IMPORTED").count() == 1
    finally:
        db.close()
        clear_overrides()


def test_duplicate_upload_waits_for_dialog_and_can_use_existing(monkeypatch, tmp_path):
    """发现同工作区重复内容时必须暂停归档，由用户选择已有工作副本。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "duplicate-owner")
    first = _upload(client, headers, "first.txt", b"identical body")
    _drain(SessionLocal)
    second = _upload(client, headers, "second.txt", b"identical body")

    process_next_filesystem_job(session_factory=SessionLocal, worker_id="duplicate-test")
    review_response = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=headers,
    )

    assert review_response.status_code == 200
    review = review_response.json()
    assert review["status"] == "WAITING_CONFIRMATION"
    assert "USE_EXISTING_FILE" in review["allowed_decisions"]
    assert review["candidates"][0]["match_type"] == "EXACT_SHA256"
    existing_copy_id = review["candidates"][0]["existing_working_copy_id"]
    existing_document_id = review["candidates"][0]["existing_document_id"]
    decision = client.post(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review/decision",
        headers=headers,
        json={
            "duplicate_review_id": review["id"],
            "decision": "USE_EXISTING_FILE",
            "selected_existing_working_copy_id": existing_copy_id,
        },
    )
    assert decision.status_code == 202
    assert decision.json()["selected_existing_document_id"] == existing_document_id
    assert decision.json()["archive_status"] == "EXISTING_FILE_SELECTED"
    db = SessionLocal()
    try:
        second_archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=second["upload_document_version_id"]
        ).one()
        assert second_archive.managed_file_id is None
        assert db.query(ManagedFile).count() == 1
        assert db.query(WorkingCopy).count() == 1
    finally:
        db.close()
        clear_overrides()


def test_cross_user_duplicate_candidate_is_sanitized(monkeypatch, tmp_path):
    """跨用户重复候选只能提示存在相同内容，不能暴露文件名、路径或业务 ID。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    first_headers = _auth(client, "private-owner")
    second_headers = _auth(client, "other-uploader")
    _upload(client, first_headers, "机密姓名名单.txt", b"cross user duplicate")
    _drain(SessionLocal)
    second = _upload(client, second_headers, "copy.txt", b"cross user duplicate")
    process_next_filesystem_job(session_factory=SessionLocal, worker_id="cross-user-test")

    review = client.get(
        f"/api/uploads/{second['upload_document_version_id']}/duplicate-review",
        headers=second_headers,
    ).json()

    candidate = review["candidates"][0]
    assert candidate["match_scope"] == "CROSS_USER"
    assert candidate["existing_working_copy_id"] is None
    assert candidate["existing_document_id"] is None
    assert "filename" not in candidate["summary"]
    assert "relative_path" not in candidate["summary"]
    assert review["allowed_decisions"] == ["CONTINUE_UPLOAD", "CANCEL_UPLOAD"]
    clear_overrides()


def test_rename_move_trash_and_restore_only_change_working_copy(monkeypatch, tmp_path):
    """路径操作不新增版本、不改原始文件；删除进入回收站且可恢复。"""

    _configure(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth(client, "operation-owner")
    upload = _upload(client, headers, "04级工程硕士开课通知.doc", b"legacy document")
    _drain(SessionLocal)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]

    rename_plan = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "working-copy-operations",
            "operation_type": "RENAME_WORKING_COPIES",
            "reason": "规范文件名",
            "items": [
                {
                    "working_copy_id": working_copy["id"],
                    "after": {"filename": "2004级工程硕士开课通知.doc"},
                    "rename_metadata": {"policy_key": "school-file-rename", "year": {"value": "2004"}},
                }
            ],
        },
    )
    assert rename_plan.status_code == 200
    assert rename_plan.json()["items"][0]["rename_metadata"]["year"]["value"] == "2004"
    rename_result = client.post(
        f"/api/operations/plans/{rename_plan.json()['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认重命名"},
    )
    assert rename_result.status_code == 200
    assert rename_result.json()["status"] == "EXECUTED"

    renamed = client.get(f"/api/working-copies/{working_copy['id']}", headers=headers).json()
    assert renamed["filename"] == "2004级工程硕士开课通知.doc"
    assert "2004" in renamed["relative_path"]
    versions = client.get(
        f"/api/working-copies/{working_copy['id']}/versions",
        headers=headers,
    ).json()
    assert len(versions) == 1

    trash_plan = client.post(
        "/api/operations/plans",
        headers=headers,
        json={
            "conversation_id": "working-copy-operations",
            "operation_type": "TRASH_WORKING_COPIES",
            "reason": "用户请求删除工作副本",
            "items": [{"working_copy_id": working_copy["id"]}],
        },
    ).json()
    trash_result = client.post(
        f"/api/operations/plans/{trash_plan['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认移入回收站"},
    )
    assert trash_result.json()["status"] == "EXECUTED"
    trash_entries = client.get("/api/trash-entries", headers=headers).json()
    assert len(trash_entries) == 1

    restore_plan = client.post(
        f"/api/trash-entries/{trash_entries[0]['id']}/restore-plan",
        headers=headers,
        json={"conversation_id": "working-copy-operations"},
    ).json()
    restored = client.post(
        f"/api/operations/plans/{restore_plan['id']}/confirm",
        headers=headers,
        json={"confirmation": "确认恢复"},
    )
    assert restored.json()["status"] == "EXECUTED"

    db = SessionLocal()
    try:
        copy = db.get(WorkingCopy, working_copy["id"])
        archive = db.query(UploadArchiveRecord).filter_by(
            upload_document_version_id=upload["upload_document_version_id"]
        ).one()
        original = db.get(ManagedFile, archive.managed_file_id)
        records = db.query(WorkingCopyPathRecord).filter_by(working_copy_id=copy.id).all()
        work_document = db.get(Document, copy.document_id)
        assert copy.status == "ACTIVE"
        assert work_document.original_filename == "2004级工程硕士开课通知.doc"
        assert db.query(DocumentVersion).filter_by(document_id=copy.document_id).count() == 1
        assert len(records) == 1
        assert records[0].status == "COMPLETED"
        assert records[0].after_filename == "2004级工程硕士开课通知.doc"
        assert (tmp_path / "originals" / original.relative_path).read_bytes() == b"legacy document"
        assert db.query(TrashEntry).filter_by(working_copy_id=copy.id, status="RESTORED").count() == 1
    finally:
        db.close()
        clear_overrides()
