"""为文件重命名收集 Docling 与原生解析候选。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.modules.files.docling_parser import try_parse_with_docling
from app.modules.files.extractors import (
    extract_document_text,
    extract_document_text_native,
    extraction_config_hash,
)


@dataclass(frozen=True)
class RenameParseCandidate:
    """一个解析器提供的正文页面和结构化元素。"""

    parser_name: str
    extractor: str
    pages: list[Any]
    elements: list[Any]
    warnings: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class RenameParsingResult:
    """重命名字段提取可使用的解析候选集合。"""

    mode: str
    candidates: list[RenameParseCandidate]
    warnings: list[dict[str, Any]] = field(default_factory=list)


class RenameParsingService:
    """按照受控模式收集解析结果，不在此处决定命名字段。"""

    def collect(
        self,
        *,
        file_path: Path | None,
        filename: str,
        content_type: str,
        primary_result: dict[str, Any] | None = None,
        primary_pages: list[Any] | None = None,
        primary_elements: list[Any] | None = None,
    ) -> RenameParsingResult:
        """复用主解析结果，并补充当前模式要求的解析候选。"""

        settings = get_settings()
        mode = settings.file_rename_parse_mode
        suffix = Path(filename).suffix.lower().lstrip(".")
        docling_available = settings.docling_enabled and suffix in set(settings.docling_formats)
        candidates: list[RenameParseCandidate] = []
        warnings: list[dict[str, Any]] = []

        primary_candidate = _primary_candidate(
            primary_result=primary_result,
            pages=primary_pages or [],
            elements=primary_elements or [],
        )

        if mode in {"hybrid", "docling"} and docling_available:
            docling_candidate = primary_candidate if primary_candidate and primary_candidate.parser_name == "docling" else None
            if docling_candidate is None and file_path is not None:
                docling_result = try_parse_with_docling(
                    file_path=file_path,
                    filename=filename,
                    content_type=content_type,
                    ocr_enabled=settings.docling_ocr_enabled,
                )
                if docling_result.get("ok"):
                    docling_candidate = _candidate_from_result(docling_result, parser_name="docling")
                else:
                    error = docling_result.get("error") or {}
                    warnings.append(
                        {
                            "code": str(error.get("code") or "DOCLING_RENAME_FALLBACK"),
                            "message": str(error.get("message") or "Docling 未生成可用重命名候选，已回退原生解析。"),
                        }
                    )
            if docling_candidate is not None:
                candidates.append(docling_candidate)

        needs_native = mode in {"hybrid", "native"} or not candidates
        if needs_native:
            native_candidate = primary_candidate if primary_candidate and primary_candidate.parser_name == "native" else None
            if native_candidate is None and file_path is not None:
                try:
                    native_result = extract_document_text_native(
                        file_path=file_path,
                        filename=filename,
                        content_type=content_type,
                    )
                except Exception as exc:
                    warnings.append(
                        {
                            "code": "NATIVE_RENAME_PARSE_EXCEPTION",
                            "message": f"原生解析器异常，已保留其他可用候选：{exc}",
                        }
                    )
                else:
                    if native_result.get("ok"):
                        native_candidate = _candidate_from_result(native_result, parser_name="native")
                    else:
                        error = native_result.get("error") or {}
                        warnings.append(
                            {
                                "code": str(error.get("code") or "NATIVE_RENAME_PARSE_FAILED"),
                                "message": str(error.get("message") or "原生解析器未生成可用重命名候选。"),
                            }
                        )
            if native_candidate is not None and not any(item.parser_name == "native" for item in candidates):
                candidates.append(native_candidate)

        if file_path is None and not candidates:
            warnings.append(
                {
                    "code": "RENAME_SOURCE_FILE_UNAVAILABLE",
                    "message": "无法打开原始文件，不能补充当前解析模式要求的候选。",
                }
            )

        return RenameParsingResult(mode=mode, candidates=candidates, warnings=warnings)


def rename_primary_config_hash(*, filename: str) -> str | None:
    """返回重命名主解析入口的配置指纹，隔离 native 与 Docling 快照。"""

    settings = get_settings()
    if settings.file_rename_parse_mode != "native":
        return extraction_config_hash(filename=filename)
    suffix = Path(filename).suffix.lower().lstrip(".")
    identity = f"rename-native-v1|format={suffix}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def extract_rename_primary(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    """按重命名模式执行主解析；native 模式不得初始化 Docling。"""

    settings = get_settings()
    if settings.file_rename_parse_mode != "native":
        return extract_document_text(
            file_path=file_path,
            filename=filename,
            content_type=content_type,
        )
    result = extract_document_text_native(
        file_path=file_path,
        filename=filename,
        content_type=content_type,
    )
    return {
        **result,
        "parser_name": "native",
        "parser_version": "",
        "parser_config_hash": rename_primary_config_hash(filename=filename) or "",
    }


def _primary_candidate(
    *,
    primary_result: dict[str, Any] | None,
    pages: list[Any],
    elements: list[Any],
) -> RenameParseCandidate | None:
    """把已持久化的主解析结果转换为候选，避免重复运行重量级解析器。"""

    if not primary_result or not primary_result.get("ok") or not pages:
        return None
    extractor = str(primary_result.get("extractor") or "")
    parser_name = str(primary_result.get("parser_name") or "").lower()
    if not parser_name:
        parser_name = "docling" if extractor.lower().startswith("docling") else "native"
    return RenameParseCandidate(
        parser_name=parser_name,
        extractor=extractor,
        pages=pages,
        elements=elements,
        warnings=list(primary_result.get("warnings") or []),
    )


def _candidate_from_result(result: dict[str, Any], *, parser_name: str) -> RenameParseCandidate:
    """把即时解析结果收敛为重命名候选。"""

    return RenameParseCandidate(
        parser_name=parser_name,
        extractor=str(result.get("extractor") or parser_name),
        pages=list(result.get("pages") or []),
        elements=list(result.get("elements") or []),
        warnings=list(result.get("warnings") or []),
    )
