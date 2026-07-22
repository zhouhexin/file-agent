"""DocumentVersion 原文 Chunk、Evidence 与 CPU 词法索引服务。

该服务只读取持久化解析事实并写入派生索引；正文、分词词项和向量不会进入 AgentGraphState 或日志。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentElement,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentPage,
    DocumentVersion,
    EvidenceSpan,
    utcnow,
)
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer, load_default_business_terms


INDEX_VERSION = "document-chunk-index-v1"
EVIDENCE_QUOTE_MAX_CHARS = 500
EMPTY_EXTRACTION_ERROR = "解析结果没有可建立索引的正文"
INDEX_SOURCE_TOO_LARGE_ERROR = "解析正文超过当前索引字符预算"
INDEX_CHUNK_LIMIT_ERROR = "解析正文超过当前索引 Chunk 数量预算"


@dataclass(slots=True)
class ChunkDraft:
    """尚未落库的轻量切分结果，只在当前索引调用内存在。"""

    text: str
    chunk_type: str = "text"
    page_start: int | None = None
    page_end: int | None = None
    sheet_name: str | None = None
    cell_range: str | None = None
    element_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentIndexService:
    """按不可变内容版本幂等建立结构化 Chunk 和可定位 Evidence。"""

    def __init__(
        self,
        *,
        db: Session,
        settings: Settings | None = None,
        tokenizer: ChineseLexicalTokenizer | None = None,
    ) -> None:
        """注入数据库、配置和可替换分词器，测试不得调用外部模型。"""

        self.db = db
        self.settings = settings or get_settings()
        self.tokenizer = tokenizer or ChineseLexicalTokenizer(load_default_business_terms())

    def build(
        self,
        *,
        document_id: str,
        document_version_id: str,
        extraction_run_id: str,
        force_reprocess: bool = False,
    ) -> dict[str, Any]:
        """建立或复用原文索引，返回不含正文和词项的结构化结果。"""

        started_at = time.perf_counter()
        document = self.db.get(Document, document_id)
        version = self.db.get(DocumentVersion, document_version_id)
        extraction = self.db.get(DocumentExtractionRun, extraction_run_id)
        if document is None or version is None or version.document_id != document_id:
            return _failed("DOCUMENT_VERSION_NOT_FOUND", "文档内容版本不存在。")
        if extraction is None or extraction.document_id != document_id or extraction.status != "COMPLETED":
            return _failed("EXTRACTION_NOT_READY", "成功解析结果不存在，不能建立原文索引。")
        if extraction.document_version_id is not None and extraction.document_version_id != document_version_id:
            return _failed("EXTRACTION_VERSION_MISMATCH", "解析结果不属于当前文档内容版本。")
        if extraction.document_version_id is None:
            version_count = (
                self.db.query(DocumentVersion)
                .filter(DocumentVersion.document_id == document_id)
                .count()
            )
            if version_count > 1:
                return _failed("EXTRACTION_VERSION_AMBIGUOUS", "历史解析结果未绑定内容版本，不能用于多版本索引。")

        config_hash = self._config_hash(extraction=extraction)
        run = (
            self.db.query(DocumentIndexRun)
            .filter(
                DocumentIndexRun.document_version_id == document_version_id,
                DocumentIndexRun.extraction_run_id == extraction_run_id,
                DocumentIndexRun.config_hash == config_hash,
            )
            .with_for_update()
            .one_or_none()
        )
        if run is not None and run.status == "COMPLETED":
            # 已完成索引中的 Chunk/Evidence ID 是历史引用基础。即使上游要求重新解析，
            # 同一 extraction_run + config 也必须复用；新的解析运行自然会生成新的索引运行。
            result = self._result(run=run, reused=True)
            self._log_result(result=result, duration_ms=int((time.perf_counter() - started_at) * 1000))
            return result
        if run is None:
            try:
                # SAVEPOINT 只吸收幂等键竞争，不允许为了并发冲突回滚外层文件生命周期事务。
                with self.db.begin_nested():
                    run = DocumentIndexRun(
                        document_id=document_id,
                        document_version_id=document_version_id,
                        extraction_run_id=extraction_run_id,
                        index_version=INDEX_VERSION,
                        tokenizer=self.tokenizer.name,
                        tokenizer_version=self.tokenizer.version,
                        config_hash=config_hash,
                        status="RUNNING",
                        embedding_status=self._embedding_status(),
                    )
                    self.db.add(run)
                    self.db.flush()
            except IntegrityError:
                run = (
                    self.db.query(DocumentIndexRun)
                    .filter(
                        DocumentIndexRun.document_version_id == document_version_id,
                        DocumentIndexRun.extraction_run_id == extraction_run_id,
                        DocumentIndexRun.config_hash == config_hash,
                    )
                    .with_for_update()
                    .one()
                )
                if run.status in {"RUNNING", "COMPLETED"}:
                    result = self._result(run=run, reused=True)
                    if run.status == "RUNNING":
                        result["status"] = "PENDING"
                    self._log_result(
                        result=result,
                        duration_ms=int((time.perf_counter() - started_at) * 1000),
                    )
                    return result
        try:
            # 派生数据写入使用 SAVEPOINT；数据库异常只回滚本次索引，不污染外层文件生命周期事务。
            with self.db.begin_nested():
                if run.status != "RUNNING":
                    self._clear_run_derivatives(run=run)
                run.status = "RUNNING"
                run.error_code = None
                run.error_message = None
                run.chunk_count = 0
                run.evidence_count = 0
                run.embedding_status = self._embedding_status()
                drafts = self._build_drafts(
                    extraction_run_id=extraction_run_id,
                    document_id=document_id,
                )
                if not drafts:
                    raise ValueError(EMPTY_EXTRACTION_ERROR)
                evidence_count = 0
                for index, draft in enumerate(drafts):
                    tokens = self.tokenizer.tokenize(draft.text)
                    location_payload = {
                        "page_start": draft.page_start,
                        "page_end": draft.page_end,
                        "sheet_name": draft.sheet_name,
                        "cell_range": draft.cell_range,
                        "element_ids": draft.element_ids,
                    }
                    chunk = DocumentChunk(
                        index_run_id=run.id,
                        document_id=document_id,
                        document_version_id=document_version_id,
                        extraction_run_id=extraction_run_id,
                        chunk_index=index,
                        chunk_type=draft.chunk_type,
                        text_content=draft.text,
                        search_text=" ".join(tokens),
                        search_vector=None,
                        content_hash=_sha256(draft.text),
                        location_hash=_sha256(json.dumps(location_payload, ensure_ascii=False, sort_keys=True)),
                        char_count=len(draft.text),
                        token_count=len(tokens),
                        page_start=draft.page_start,
                        page_end=draft.page_end,
                        sheet_name=draft.sheet_name,
                        cell_range=draft.cell_range,
                        element_ids_json=draft.element_ids,
                        metadata_json=draft.metadata,
                        embedding=None,
                        embedding_status=self._embedding_status(),
                        embedding_provider=self.settings.embedding_provider,
                        embedding_model="",
                    )
                    self.db.add(chunk)
                    self.db.flush()
                    quote = _evidence_quote(draft.text)
                    if quote:
                        start_offset = draft.text.find(quote)
                        self.db.add(
                            EvidenceSpan(
                                chunk_id=chunk.id,
                                document_id=document_id,
                                document_version_id=document_version_id,
                                extraction_run_id=extraction_run_id,
                                span_index=0,
                                evidence_type="table_cell_range" if draft.sheet_name else "text_quote",
                                quote=quote,
                                start_offset=max(0, start_offset),
                                end_offset=max(0, start_offset) + len(quote),
                                page_number=draft.page_start,
                                sheet_name=draft.sheet_name,
                                cell_range=draft.cell_range,
                                source="document_chunk",
                                metadata_json={"index_version": INDEX_VERSION},
                            )
                        )
                        evidence_count += 1

                self.db.flush()
                if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
                    # 中文已经由应用层分词；数据库只负责 simple 词项索引，不依赖服务器中文分词插件。
                    self.db.query(DocumentChunk).filter(DocumentChunk.index_run_id == run.id).update(
                        {DocumentChunk.search_vector: func.to_tsvector("simple", DocumentChunk.search_text)},
                        synchronize_session=False,
                    )
                run.status = "COMPLETED"
                run.chunk_count = len(drafts)
                run.evidence_count = evidence_count
                run.updated_at = utcnow()
                self.db.flush()
            result = self._result(run=run, reused=False)
            self._log_result(result=result, duration_ms=int((time.perf_counter() - started_at) * 1000))
            return result
        except Exception as exc:
            error_code, error_message = _safe_index_error(exc)
            with self.db.begin_nested():
                self._clear_run_derivatives(run=run)
                run.status = "FAILED"
                run.error_code = error_code
                run.error_message = error_message
                run.chunk_count = 0
                run.evidence_count = 0
                run.updated_at = utcnow()
                self.db.flush()
            result = self._result(run=run, reused=False)
            self._log_result(result=result, duration_ms=int((time.perf_counter() - started_at) * 1000))
            return result

    def build_latest_for_user(self, *, document_id: str, user_id: str) -> dict[str, Any]:
        """在所有权边界内选择最新内容版本和成功解析运行，供白名单 Tool 调用。"""

        document = (
            self.db.query(Document)
            .filter(Document.id == document_id, Document.user_id == user_id)
            .one_or_none()
        )
        if document is None:
            return _failed("DOCUMENT_NOT_FOUND", "文件不存在或不属于当前用户。")
        version = (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == document_id)
            .order_by(DocumentVersion.version_number.desc(), DocumentVersion.created_at.desc())
            .first()
        )
        if version is None:
            return _failed("INDEX_SOURCE_NOT_READY", "文件内容版本尚未准备好。")
        extraction = (
            self.db.query(DocumentExtractionRun)
            .filter(
                DocumentExtractionRun.document_id == document_id,
                DocumentExtractionRun.document_version_id == version.id,
                DocumentExtractionRun.status == "COMPLETED",
            )
            .order_by(DocumentExtractionRun.updated_at.desc())
            .first()
        )
        if extraction is None:
            # 迁移前的 NULL 版本解析事实只允许在单版本文档中兼容，避免旧正文绑定到新版本。
            version_count = self.db.query(DocumentVersion).filter(DocumentVersion.document_id == document_id).count()
            if version_count == 1:
                extraction = (
                    self.db.query(DocumentExtractionRun)
                    .filter(
                        DocumentExtractionRun.document_id == document_id,
                        DocumentExtractionRun.document_version_id.is_(None),
                        DocumentExtractionRun.status == "COMPLETED",
                    )
                    .order_by(DocumentExtractionRun.updated_at.desc())
                    .first()
                )
        if extraction is None:
            return _failed("INDEX_SOURCE_NOT_READY", "当前文件版本的成功解析结果尚未准备好。")
        return self.build(
            document_id=document_id,
            document_version_id=version.id,
            extraction_run_id=extraction.id,
        )

    def _clear_run_derivatives(self, *, run: DocumentIndexRun) -> None:
        """只清理失败运行的派生 Chunk/Evidence，已完成运行在入口处强制复用。"""

        chunk_ids = self.db.query(DocumentChunk.id).filter(DocumentChunk.index_run_id == run.id)
        self.db.query(EvidenceSpan).filter(EvidenceSpan.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
        self.db.query(DocumentChunk).filter(DocumentChunk.index_run_id == run.id).delete(
            synchronize_session=False
        )

    def _build_drafts(self, *, extraction_run_id: str, document_id: str) -> list[ChunkDraft]:
        """优先使用覆盖完整正文的结构化元素，并同时校验解析运行与文档归属。"""

        page_char_count = int(
            self.db.query(func.coalesce(func.sum(func.length(DocumentPage.text_content)), 0))
            .filter(
                DocumentPage.extraction_run_id == extraction_run_id,
                DocumentPage.document_id == document_id,
            )
            .scalar()
            or 0
        )
        element_char_count = int(
            self.db.query(func.coalesce(func.sum(func.length(DocumentElement.text_content)), 0))
            .filter(
                DocumentElement.extraction_run_id == extraction_run_id,
                DocumentElement.document_id == document_id,
            )
            .scalar()
            or 0
        )
        # pages 与 elements 可能是同一正文的两种表示，取较大值而不是相加，避免重复计算。
        if max(page_char_count, element_char_count) > self.settings.document_index_max_chars:
            raise ValueError(INDEX_SOURCE_TOO_LARGE_ERROR)
        pages = (
            self.db.query(DocumentPage)
            .filter(
                DocumentPage.extraction_run_id == extraction_run_id,
                DocumentPage.document_id == document_id,
            )
            .order_by(DocumentPage.page_number.asc().nullslast(), DocumentPage.created_at.asc())
            .all()
        )
        elements = (
            self.db.query(DocumentElement)
            .filter(
                DocumentElement.extraction_run_id == extraction_run_id,
                DocumentElement.document_id == document_id,
            )
            .order_by(DocumentElement.element_index.asc())
            .all()
        )
        page_chars = sum(len(page.text_content.strip()) for page in pages)
        element_chars = sum(len(element.text_content.strip()) for element in elements)
        has_spreadsheet = any(page.sheet_name for page in pages)
        drafts = (
            self._drafts_from_elements(elements)
            if elements and not has_spreadsheet and (not page_chars or element_chars >= page_chars * 0.85)
            else self._drafts_from_pages(pages)
        )
        if len(drafts) > self.settings.document_index_max_chunks:
            raise ValueError(INDEX_CHUNK_LIMIT_ERROR)
        return drafts

    def _drafts_from_elements(self, elements: list[DocumentElement]) -> list[ChunkDraft]:
        """沿标题、段落和表格元素边界切分，并保留元素稳定 ID。"""

        drafts: list[ChunkDraft] = []
        for element in elements:
            text = element.text_content.strip()
            if not text:
                continue
            for part in _split_text(
                text,
                max_chars=self.settings.document_chunk_max_chars,
                overlap_chars=self.settings.document_chunk_overlap_chars,
            ):
                drafts.append(
                    ChunkDraft(
                        text=part,
                        chunk_type=str(element.label or "text"),
                        page_start=element.page_number,
                        page_end=element.page_number,
                        element_ids=[element.id],
                        metadata={"content_layer": element.content_layer},
                    )
                )
        return drafts

    def _drafts_from_pages(self, pages: list[DocumentPage]) -> list[ChunkDraft]:
        """按页或 Sheet 行边界切分，Excel 使用解析器保存的真实行单元格范围。"""

        drafts: list[ChunkDraft] = []
        for page in pages:
            if page.sheet_name:
                drafts.extend(self._spreadsheet_drafts(page))
                continue
            for part in _split_text(
                page.text_content,
                max_chars=self.settings.document_chunk_max_chars,
                overlap_chars=self.settings.document_chunk_overlap_chars,
            ):
                drafts.append(
                    ChunkDraft(
                        text=part,
                        chunk_type="page",
                        page_start=page.page_number,
                        page_end=page.page_number,
                        metadata={"document_page_id": page.id},
                    )
                )
        return drafts

    def _spreadsheet_drafts(self, page: DocumentPage) -> list[ChunkDraft]:
        """按工作表非空行组合 Chunk，并合并对应的真实单元格范围。"""

        lines = page.text_content.splitlines()
        line_ranges = list((page.metadata_json or {}).get("line_cell_ranges") or [])
        if not lines:
            return []
        drafts: list[ChunkDraft] = []
        current_lines: list[str] = []
        current_ranges: list[str] = []
        for index, line in enumerate(lines):
            line_range = ""
            if index < len(line_ranges) and isinstance(line_ranges[index], dict):
                line_range = str(line_ranges[index].get("cell_range") or "")
            if len(line) > self.settings.document_chunk_max_chars:
                if current_lines:
                    drafts.append(self._spreadsheet_draft(page, current_lines, current_ranges))
                    current_lines = []
                    current_ranges = []
                for part in _window_text(
                    line,
                    max_chars=self.settings.document_chunk_max_chars,
                    overlap_chars=self.settings.document_chunk_overlap_chars,
                ):
                    drafts.append(self._spreadsheet_draft(page, [part], [line_range] if line_range else []))
                continue
            candidate = "\n".join([*current_lines, line]).strip()
            if current_lines and len(candidate) > self.settings.document_chunk_max_chars:
                drafts.append(self._spreadsheet_draft(page, current_lines, current_ranges))
                current_lines = []
                current_ranges = []
            current_lines.append(line)
            if line_range:
                current_ranges.append(line_range)
        if current_lines:
            drafts.append(self._spreadsheet_draft(page, current_lines, current_ranges))
        return drafts

    @staticmethod
    def _spreadsheet_draft(page: DocumentPage, lines: list[str], ranges: list[str]) -> ChunkDraft:
        """构造单个 Sheet Chunk；无法定位时保留空范围，绝不伪造坐标。"""

        return ChunkDraft(
            text="\n".join(lines).strip(),
            chunk_type="table",
            page_start=page.page_number,
            page_end=page.page_number,
            sheet_name=page.sheet_name,
            cell_range=_merge_cell_ranges(ranges),
            metadata={"document_page_id": page.id},
        )

    def _config_hash(self, *, extraction: DocumentExtractionRun) -> str:
        """计算索引幂等指纹；模型开关不影响 CPU Chunk 的稳定 ID。"""

        payload = {
            "index_version": INDEX_VERSION,
            "parser_config_hash": extraction.parser_config_hash,
            "tokenizer": self.tokenizer.name,
            "tokenizer_version": self.tokenizer.version,
            "max_chars": self.settings.document_chunk_max_chars,
            "overlap_chars": self.settings.document_chunk_overlap_chars,
        }
        return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    def _embedding_status(self) -> str:
        """本阶段不调用 embedding；仅显式标记配置状态供后续回填任务读取。"""

        if not self.settings.embedding_enabled or self.settings.embedding_provider == "disabled":
            return "DISABLED"
        return "PENDING"

    @staticmethod
    def _log_result(*, result: dict[str, Any], duration_ms: int) -> None:
        """记录不含正文、分词文本和向量的索引诊断事件。"""

        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        pending = result.get("status") == "PENDING"
        event_name = (
            "document.index.pending"
            if pending
            else "document.index.completed"
            if result.get("ok")
            else "document.index.failed"
        )
        log_event(
            event_name,
            level="INFO" if pending or result.get("ok") else "ERROR",
            document_id=result.get("document_id"),
            status=result.get("status"),
            duration_ms=duration_ms,
            error_code=error.get("code"),
            index_run_id=result.get("index_run_id"),
            document_version_id=result.get("document_version_id"),
            extraction_run_id=result.get("extraction_run_id"),
            chunk_count=result.get("chunk_count"),
            evidence_count=result.get("evidence_count"),
            embedding_status=result.get("embedding_status"),
            reused=result.get("reused"),
            message="原文索引处理完成",
        )

    @staticmethod
    def _result(*, run: DocumentIndexRun, reused: bool) -> dict[str, Any]:
        """返回不包含正文、分词、绝对路径和 embedding 的安全结果。"""

        result: dict[str, Any] = {
            "ok": run.status == "COMPLETED",
            "status": run.status,
            "index_run_id": run.id,
            "document_id": run.document_id,
            "document_version_id": run.document_version_id,
            "extraction_run_id": run.extraction_run_id,
            "chunk_count": run.chunk_count,
            "evidence_count": run.evidence_count,
            "embedding_status": run.embedding_status,
            "reused": reused,
        }
        if run.status == "FAILED":
            result["error"] = {
                "code": run.error_code or "INDEX_BUILD_FAILED",
                "message": run.error_message or "原文索引建立失败。",
            }
        return result


def _split_text(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """优先沿段落和句子边界切分，超长段落再使用带重叠的确定性窗口。"""

    normalized = str(text or "").strip()
    if not normalized:
        return []
    blocks = [item.strip() for item in re.split(r"\n\s*\n|(?<=[。！？；])\s*", normalized) if item.strip()]
    results: list[str] = []
    current = ""
    for block in blocks:
        if len(block) > max_chars:
            if current:
                results.append(current)
                current = ""
            results.extend(_window_text(block, max_chars=max_chars, overlap_chars=overlap_chars))
            continue
        candidate = f"{current}\n{block}".strip() if current else block
        if current and len(candidate) > max_chars:
            results.append(current)
            current = block
        else:
            current = candidate
    if current:
        results.append(current)
    return results


def _window_text(text: str, *, max_chars: int, overlap_chars: int) -> list[str]:
    """切分无法按结构缩小的长段落，重叠不得等于或超过窗口。"""

    step = max(1, max_chars - min(overlap_chars, max_chars - 1))
    return [text[start : start + max_chars].strip() for start in range(0, len(text), step) if text[start : start + max_chars].strip()]


def _evidence_quote(text: str) -> str:
    """从 Chunk 中截取可验证 quote；返回值必须是原文的连续子串。"""

    stripped = text.strip()
    return stripped[:EVIDENCE_QUOTE_MAX_CHARS]


def _merge_cell_ranges(ranges: list[str]) -> str | None:
    """把同一 Sheet 的行范围合并为首尾坐标；非法输入不会生成猜测坐标。"""

    parsed = [match for item in ranges if (match := re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", item))]
    if not parsed:
        return None
    first = parsed[0]
    last = parsed[-1]
    return f"{first.group(1)}{first.group(2)}:{last.group(3)}{last.group(4)}"


def _sha256(value: str) -> str:
    """生成派生内容指纹，不记录正文。"""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _failed(code: str, message: str) -> dict[str, Any]:
    """构造不包含内部异常和正文的索引失败结果。"""

    return {"ok": False, "status": "FAILED", "error": {"code": code, "message": message}}


def _safe_index_error(exc: Exception) -> tuple[str, str]:
    """把内部异常压缩为安全错误，禁止正文、SQL 参数或绝对路径进入 Tool 输出和审计表。"""

    if isinstance(exc, ValueError) and str(exc) == EMPTY_EXTRACTION_ERROR:
        return "EMPTY_EXTRACTION", "解析结果没有可建立索引的正文。"
    if isinstance(exc, ValueError) and str(exc) == INDEX_SOURCE_TOO_LARGE_ERROR:
        return "INDEX_SOURCE_TOO_LARGE", "正文超过当前索引资源预算，文件已保留并等待分批处理。"
    if isinstance(exc, ValueError) and str(exc) == INDEX_CHUNK_LIMIT_ERROR:
        return "INDEX_CHUNK_LIMIT_EXCEEDED", "Chunk 数量超过当前索引资源预算，文件已保留并等待分批处理。"
    return exc.__class__.__name__[:100], "原文索引建立失败，内部异常详情已隐藏。"
