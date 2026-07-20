"""为解析、分类和重命名统一解析实际可读文件源。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document
from app.modules.files.extractors import extraction_config_hash
from app.modules.files.office_conversion import (
    DOCX_CONTENT_TYPE,
    LegacyOfficeConversionService,
    OfficeConversionError,
    legacy_office_converter_config_hash,
)


@dataclass(frozen=True)
class ReadableDocumentSource:
    """原件和实际解析源之间的稳定映射。"""

    original_document_id: str
    original_path: Path
    parse_path: Path
    original_filename: str
    parse_filename: str
    original_content_type: str
    parse_content_type: str
    parser_config_hash: str | None
    artifact_id: str | None = None
    converted: bool = False
    reused: bool = False
    converter_name: str | None = None
    converter_version: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)


class ReadableDocumentSourceResolver:
    """让所有文件读取方共享同一旧格式转换结果。"""

    def __init__(
        self,
        *,
        db: Session,
        conversion_service: LegacyOfficeConversionService | None = None,
    ) -> None:
        """注入请求级数据库会话和可替换转换服务。"""

        self.db = db
        self._conversion_service = conversion_service
        self._resolved_cache: dict[tuple[str, str], ReadableDocumentSource] = {}

    @property
    def conversion_service(self) -> LegacyOfficeConversionService:
        """仅在处理旧版 DOC 时初始化 LibreOffice 转换依赖。"""

        if self._conversion_service is None:
            self._conversion_service = LegacyOfficeConversionService(db=self.db)
        return self._conversion_service

    def expected_parser_config_hash(self, *, document: Document, purpose: str = "document") -> str | None:
        """在读取原件前计算可复用解析运行的预期指纹。"""

        if not _is_legacy_doc(document.original_filename, document.content_type):
            return _parser_config_hash(filename=document.original_filename, purpose=purpose)
        settings = get_settings()
        if not settings.legacy_office_conversion_enabled or self.conversion_service.executable is None:
            return extraction_config_hash(filename=document.original_filename)
        conversion_hash = legacy_office_converter_config_hash(
            converter_name=settings.legacy_office_converter,
            converter_version=self.conversion_service.converter_version,
        )
        docx_hash = _parser_config_hash(
            filename=f"{Path(document.original_filename).stem}.docx",
            purpose=purpose,
        )
        identity = "|".join(
            [
                "legacy-doc-readable-source-v1",
                f"conversion={conversion_hash}",
                f"parser={docx_hash or 'python-docx-native'}",
            ]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def resolve(
        self,
        *,
        document: Document,
        original_path: Path,
        force_reconvert: bool = False,
        purpose: str = "document",
    ) -> ReadableDocumentSource:
        """返回实际解析源；转换异常时安全返回原始 DOC 供旧链路回退。"""

        original_path = original_path.resolve()
        cache_key = (document.id, purpose)
        if not force_reconvert and cache_key in self._resolved_cache:
            return self._resolved_cache[cache_key]
        if not _is_legacy_doc(document.original_filename, document.content_type):
            result = _original_source(document=document, original_path=original_path, purpose=purpose)
            self._resolved_cache[cache_key] = result
            return result
        try:
            artifact = self.conversion_service.get_or_create_docx(
                document=document,
                source_path=original_path,
                force_reconvert=force_reconvert,
            )
        except OfficeConversionError as exc:
            result = replace(
                _original_source(document=document, original_path=original_path, purpose=purpose),
                warnings=[
                    {
                        "code": exc.code,
                        "message": f"DOC 转 DOCX 失败，已回退旧版正文读取：{exc.message}",
                        "retryable": exc.retryable,
                    }
                ],
            )
            self._resolved_cache[cache_key] = result
            return result
        result = ReadableDocumentSource(
            original_document_id=document.id,
            original_path=original_path,
            parse_path=artifact.file_path,
            original_filename=document.original_filename,
            parse_filename=f"{Path(document.original_filename).stem}.docx",
            original_content_type=document.content_type,
            parse_content_type=DOCX_CONTENT_TYPE,
            parser_config_hash=self.expected_parser_config_hash(document=document, purpose=purpose),
            artifact_id=artifact.artifact_id,
            converted=True,
            reused=artifact.reused,
            converter_name=artifact.converter_name,
            converter_version=artifact.converter_version,
        )
        self._resolved_cache[cache_key] = result
        return result


def apply_readable_source_metadata(
    extraction: dict[str, Any],
    *,
    source: ReadableDocumentSource,
) -> dict[str, Any]:
    """把转换来源写入解析结果、页面和元素元数据。"""

    result = dict(extraction)
    warnings = [*list(result.get("warnings") or []), *source.warnings]
    if warnings:
        result["warnings"] = warnings
    result["parser_config_hash"] = source.parser_config_hash or result.get("parser_config_hash", "")
    result["conversion_artifact_id"] = source.artifact_id
    result["conversion_reused"] = source.reused if source.converted else None
    if not source.converted:
        return result
    result["conversion_source_format"] = "doc"
    result["conversion_parsed_format"] = "docx"
    result["conversion_converter"] = source.converter_name
    result["conversion_converter_version"] = source.converter_version
    source_metadata = {
        "source_format": "doc",
        "parsed_format": "docx",
        "conversion_artifact_id": source.artifact_id,
        "converter": source.converter_name,
        "converter_version": source.converter_version,
        "conversion_reused": source.reused,
    }
    pages = []
    for page in result.get("pages") or []:
        page_copy = dict(page)
        page_copy["metadata"] = {**dict(page_copy.get("metadata") or {}), **source_metadata}
        pages.append(page_copy)
    result["pages"] = pages
    elements = []
    for element in result.get("elements") or []:
        element_copy = dict(element)
        element_copy["metadata"] = {**dict(element_copy.get("metadata") or {}), **source_metadata}
        elements.append(element_copy)
    result["elements"] = elements
    return result


def _original_source(*, document: Document, original_path: Path, purpose: str) -> ReadableDocumentSource:
    """构造无需转换或转换失败后的原件读取源。"""

    return ReadableDocumentSource(
        original_document_id=document.id,
        original_path=original_path,
        parse_path=original_path,
        original_filename=document.original_filename,
        parse_filename=document.original_filename,
        original_content_type=document.content_type,
        parse_content_type=document.content_type,
        parser_config_hash=_parser_config_hash(filename=document.original_filename, purpose=purpose),
    )


def _is_legacy_doc(filename: str, content_type: str) -> bool:
    """根据扩展名和 MIME 判断旧版 Word 文件。"""

    return Path(filename).suffix.lower() == ".doc" or content_type == "application/msword"


def _parser_config_hash(*, filename: str, purpose: str) -> str | None:
    """按普通读取或重命名模式生成解析指纹。"""

    if purpose != "rename":
        return extraction_config_hash(filename=filename)
    settings = get_settings()
    if settings.file_rename_parse_mode != "native":
        return extraction_config_hash(filename=filename)
    suffix = Path(filename).suffix.lower().lstrip(".")
    identity = f"rename-native-v1|format={suffix}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
