"""文件上传和 Document 持久化测试。"""

from __future__ import annotations

from app.core import config
from app.db.models import Document, FileObject
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

    db = SessionLocal()
    try:
        document = db.get(Document, data["document_id"])
        assert document is not None
        assert document.original_filename == "student.xlsx"
        assert document.size_bytes == len(b"student-file-content")

        file_object = db.query(FileObject).filter(FileObject.document_id == document.id).one()
        assert file_object.storage_backend == "local"
        assert (tmp_path / file_object.storage_path).read_bytes() == b"student-file-content"
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()
