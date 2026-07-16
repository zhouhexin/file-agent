"""重命名多解析器候选服务测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.modules.file_rename.parsing_service import (
    RenameParsingService,
    extract_rename_primary,
    rename_primary_config_hash,
)


def _settings(*, mode: str, docling_enabled: bool = True):
    """构造测试所需的最小配置。"""

    return SimpleNamespace(
        file_rename_parse_mode=mode,
        docling_enabled=docling_enabled,
        docling_formats=("pdf", "docx"),
        docling_ocr_enabled=False,
    )


def _primary_docling_result():
    """构造已持久化的 Docling 主解析结果。"""

    return {
        "ok": True,
        "extractor": "docling@2.50.0",
        "parser_name": "docling",
    }


def test_hybrid_collects_docling_and_native_candidates(monkeypatch, tmp_path):
    """hybrid 必须保留 Docling 主结果并补充原生候选。"""

    monkeypatch.setattr("app.modules.file_rename.parsing_service.get_settings", lambda: _settings(mode="hybrid"))
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text_native",
        lambda **_: {"ok": True, "extractor": "pdf", "pages": [{"text": "原生正文"}], "elements": []},
    )

    result = RenameParsingService().collect(
        file_path=tmp_path / "notice.pdf",
        filename="notice.pdf",
        content_type="application/pdf",
        primary_result=_primary_docling_result(),
        primary_pages=[{"text": "Docling 正文"}],
        primary_elements=[{"label": "title", "text": "Docling 标题"}],
    )

    assert [item.parser_name for item in result.candidates] == ["docling", "native"]


def test_native_mode_ignores_docling_primary_result(monkeypatch, tmp_path):
    """native 模式必须明确绕过已经存在的 Docling 结果。"""

    monkeypatch.setattr("app.modules.file_rename.parsing_service.get_settings", lambda: _settings(mode="native"))
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text_native",
        lambda **_: {"ok": True, "extractor": "docx", "pages": [{"text": "原生正文"}], "elements": []},
    )

    result = RenameParsingService().collect(
        file_path=tmp_path / "notice.docx",
        filename="notice.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        primary_result=_primary_docling_result(),
        primary_pages=[{"text": "Docling 正文"}],
    )

    assert [item.parser_name for item in result.candidates] == ["native"]


def test_docling_mode_falls_back_to_native_when_docling_is_disabled(monkeypatch, tmp_path):
    """Docling 未启用时，docling 模式也不能让重命名任务直接失败。"""

    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.get_settings",
        lambda: _settings(mode="docling", docling_enabled=False),
    )
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text_native",
        lambda **_: {"ok": True, "extractor": "docx", "pages": [{"text": "原生正文"}], "elements": []},
    )

    result = RenameParsingService().collect(
        file_path=tmp_path / "notice.docx",
        filename="notice.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert [item.parser_name for item in result.candidates] == ["native"]


def test_docling_mode_records_failure_and_falls_back_to_native(monkeypatch, tmp_path):
    """Docling 转换失败时必须记录原因并提供原生候选。"""

    monkeypatch.setattr("app.modules.file_rename.parsing_service.get_settings", lambda: _settings(mode="docling"))
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.try_parse_with_docling",
        lambda **_: {"ok": False, "error": {"code": "DOCLING_FAILED", "message": "转换失败"}},
    )
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text_native",
        lambda **_: {"ok": True, "extractor": "pdf", "pages": [{"text": "原生正文"}], "elements": []},
    )

    result = RenameParsingService().collect(
        file_path=tmp_path / "notice.pdf",
        filename="notice.pdf",
        content_type="application/pdf",
    )

    assert [item.parser_name for item in result.candidates] == ["native"]
    assert result.warnings[0]["code"] == "DOCLING_FAILED"


def test_native_primary_extraction_never_calls_docling_entry(monkeypatch, tmp_path):
    """native 主解析必须直接调用原生解析器，并使用独立配置指纹。"""

    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.get_settings",
        lambda: _settings(mode="native"),
    )
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text",
        lambda **_: pytest.fail("native 模式不应调用 Docling 优先入口"),
    )
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text_native",
        lambda **_: {"ok": True, "extractor": "docx", "pages": [{"text": "原生正文"}]},
    )

    result = extract_rename_primary(
        file_path=tmp_path / "notice.docx",
        filename="notice.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    assert result["parser_name"] == "native"
    assert result["parser_config_hash"] == rename_primary_config_hash(filename="notice.docx")
    assert len(result["parser_config_hash"]) == 64


@pytest.mark.parametrize("suffix", ["xls", "xlsx"])
def test_spreadsheets_use_native_candidate_in_hybrid_mode(monkeypatch, tmp_path, suffix):
    """非 Docling 格式在 hybrid 模式下继续只使用原生解析器。"""

    monkeypatch.setattr("app.modules.file_rename.parsing_service.get_settings", lambda: _settings(mode="hybrid"))
    monkeypatch.setattr(
        "app.modules.file_rename.parsing_service.extract_document_text_native",
        lambda **_: {"ok": True, "extractor": "excel", "pages": [{"text": "表格正文"}], "elements": []},
    )

    result = RenameParsingService().collect(
        file_path=Path(tmp_path / f"table.{suffix}"),
        filename=f"table.{suffix}",
        content_type="application/vnd.ms-excel",
    )

    assert [item.parser_name for item in result.candidates] == ["native"]
