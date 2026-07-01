"""文件上传和 Document 持久化测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import Document, DocumentInsight, FileObject
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


def test_upload_file_creates_document_and_file_object(monkeypatch, tmp_path):
    """上传文件后必须创建 Document、FileObject，并把原始文件保存到本地存储。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()

    response = client.post(
        "/api/files/upload",
        headers=_auth_header(client),
        files={"file": ("student.xlsx", b"student-file-content", "application/vnd.ms-excel")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["document_id"]
    assert data["filename"] == "student.xlsx"
    assert data["content_type"] == "application/vnd.ms-excel"
    assert data["size_bytes"] == len(b"student-file-content")
    assert data["status"] == "UPLOADED"
    assert data["ingest_status"] == "INGESTED"

    db = SessionLocal()
    try:
        document = db.get(Document, data["document_id"])
        assert document is not None
        assert document.original_filename == "student.xlsx"
        assert document.size_bytes == len(b"student-file-content")
        assert document.ingest_status == "INGESTED"

        file_object = db.query(FileObject).filter(FileObject.document_id == document.id).one()
        assert file_object.storage_backend == "local"
        assert (tmp_path / file_object.storage_path).read_bytes() == b"student-file-content"
        insight = db.query(DocumentInsight).filter(DocumentInsight.document_id == document.id).one()
        assert "student" in insight.keywords_json
        assert insight.labels_json
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_upload_duplicate_file_reuses_existing_document(monkeypatch, tmp_path):
    """同一用户重复上传相同文件时，应复用已有 Document 和处理结果。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    auth_header = _auth_header(client, username="dedupe-owner")

    first_response = client.post(
        "/api/files/upload",
        headers=auth_header,
        files={"file": ("first.txt", b"same-content", "text/plain")},
    )
    second_response = client.post(
        "/api/files/upload",
        headers=auth_header,
        files={"file": ("second.txt", b"same-content", "text/plain")},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["document_id"] == first_response.json()["document_id"]
    assert second_response.json()["deduplicated"] is True

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 1
        assert db.query(FileObject).count() == 1
        assert db.query(DocumentInsight).count() == 1
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_cross_user_duplicate_file_reuses_global_file_object(monkeypatch, tmp_path):
    """不同用户上传相同内容时，Document 隔离，但底层物理文件应全局复用。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    first_header = _auth_header(client, username="dedupe-user-a")
    second_header = _auth_header(client, username="dedupe-user-b")

    first_response = client.post(
        "/api/files/upload",
        headers=first_header,
        files={"file": ("shared-a.txt", b"same-content", "text/plain")},
    )
    second_response = client.post(
        "/api/files/upload",
        headers=second_header,
        files={"file": ("shared-b.txt", b"same-content", "text/plain")},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    first_document_id = first_response.json()["document_id"]
    second_document_id = second_response.json()["document_id"]
    assert second_document_id != first_document_id
    assert second_response.json()["deduplicated"] is True

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 2
        assert db.query(FileObject).count() == 2
        assert db.query(DocumentInsight).count() == 2
        first_file_object = db.query(FileObject).filter(FileObject.document_id == first_document_id).one()
        second_file_object = db.query(FileObject).filter(FileObject.document_id == second_document_id).one()
        assert second_file_object.storage_path == first_file_object.storage_path
        shared_path = tmp_path / first_file_object.storage_path
        assert shared_path.exists()
        assert shared_path.read_bytes() == b"same-content"
    finally:
        db.close()

    delete_response = client.delete(f"/api/files/{first_document_id}", headers=first_header)
    assert delete_response.status_code == 200
    assert shared_path.exists()

    content_response = client.get(f"/api/files/{second_document_id}/content", headers=second_header)
    assert content_response.status_code == 200
    assert content_response.content == b"same-content"

    db = SessionLocal()
    try:
        assert db.query(Document).count() == 1
        assert db.query(FileObject).count() == 1
        assert db.query(DocumentInsight).count() == 1
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_cross_user_duplicate_file_reads_after_source_document_locked(monkeypatch, tmp_path):
    """源用户文件进入对话锁定后，其他用户重传同内容仍应读取自己的 Document。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    first_header = _auth_header(client, username="locked-source-a")
    second_header = _auth_header(client, username="locked-source-b")

    first_response = client.post(
        "/api/files/upload",
        headers=first_header,
        files={"file": ("source.txt", b"locked-source-content", "text/plain")},
    )
    first_document_id = first_response.json()["document_id"]
    message_response = client.post(
        "/api/conversations/source-lock/messages",
        headers=first_header,
        json={"content": "读取这个文件", "attachments": [{"document_id": first_document_id}]},
    )
    assert message_response.status_code == 200

    second_response = client.post(
        "/api/files/upload",
        headers=second_header,
        files={"file": ("source-copy.txt", b"locked-source-content", "text/plain")},
    )

    assert second_response.status_code == 200
    assert second_response.json()["deduplicated"] is True
    second_document_id = second_response.json()["document_id"]
    assert second_document_id != first_document_id

    content_response = client.get(f"/api/files/{second_document_id}/content", headers=second_header)

    assert content_response.status_code == 200
    assert content_response.content == b"locked-source-content"

    db = SessionLocal()
    try:
        first_document = db.get(Document, first_document_id)
        second_document = db.get(Document, second_document_id)
        assert first_document.status == "USED_IN_MESSAGE"
        assert second_document.status == "UPLOADED"
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_get_file_content_returns_original_file(monkeypatch, tmp_path):
    """点击附件时应能通过 document_id 读取原始文件内容。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, _ = client_with_database()
    auth_header = _auth_header(client, username="content-owner")

    upload_response = client.post(
        "/api/files/upload",
        headers=auth_header,
        files={"file": ("preview.txt", b"preview-content", "text/plain")},
    )
    document_id = upload_response.json()["document_id"]

    response = client.get(f"/api/files/{document_id}/content", headers=auth_header)

    assert response.status_code == 200
    assert response.content == b"preview-content"
    assert response.headers["content-type"].startswith("text/plain")
    assert "preview.txt" in response.headers["content-disposition"]
    clear_overrides()
    config.get_settings.cache_clear()


def test_get_file_content_rejects_other_user_document(monkeypatch, tmp_path):
    """文件内容读取必须限制当前用户自己的 Document，避免跨用户下载附件。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, _ = client_with_database()
    owner_header = _auth_header(client, username="content-owner-a")
    viewer_header = _auth_header(client, username="content-viewer-b")

    upload_response = client.post(
        "/api/files/upload",
        headers=owner_header,
        files={"file": ("shared.txt", b"shared-content", "text/plain")},
    )
    document_id = upload_response.json()["document_id"]

    response = client.get(f"/api/files/{document_id}/content", headers=viewer_header)

    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"
    clear_overrides()
    config.get_settings.cache_clear()


def test_delete_uploaded_file_removes_database_rows_and_local_file(monkeypatch, tmp_path):
    """发送消息前删除上传文件时，必须同时删除数据库记录和本地文件。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    auth_header = _auth_header(client, username="delete-owner")

    upload_response = client.post(
        "/api/files/upload",
        headers=auth_header,
        files={"file": ("delete-me.png", b"image-content", "image/png")},
    )
    document_id = upload_response.json()["document_id"]

    db = SessionLocal()
    try:
        file_object = db.query(FileObject).filter(FileObject.document_id == document_id).one()
        stored_path = tmp_path / file_object.storage_path
        document_dir = stored_path.parent
        user_dir = document_dir.parent
        assert stored_path.exists()
        assert document_dir.exists()
        assert user_dir.exists()
    finally:
        db.close()

    delete_response = client.delete(f"/api/files/{document_id}", headers=auth_header)

    assert delete_response.status_code == 200
    assert delete_response.json() == {"deleted": True}
    assert not stored_path.exists()
    assert not document_dir.exists()
    assert not user_dir.exists()
    assert tmp_path.exists()

    db = SessionLocal()
    try:
        assert db.get(Document, document_id) is None
        assert db.query(FileObject).filter(FileObject.document_id == document_id).count() == 0
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_delete_file_after_message_is_rejected(monkeypatch, tmp_path):
    """文件进入对话后必须被锁定，不能再通过删除接口移除。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    auth_header = _auth_header(client, username="locked-owner")

    upload_response = client.post(
        "/api/files/upload",
        headers=auth_header,
        files={"file": ("locked.png", b"locked-image", "image/png")},
    )
    document_id = upload_response.json()["document_id"]

    message_response = client.post(
        "/api/conversations/locked-conv/messages",
        headers=auth_header,
        json={"content": "处理这张图片", "attachments": [{"document_id": document_id}]},
    )

    assert message_response.status_code == 200

    delete_response = client.delete(f"/api/files/{document_id}", headers=auth_header)

    assert delete_response.status_code == 409
    db = SessionLocal()
    try:
        document = db.get(Document, document_id)
        assert document is not None
        assert document.status == "USED_IN_MESSAGE"
        assert document.locked_message_id == message_response.json()["message"]["id"]
        assert document.locked_conversation_id == "locked-conv"
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()
