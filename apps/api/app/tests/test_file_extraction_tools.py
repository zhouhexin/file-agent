"""原始文件读取与文本解析 Tool 测试。"""

from __future__ import annotations

import subprocess
from io import BytesIO

from fastapi.testclient import TestClient

from app.core import config
from app.db.models import DocumentExtractionRun, DocumentPage
from app.modules.files.extractors import extract_document_text
from app.modules.agent.tool_registry import ToolRegistry
from app.tests.helpers import clear_overrides, client_with_database


class FakeOcrService:
    """测试用 OCR 服务，避免依赖真实 PaddleOCR 或外部 LLM。"""

    def __init__(self, text: str = "OCR 识别文本", source: str = "paddleocr_cpu", quality_score: float = 0.9):
        """保存固定 OCR 返回值。"""

        self.text = text
        self.source = source
        self.quality_score = quality_score
        self.calls: list[dict] = []

    def extract_image(self, *, image_path, page_number: int = 1):
        """记录调用并返回固定 OCR 页面。"""

        self.calls.append({"image_path": image_path, "page_number": page_number})
        return {
            "ok": True,
            "text": self.text,
            "source": self.source,
            "provider_name": self.source,
            "quality_score": self.quality_score,
            "confidence": 0.88,
            "blocks": [],
            "warnings": [],
        }


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
        assert result.output_json["read_quality"] == "GOOD"
        assert result.output_json["read_profile"]["file_type"] == "text"
        assert result.output_json["read_profile"]["char_count"] > 0
        assert result.output_json["pages"][0]["char_count"] > 0
        assert db.query(DocumentExtractionRun).count() == 1
        page = db.query(DocumentPage).one()
        assert page.document_id == document_id
        assert "张三" in page.text_content
        assert page.metadata_json["read_quality"] == "GOOD"
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


def test_extract_document_text_supports_doc_with_textutil(monkeypatch, tmp_path):
    """extract-document-text 应通过系统转换器读取旧版 doc 正文。"""

    doc_path = tmp_path / "promise.doc"
    doc_path.write_bytes(b"legacy-doc")

    def fake_which(name: str) -> str | None:
        """测试中只模拟 textutil 可用，避免依赖本机 Office 工具。"""

        return "/usr/bin/textutil" if name == "textutil" else None

    def fake_run(*args, **kwargs) -> subprocess.CompletedProcess[bytes]:
        """模拟 textutil 将 doc 转成纯文本输出。"""

        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="电子发票承诺书\n经办人：李四".encode("utf-8"), stderr=b"")

    monkeypatch.setattr("app.modules.files.extractors.shutil.which", fake_which)
    monkeypatch.setattr("app.modules.files.extractors.subprocess.run", fake_run)

    result = extract_document_text(
        file_path=doc_path,
        filename="电子发票承诺书.doc",
        content_type="application/msword",
    )

    assert result["ok"] is True
    assert result["status"] == "COMPLETED"
    assert result["extractor"] == "doc-textutil"
    assert "电子发票承诺书" in result["pages"][0]["text"]


def test_extract_document_text_converts_xls_before_reading(monkeypatch, tmp_path):
    """旧版 xls 必须先转换为派生 xlsx，再交给 openpyxl 读取。"""

    xls_path = tmp_path / "奖学金汇总.xls"
    xls_path.write_bytes(b"legacy-xls")

    def fake_converter(*, source_path, output_dir):
        """模拟 LibreOffice 生成 xlsx 派生件，避免测试依赖本机办公套件。"""

        assert source_path == xls_path
        converted_path = output_dir / "奖学金汇总.xlsx"
        workbook = __import__("openpyxl").Workbook()
        worksheet = workbook.active
        worksheet.title = "汇总"
        worksheet.append(["姓名", "等级"])
        worksheet.append(["赵六", "一等奖"])
        workbook.save(converted_path)
        return converted_path

    monkeypatch.setattr("app.modules.files.extractors.convert_xls_to_xlsx", fake_converter)

    result = extract_document_text(
        file_path=xls_path,
        filename="奖学金汇总.xls",
        content_type="application/vnd.ms-excel",
    )

    assert result["ok"] is True
    assert result["status"] == "COMPLETED"
    assert result["extractor"] == "excel-xls-converted"
    assert result["pages"][0]["sheet_name"] == "汇总"
    assert "赵六\t一等奖" in result["pages"][0]["text"]
    assert result["pages"][0]["metadata"]["converted_from"] == ".xls"


def test_extract_image_uses_injected_ocr_service(tmp_path):
    """图片解析应通过 OCR 服务写入统一页面文本。"""

    image_path = tmp_path / "scan.png"
    image_path.write_bytes(b"fake-image")
    ocr_service = FakeOcrService(text="电子发票承诺书 OCR 文本")

    result = extract_document_text(
        file_path=image_path,
        filename="scan.png",
        content_type="image/png",
        ocr_service=ocr_service,
    )

    assert result["ok"] is True
    assert result["extractor"] == "paddleocr_cpu"
    assert result["pages"][0]["text"] == "电子发票承诺书 OCR 文本"
    assert result["pages"][0]["metadata"]["ocr_source"] == "paddleocr_cpu"
    assert ocr_service.calls[0]["page_number"] == 1


def test_empty_pdf_triggers_ocr_fallback(monkeypatch, tmp_path):
    """PDF 原生文本为空时应渲染页面并触发 OCR 兜底。"""

    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"fake-pdf")
    rendered_page = tmp_path / "page-1.png"
    rendered_page.write_bytes(b"fake-render")
    ocr_service = FakeOcrService(text="扫描 PDF OCR 文本")

    monkeypatch.setattr(
        "app.modules.files.extractors._extract_pdf_native_pages",
        lambda file_path: [{"page_number": 1, "sheet_name": None, "text": "", "metadata": {"page_index": 0}}],
    )
    monkeypatch.setattr(
        "app.modules.files.extractors._render_pdf_pages_for_ocr",
        lambda file_path, page_numbers: {1: rendered_page},
    )

    result = extract_document_text(
        file_path=pdf_path,
        filename="scan.pdf",
        content_type="application/pdf",
        ocr_service=ocr_service,
    )

    assert result["ok"] is True
    assert result["extractor"] == "pdf+paddleocr_cpu"
    assert result["pages"][0]["text"] == "扫描 PDF OCR 文本"
    assert result["pages"][0]["metadata"]["ocr_fallback"] is True
    assert ocr_service.calls[0]["image_path"] == rendered_page


def test_empty_pdf_marks_ocr_needed_when_ocr_disabled(monkeypatch, tmp_path):
    """PDF 原生文本为空且 OCR 关闭时，应返回统一读取质量而不是误报 GOOD。"""

    monkeypatch.setenv("OCR_ENABLED", "false")
    config.get_settings.cache_clear()
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"fake-pdf")
    monkeypatch.setattr(
        "app.modules.files.extractors._extract_pdf_native_pages",
        lambda file_path: [{"page_number": 1, "sheet_name": None, "text": "", "metadata": {"page_index": 0}}],
    )

    result = extract_document_text(
        file_path=pdf_path,
        filename="scan.pdf",
        content_type="application/pdf",
    )

    assert result["ok"] is True
    assert result["read_quality"] == "OCR_NEEDED"
    assert result["read_profile"]["requires_ocr"] is True
    assert result["read_profile"]["char_count"] == 0
    assert result["pages"][0]["metadata"]["read_quality"] == "OCR_NEEDED"
    config.get_settings.cache_clear()
