"""旧版 Office 派生件转换与复用测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
import subprocess

from docx import Document as DocxDocument
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import Document, DocumentArtifact
from app.modules.files.office_conversion import (
    LegacyOfficeConversionService,
    OfficeConversionError,
    libreoffice_profile_uri,
    resolve_libreoffice_executable,
)


def _session():
    """创建包含完整 ORM 表的隔离数据库会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _document(*, source_path: Path, document_id: str) -> Document:
    """构造与源文件哈希一致的测试 Document。"""

    content = source_path.read_bytes()
    return Document(
        id=document_id,
        user_id=f"user-{document_id}",
        workspace_id=None,
        original_filename=source_path.name,
        content_type="application/msword",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _fake_converter(calls: list[list[str]]):
    """返回会生成合法 DOCX 的测试命令执行器。"""

    def run(command: list[str], *, timeout_seconds: int):
        """记录参数并在输出目录创建合法 DOCX。"""

        calls.append(command)
        output_dir = Path(command[command.index("--outdir") + 1])
        converted = DocxDocument()
        converted.add_heading("关于开展测试工作的通知", level=0)
        converted.add_paragraph("这是转换后的正文。")
        converted.save(output_dir / "source.docx")
        return subprocess.CompletedProcess(command, 0, stdout=b"converted", stderr=b"")

    return run


def test_resolve_libreoffice_executable_prefers_configured_path(tmp_path):
    """显式配置必须高于 PATH 和平台默认目录。"""

    executable = tmp_path / "custom-soffice"
    executable.write_bytes(b"")

    resolved = resolve_libreoffice_executable(
        configured=str(executable),
        platform_name="linux",
        environ={},
        which=lambda _: None,
    )

    assert resolved == executable


def test_resolve_libreoffice_executable_finds_windows_soffice_com_first(tmp_path):
    """Windows 默认目录中必须优先使用 soffice.com。"""

    program_files = tmp_path / "Program Files"
    executable = program_files / "LibreOffice" / "program" / "soffice.com"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"")

    resolved = resolve_libreoffice_executable(
        platform_name="win32",
        environ={"ProgramFiles": str(program_files)},
        which=lambda _: None,
    )

    assert resolved == executable


def test_libreoffice_profile_uri_supports_windows_drive_path():
    """Windows LibreOffice profile 必须使用合法 file URI。"""

    assert libreoffice_profile_uri(PureWindowsPath("C:/Temp/file-agent-profile")) == (
        "file:///C:/Temp/file-agent-profile"
    )


def test_doc_conversion_creates_and_reuses_persistent_artifact(monkeypatch, tmp_path):
    """同一 Document 第二次读取必须复用持久派生件。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    source_path = tmp_path / "notice.doc"
    source_path.write_bytes(b"legacy-doc-content")
    executable = tmp_path / "soffice"
    executable.write_bytes(b"")
    db = _session()
    document = _document(source_path=source_path, document_id="document-1")
    db.add(document)
    db.flush()
    calls: list[list[str]] = []
    service = LegacyOfficeConversionService(
        db=db,
        storage_root=tmp_path / "storage",
        executable=executable,
        command_runner=_fake_converter(calls),
        converter_version="LibreOffice Test 1.0",
    )

    first = service.get_or_create_docx(document=document, source_path=source_path)
    second = service.get_or_create_docx(document=document, source_path=source_path)

    assert first.reused is False
    assert second.reused is True
    assert first.storage_path == second.storage_path
    assert first.file_path.exists()
    assert len(calls) == 1
    assert db.query(DocumentArtifact).count() == 1


def test_same_content_across_documents_reuses_physical_artifact(monkeypatch, tmp_path):
    """跨用户同内容应复用物理文件，但保留独立 Artifact 权限记录。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    first_source = tmp_path / "first.doc"
    second_source = tmp_path / "second.doc"
    first_source.write_bytes(b"same-legacy-doc-content")
    second_source.write_bytes(b"same-legacy-doc-content")
    executable = tmp_path / "soffice"
    executable.write_bytes(b"")
    db = _session()
    first_document = _document(source_path=first_source, document_id="document-a")
    second_document = _document(source_path=second_source, document_id="document-b")
    db.add_all([first_document, second_document])
    db.flush()
    calls: list[list[str]] = []
    service = LegacyOfficeConversionService(
        db=db,
        storage_root=tmp_path / "storage",
        executable=executable,
        command_runner=_fake_converter(calls),
        converter_version="LibreOffice Test 1.0",
    )

    first = service.get_or_create_docx(document=first_document, source_path=first_source)
    second = service.get_or_create_docx(document=second_document, source_path=second_source)

    assert first.storage_path == second.storage_path
    assert second.reused is True
    assert len(calls) == 1
    artifacts = db.query(DocumentArtifact).order_by(DocumentArtifact.document_id).all()
    assert [item.document_id for item in artifacts] == ["document-a", "document-b"]
    assert len({item.storage_path for item in artifacts}) == 1


def test_doc_conversion_rejects_missing_output(monkeypatch, tmp_path):
    """LibreOffice 未生成 DOCX 时必须返回稳定错误码。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    source_path = tmp_path / "broken.doc"
    source_path.write_bytes(b"broken-content")
    executable = tmp_path / "soffice"
    executable.write_bytes(b"")
    db = _session()
    document = _document(source_path=source_path, document_id="document-broken")
    db.add(document)
    db.flush()

    def no_output(command: list[str], *, timeout_seconds: int):
        """模拟转换命令成功退出但没有生成输出。"""

        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    service = LegacyOfficeConversionService(
        db=db,
        storage_root=tmp_path / "storage",
        executable=executable,
        command_runner=no_output,
        converter_version="LibreOffice Test 1.0",
    )

    with pytest.raises(OfficeConversionError) as exc_info:
        service.get_or_create_docx(document=document, source_path=source_path)

    assert exc_info.value.code == "DOCX_OUTPUT_MISSING"
    assert db.query(DocumentArtifact).count() == 0
