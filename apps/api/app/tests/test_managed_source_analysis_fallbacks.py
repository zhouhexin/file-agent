"""受管源图片降级索引与旧 DOC 统一转换入口回归测试。"""

from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    DocumentPage,
    ManagedFile,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedRoot,
)
from app.modules.managed_files import source_analysis


def test_image_without_text_becomes_metadata_only_success():
    """OCR 正常返回空文字时，文件仍应发布元数据投影并保留无文字提示。"""

    result = source_analysis._prepare_image_metadata_extraction(
        extraction={
            "ok": True,
            "status": "COMPLETED",
            "extractor": "paddleocr_cpu",
            "read_profile": {"file_type": "image", "has_text": False},
            "pages": [
                {
                    "page_number": 1,
                    "sheet_name": None,
                    "text": "",
                    "metadata": {"ocr_provider": "paddleocr_cpu"},
                }
            ],
            "warnings": [],
        },
        filename="IMG_0198.JPG",
        content_type="image/jpeg",
    )

    assert result["ok"] is True
    assert result["metadata_only"] is True
    assert result["warnings"][-1]["code"] == "IMAGE_NO_TEXT"
    assert "无可识别文字" in result["metadata_notice"]
    assert result["pages"][0]["text"] == ""
    assert result["pages"][0]["metadata"]["image_text_status"] == "NO_TEXT"


def test_image_ocr_failure_becomes_searchable_metadata_with_warning():
    """OCR 技术失败不能让图片从材料清单和元数据检索中消失。"""

    result = source_analysis._prepare_image_metadata_extraction(
        extraction={
            "ok": False,
            "status": "FAILED",
            "extractor": "ocr",
            "error": {
                "code": "OCR_ENGINE_NOT_AVAILABLE",
                "message": "private runtime details",
            },
            "pages": [],
        },
        filename="IMG_0199.JPG",
        content_type="image/jpeg",
    )

    assert result["ok"] is True
    assert result["metadata_only"] is True
    assert result["warnings"][-1]["code"] == "OCR_ENGINE_NOT_AVAILABLE"
    assert "private runtime details" not in result["warnings"][-1]["message"]
    assert result["pages"][0]["metadata"]["image_text_status"] == "OCR_FAILED"
    assert result["read_profile"]["requires_ocr"] is True


def test_managed_source_doc_uses_readable_source_resolver(monkeypatch, tmp_path):
    """受管源旧 DOC 必须先经过读取 LIBREOFFICE_EXECUTABLE 的统一可读源解析器。"""

    source_path = tmp_path / "流程.doc"
    source_path.write_bytes(b"legacy-doc")
    converted_path = tmp_path / "流程.docx"
    converted_path.write_bytes(b"converted-docx")
    calls = {}

    class FakeResolver:
        def __init__(self, *, db):
            calls["db"] = db

        def resolve(self, *, document, document_version, original_path):
            calls["document"] = document
            calls["document_version"] = document_version
            calls["original_path"] = original_path
            return SimpleNamespace(
                parse_path=converted_path,
                parse_filename="流程.docx",
                parse_content_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
            )

    def fake_extract_document_text(*, file_path, filename, content_type):
        calls["parse_input"] = (file_path, filename, content_type)
        return {"ok": True, "pages": []}

    monkeypatch.setattr(source_analysis, "ReadableDocumentSourceResolver", FakeResolver)
    monkeypatch.setattr(source_analysis, "extract_document_text", fake_extract_document_text)
    monkeypatch.setattr(
        source_analysis,
        "apply_readable_source_metadata",
        lambda extraction, *, source: {**extraction, "converted": source.parse_filename},
    )
    document = SimpleNamespace(id="managed-doc")
    document_version = SimpleNamespace(id="managed-version")

    result = source_analysis._extract_managed_source_document(
        db="db-session",
        document=document,
        document_version=document_version,
        source_path=source_path,
    )

    assert calls["db"] == "db-session"
    assert calls["document"] is document
    assert calls["document_version"] is document_version
    assert calls["original_path"] == source_path
    assert calls["parse_input"][0] == converted_path
    assert calls["parse_input"][1] == "流程.docx"
    assert result["converted"] == "流程.docx"


def test_table_shape_reads_end_row_and_end_column_without_blocking_on_bad_ranges():
    """Excel 范围应读取结束行列，畸形辅助范围不能使整份工作簿分析失败。"""

    row_count, column_count, headers = source_analysis._table_shape(
        "姓名\t单位\t费用\n潘志庚\t杭州师范大学\t960",
        [
            {"cell_range": "A1:C2"},
            {"cell_range": "$A$3:$AA$12"},
            {"cell_range": "b13"},
            {"cell_range": "not-a-cell-range"},
            None,
        ],
    )

    assert row_count == 13
    assert column_count == 27
    assert headers == ["姓名", "单位", "费用"]


def test_source_analysis_publishes_profile_when_image_ocr_fails(monkeypatch, tmp_path):
    """OCR 异常图片应成为 READY 元数据源，而不是停留在 FAILED。"""

    image_path = tmp_path / "20170606大数据联合实验室授牌" / "IMG_0198.JPG"
    image_path.parent.mkdir()
    image_path.write_bytes(b"not-used-by-fake-ocr")
    stat = image_path.stat()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    root = ManagedRoot(
        id="image-root",
        root_key="image-root",
        display_name="图片目录",
        container_path=str(tmp_path),
        enabled=True,
        created_by="user-image",
    )
    managed_file = ManagedFile(
        id="image-file",
        root_id=root.id,
        relative_path="20170606大数据联合实验室授牌/IMG_0198.JPG",
        relative_path_hash="image-path",
        filename="IMG_0198.JPG",
        extension=".jpg",
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        fingerprint="image-fingerprint",
        status="ACTIVE",
    )
    revision = ManagedFileRevision(
        id="image-revision",
        managed_file_id=managed_file.id,
        revision_number=1,
        size_bytes=stat.st_size,
        modified_at=managed_file.modified_at,
        quick_fingerprint=managed_file.fingerprint,
        status="ANALYSIS_PENDING",
        analysis_status="PENDING",
        is_current=True,
    )
    db.add_all([root, managed_file, revision])
    db.flush()
    monkeypatch.setattr(
        source_analysis,
        "_extract_managed_source_document",
        lambda **_kwargs: {
            "ok": False,
            "status": "FAILED",
            "extractor": "ocr",
            "error": {
                "code": "OCR_ENGINE_NOT_AVAILABLE",
                "message": "runtime failure",
            },
            "pages": [],
        },
    )
    monkeypatch.setattr(source_analysis, "log_event", lambda *_args, **_kwargs: None)

    result = source_analysis.ManagedSourceAnalysisService(db=db).analyze(
        revision_id=revision.id,
        user_id="user-image",
    )

    assert result["status"] == "READY"
    assert result["index_run_id"] is None
    assert result["warnings"][-1]["code"] == "OCR_ENGINE_NOT_AVAILABLE"
    assert revision.status == "READY"
    profile = db.query(ManagedFileSearchProfile).filter(
        ManagedFileSearchProfile.managed_file_revision_id == revision.id
    ).one()
    assert profile.status == "ACTIVE"
    assert "实验室" in profile.search_text
    assert "授牌" in profile.search_text
    assert "OCR" in profile.summary
    page = db.query(DocumentPage).one()
    assert page.text_content == ""
    assert page.metadata_json["image_text_status"] == "OCR_FAILED"
    db.close()
