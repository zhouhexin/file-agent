"""为解析、分类和重命名统一解析实际可读文件源。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document, DocumentVersion
from app.modules.files.extractors import extraction_config_hash
from app.modules.files.office_conversion import (
    DOCX_CONTENT_TYPE,
    XLSX_CONTENT_TYPE,
    DOC_TO_DOCX,
    XLS_TO_XLSX,
    LegacyOfficeConversionService,
    OfficeConversionSpec,
    OfficeConversionError,
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
    document_version_id: str | None = None
    artifact_id: str | None = None
    artifact_type: str | None = None
    converted: bool = False
    reused: bool = False
    converter_name: str | None = None
    converter_version: str | None = None
    converter_config_hash: str | None = None
    source_format: str | None = None
    parsed_format: str | None = None
    conversion_error: dict[str, Any] | None = None
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
        self._resolved_cache: dict[tuple[str, str, str], ReadableDocumentSource] = {}

    @property
    def conversion_service(self) -> LegacyOfficeConversionService:
        """仅在处理旧版 DOC/XLS 时初始化 LibreOffice 转换依赖。"""

        if self._conversion_service is None:
            self._conversion_service = LegacyOfficeConversionService(db=self.db)
        return self._conversion_service

    def expected_parser_config_hash(
        self,
        *,
        document: Document,
        document_version: DocumentVersion | None = None,
        purpose: str = "document",
    ) -> str | None:
        """在读取原件前计算可复用解析运行的预期指纹。"""

        spec = _legacy_conversion_spec(document.original_filename, document.content_type)
        if spec is None:
            return _parser_config_hash(filename=document.original_filename, purpose=purpose)
        settings = get_settings()
        if not settings.legacy_office_conversion_enabled:
            return extraction_config_hash(filename=document.original_filename)
        conversion_hash = self.conversion_service.reusable_converter_config_hash(
            document=document,
            document_version=document_version,
            spec=spec,
        )
        if conversion_hash is None:
            return extraction_config_hash(filename=document.original_filename)
        downstream_hash = _parser_config_hash(
            filename=f"{Path(document.original_filename).stem}{spec.target_suffix}",
            purpose=purpose,
        )
        identity = "|".join(
            [
                "legacy-office-readable-source-v2",
                f"version={document_version.id if document_version else 'legacy'}",
                f"conversion={conversion_hash}",
                f"parser={downstream_hash or 'native'}",
            ]
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def resolve(
        self,
        *,
        document: Document,
        document_version: DocumentVersion | None = None,
        original_path: Path,
        force_reconvert: bool = False,
        purpose: str = "document",
    ) -> ReadableDocumentSource:
        """返回实际解析源；DOC 可降级，XLS 失败则保留结构化错误而不伪造正文。"""

        original_path = original_path.resolve()
        cache_key = (document_version.id if document_version else document.id, purpose, str(original_path))
        if not force_reconvert and cache_key in self._resolved_cache:
            return self._resolved_cache[cache_key]
        spec = _legacy_conversion_spec(document.original_filename, document.content_type)
        if spec is None:
            result = _original_source(
                document=document,
                document_version=document_version,
                original_path=original_path,
                purpose=purpose,
            )
            self._resolved_cache[cache_key] = result
            return result
        try:
            if spec == DOC_TO_DOCX:
                artifact = self.conversion_service.get_or_create_docx(
                    document=document,
                    document_version=document_version,
                    source_path=original_path,
                    force_reconvert=force_reconvert,
                )
            else:
                if document_version is None:
                    raise OfficeConversionError(
                        "DOCUMENT_VERSION_REQUIRED",
                        "XLS 持久化转换缺少明确内容版本。",
                    )
                artifact = self.conversion_service.get_or_create_xlsx(
                    document=document,
                    document_version=document_version,
                    source_path=original_path,
                    force_reconvert=force_reconvert,
                )
        except OfficeConversionError as exc:
            source_format = spec.source_suffix.lstrip(".")
            parsed_format = spec.target_suffix.lstrip(".")
            is_doc = spec == DOC_TO_DOCX
            result = replace(
                _original_source(
                    document=document,
                    document_version=document_version,
                    original_path=original_path,
                    purpose=purpose,
                ),
                source_format=source_format,
                parsed_format=parsed_format,
                conversion_error={
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                },
                warnings=[
                    {
                        "code": exc.code,
                        "message": (
                            f"DOC 转 DOCX 失败，已回退旧版正文读取：{exc.message}"
                            if is_doc
                            else f"XLS 转 XLSX 失败，未生成可作为正文证据的表格内容：{exc.message}"
                        ),
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
            parse_filename=f"{Path(document.original_filename).stem}{spec.target_suffix}",
            original_content_type=document.content_type,
            parse_content_type=(DOCX_CONTENT_TYPE if spec == DOC_TO_DOCX else XLSX_CONTENT_TYPE),
            parser_config_hash=self.expected_parser_config_hash(
                document=document,
                document_version=document_version,
                purpose=purpose,
            ),
            document_version_id=document_version.id if document_version else None,
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            converted=True,
            reused=artifact.reused,
            converter_name=artifact.converter_name,
            converter_version=artifact.converter_version,
            converter_config_hash=artifact.converter_config_hash,
            source_format=artifact.source_format,
            parsed_format=artifact.parsed_format,
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
    result["conversion_artifact_type"] = source.artifact_type
    result["conversion_reused"] = source.reused if source.converted else None
    result["conversion_config_hash"] = source.converter_config_hash
    if not source.converted:
        if source.conversion_error and not result.get("ok"):
            # XLS 没有可靠原生回退，最终失败必须保留真正的 LibreOffice/持久化
            # 原因，不能被底层“需要派生件”占位错误覆盖。
            result["error"] = dict(source.conversion_error)
        return result
    result["conversion_source_format"] = source.source_format
    result["conversion_parsed_format"] = source.parsed_format
    result["conversion_converter"] = source.converter_name
    result["conversion_converter_version"] = source.converter_version
    source_metadata = {
        "source_format": source.source_format,
        "parsed_format": source.parsed_format,
        "conversion_artifact_id": source.artifact_id,
        "conversion_artifact_type": source.artifact_type,
        "document_version_id": source.document_version_id,
        "converter": source.converter_name,
        "converter_version": source.converter_version,
        "conversion_config_hash": source.converter_config_hash,
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


def _original_source(
    *,
    document: Document,
    document_version: DocumentVersion | None,
    original_path: Path,
    purpose: str,
) -> ReadableDocumentSource:
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
        document_version_id=document_version.id if document_version else None,
    )


def _is_legacy_doc(filename: str, content_type: str) -> bool:
    """根据扩展名和 MIME 判断旧版 Word 文件。"""

    return Path(filename).suffix.lower() == ".doc" or content_type == "application/msword"


def _legacy_conversion_spec(filename: str, content_type: str) -> OfficeConversionSpec | None:
    """根据受控扩展名和 MIME 选择旧版 Office 转换规格。"""

    if _is_legacy_doc(filename, content_type):
        return DOC_TO_DOCX
    if Path(filename).suffix.lower() == ".xls" or content_type == "application/vnd.ms-excel":
        return XLS_TO_XLSX
    return None


def _parser_config_hash(*, filename: str, purpose: str) -> str | None:
    """按普通读取或重命名模式生成解析指纹。"""

    if purpose != "rename":
        return extraction_config_hash(filename=filename)
    settings = get_settings()
    if settings.file_rename_parse_mode != "native":
        return extraction_config_hash(filename=filename)
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"txt", "md", "csv"}:
        identity = (
            "rename-native-v2|"
            f"format={suffix}|parser={extraction_config_hash(filename=filename)}"
        )
    else:
        identity = f"rename-native-v1|format={suffix}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
