"""原始文件读取与文本解析 Tool 测试。"""

from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient

from app.core import config
from app.db.models import DocumentExtractionRun, DocumentPage
from app.modules.agent.tool_registry import ToolRegistry
from app.tests.helpers import clear_overrides, client_with_database


def _auth_header(client: TestClient, username: str) -> dict[str, str]:
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


def _upload_text(client: TestClient, headers: dict[str, str], filename: str = "notes.txt") -> str:
    """上传一个 UTF-8 文本文件并返回 document_id。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={"file": (filename, "学生姓名：张三\n奖学金：一等奖\n".encode("utf-8"), "text/plain")},
    )
    assert response.status_code == 200
    return response.json()["document_id"]


def _docx_bytes() -> bytes:
    """构造包含中文正文的 docx 测试文件。"""

    from docx import Document as DocxDocument

    document = DocxDocument()
    document.add_paragraph("学生姓名：王五")
    document.add_paragraph("奖学金：三等奖")
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _upload_docx(client: TestClient, headers: dict[str, str]) -> str:
    """上传一个 docx 文件并返回 document_id。"""

    response = client.post(
        "/api/files/upload",
        headers=headers,
        files={
            "file": (
                "student.docx",
                _docx_bytes(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 200
    return response.json()["document_id"]


def test_extraction_tables_can_be_created():
    """文件解析运行表和页面表必须纳入 ORM metadata。"""

    assert DocumentExtractionRun.__tablename__ == "document_extraction_runs"
    assert DocumentPage.__tablename__ == "document_pages"


def test_read_original_file_returns_metadata_for_owner(monkeypatch, tmp_path):
    """read-original-file 只能读取当前用户自己的文件元信息。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "file-reader")
    document_id = _upload_text(client, headers)

    db = SessionLocal()
    try:
        user_id = client.get("/api/auth/me", headers=headers).json()["id"]
        result = ToolRegistry(db=db, user_id=user_id).invoke(
            "read-original-file",
            {"document_id": document_id},
        )

        assert result.status == "COMPLETED"
        assert result.output_json["ok"] is True
        assert result.output_json["document_id"] == document_id
        assert result.output_json["filename"] == "notes.txt"
        assert result.output_json["storage_backend"] == "local"
        assert "storage_path" not in result.output_json
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_read_original_file_rejects_other_users_document(monkeypatch, tmp_path):
    """read-original-file 不能跨用户读取文件。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    owner_headers = _auth_header(client, "file-owner")
    other_headers = _auth_header(client, "file-other")
    document_id = _upload_text(client, owner_headers)

    db = SessionLocal()
    try:
        other_user_id = client.get("/api/auth/me", headers=other_headers).json()["id"]
        result = ToolRegistry(db=db, user_id=other_user_id).invoke(
            "read-original-file",
            {"document_id": document_id},
        )

        assert result.output_json["ok"] is False
        assert result.output_json["error"]["code"] == "DOCUMENT_NOT_FOUND"
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_extract_document_text_persists_text_pages(monkeypatch, tmp_path):
    """extract-document-text 应解析文本文件并持久化 DocumentPage。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "text-extractor")
    document_id = _upload_text(client, headers)

    db = SessionLocal()
    try:
        user_id = client.get("/api/auth/me", headers=headers).json()["id"]
        result = ToolRegistry(db=db, user_id=user_id).invoke(
            "extract-document-text",
            {"document_id": document_id},
        )

        assert result.output_json["ok"] is True
        assert result.output_json["status"] == "COMPLETED"
        assert result.output_json["pages"][0]["char_count"] > 0
        assert db.query(DocumentExtractionRun).count() == 1
        page = db.query(DocumentPage).one()
        assert page.document_id == document_id
        assert "张三" in page.text_content
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()


def test_extract_document_text_persists_docx_pages(monkeypatch, tmp_path):
    """extract-document-text 应解析 docx 正文并持久化 DocumentPage。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path))
    config.get_settings.cache_clear()
    client, SessionLocal = client_with_database()
    headers = _auth_header(client, "docx-extractor")
    document_id = _upload_docx(client, headers)

    db = SessionLocal()
    try:
        user_id = client.get("/api/auth/me", headers=headers).json()["id"]
        result = ToolRegistry(db=db, user_id=user_id).invoke(
            "extract-document-text",
            {"document_id": document_id},
        )

        assert result.output_json["ok"] is True
        assert result.output_json["status"] == "COMPLETED"
        assert result.output_json["extractor"] == "docx"
        page = db.query(DocumentPage).one()
        assert page.document_id == document_id
        assert "王五" in page.text_content
        assert "三等奖" in page.text_content
    finally:
        db.close()
        clear_overrides()
        config.get_settings.cache_clear()
