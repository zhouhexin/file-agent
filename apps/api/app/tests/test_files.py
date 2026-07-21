"""上传暂存、异步生命周期和附件删除边界测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import (
    Document,
    DocumentVersion,
    FileObject,
    FilesystemJob,
    UploadArchiveRecord,
    UploadDuplicateReview,
)
from app.modules.managed_files.worker import process_next_filesystem_job
from app.tests.helpers import clear_overrides, client_with_database


def _auth_header(client, username: str = "file-user") -> dict[str, str]:
    """注册并登录测试用户，返回 Authorization header。"""

    client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {login_response.json()['access_token']}"}


def _configure_storage(monkeypatch, tmp_path) -> None:
    """把三层存储全部隔离到当前测试目录。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    config.get_settings.cache_clear()


def _drain_jobs(SessionLocal, *, maximum: int = 20) -> list[str]:
    """同步驱动测试数据库中的 worker；生产环境仍由独立进程消费。"""

    processed: list[str] = []
    for _ in range(maximum):
        job_id = process_next_filesystem_job(session_factory=SessionLocal, worker_id="test-worker")
        if job_id is None:
            break
        processed.append(job_id)
    return processed


def test_upload_creates_version_review_and_persistent_job(monkeypatch, tmp_path):
    """上传请求只保存暂存和创建任务，不得同步归档或导入。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    response = client.post(
        "/api/files/upload",
        headers=_auth_header(client),
        files={"file": ("student.xlsx", b"student-file-content", "application/vnd.ms-excel")},
    )

    assert response.status_code == 202
    data = response.json()
    assert data["status"] == "UPLOADED"
    assert data["ingest_status"] == "DUPLICATE_CHECK_PENDING"
    assert data["deduplicated"] is False
    assert data["upload_document_version_id"]
    assert data["duplicate_review_id"]
    assert data["filesystem_job_id"]

    db = SessionLocal()
    try:
        document = db.get(Document, data["document_id"])
        version = db.get(DocumentVersion, data["upload_document_version_id"])
        review = db.get(UploadDuplicateReview, data["duplicate_review_id"])
        archive = db.query(UploadArchiveRecord).filter_by(upload_document_version_id=version.id).one()
        job = db.get(FilesystemJob, data["filesystem_job_id"])
        file_object = db.query(FileObject).filter_by(document_id=document.id).one()
        assert version.storage_tier == "UPLOAD"
        assert review.status == "CHECKING"
        assert archive.status == "DUPLICATE_CHECK_PENDING"
        assert job.status == "PENDING"
        assert (tmp_path / "uploads" / file_object.storage_path).read_bytes() == b"student-file-content"
        assert not (tmp_path / "originals").exists()
        assert not (tmp_path / "working").exists()
    finally:
        db.close()
        clear_overrides()


def test_same_content_uploads_remain_distinct_until_dialog_decision(monkeypatch, tmp_path):
    """同内容上传不能在请求线程静默复用 Document 或物理暂存文件。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "distinct-upload-owner")
    first = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("first.txt", b"same-content", "text/plain")},
    )
    second = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("second.txt", b"same-content", "text/plain")},
    )

    assert first.status_code == second.status_code == 202
    assert first.json()["document_id"] != second.json()["document_id"]
    db = SessionLocal()
    try:
        objects = db.query(FileObject).order_by(FileObject.created_at.asc()).all()
        assert len(objects) == 2
        assert objects[0].storage_path != objects[1].storage_path
    finally:
        db.close()
        clear_overrides()


def test_get_file_content_enforces_owner(monkeypatch, tmp_path):
    """暂存附件读取必须校验所属用户，不能因为内容相同跨用户共享路径。"""

    _configure_storage(monkeypatch, tmp_path)
    client, _ = client_with_database()
    owner = _auth_header(client, "content-owner")
    viewer = _auth_header(client, "content-viewer")
    upload = client.post(
        "/api/files/upload",
        headers=owner,
        files={"file": ("preview.txt", b"preview-content", "text/plain")},
    )
    document_id = upload.json()["document_id"]

    own_response = client.get(f"/api/files/{document_id}/content", headers=owner)
    cross_response = client.get(f"/api/files/{document_id}/content", headers=viewer)

    assert own_response.status_code == 200
    assert own_response.content == b"preview-content"
    assert cross_response.status_code == 404
    clear_overrides()


def test_delete_unsent_upload_cancels_lifecycle_and_cleans_asynchronously(monkeypatch, tmp_path):
    """未发送附件可删除；保留审计记录，但物理暂存由 worker 异步清理。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "delete-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("delete-me.png", b"image-content", "image/png")},
    )
    document_id = upload.json()["document_id"]
    db = SessionLocal()
    try:
        file_object = db.query(FileObject).filter_by(document_id=document_id).one()
        stored_path = tmp_path / "uploads" / file_object.storage_path
        assert stored_path.exists()
    finally:
        db.close()

    deleted = client.delete(f"/api/files/{document_id}", headers=headers)

    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True
    assert deleted.json()["cleanup_job_id"]
    assert stored_path.exists()
    _drain_jobs(SessionLocal)
    assert not stored_path.exists()
    db = SessionLocal()
    try:
        # Document/版本承担取消审计，不因清理暂存而被物理删除。
        assert db.get(Document, document_id).status == "UPLOAD_CANCELLED"
        review = db.query(UploadDuplicateReview).filter_by(
            upload_document_version_id=upload.json()["upload_document_version_id"]
        ).one()
        assert review.decision == "CANCEL_UPLOAD"
    finally:
        db.close()
        clear_overrides()


def test_delete_file_after_message_is_rejected(monkeypatch, tmp_path):
    """附件真正进入消息后必须保留引用，不能再作为未发送暂存删除。"""

    _configure_storage(monkeypatch, tmp_path)
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "locked-owner")
    upload = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": ("locked.png", b"locked-image", "image/png")},
    )
    document_id = upload.json()["document_id"]
    sent = client.post(
        "/api/conversations/locked-conv/messages",
        headers=headers,
        json={"content": "处理这张图片", "attachments": [{"document_id": document_id}]},
    )

    assert sent.status_code == 200
    deleted = client.delete(f"/api/files/{document_id}", headers=headers)
    assert deleted.status_code == 409
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        assert document.status == "USED_IN_MESSAGE"
        assert document.locked_message_id == sent.json()["message"]["id"]
    finally:
        db.close()
        clear_overrides()
