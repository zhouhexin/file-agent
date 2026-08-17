"""受管原始文件的只读修订、分析与按需物化辅助服务。

本模块把受管目录的原始文件与可操作工作副本明确隔离：扫描只登记修订，
SOURCE_ANALYSIS 只通过受控相对路径读取原件并持久化可重建检索资料；它绝不
移动、重命名、删除或覆盖原始目录中的文件。
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentChunk,
    DocumentClassificationSummary,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentPage,
    DocumentSummary,
    DocumentVersion,
    ManagedFile,
    ManagedFileAnalysisRun,
    ManagedFileRevision,
    ManagedFileSearchProfile,
    ManagedFileTableStructure,
    ManagedFileTextChunk,
    ManagedRoot,
    User,
    WorkingCopy,
    utcnow,
)
from app.modules.chunks.service import DocumentIndexService, INDEX_VERSION
from app.modules.chunks.tokenizer import ChineseLexicalTokenizer, load_default_business_terms
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.extractors import extract_document_text
from app.modules.managed_files.path_policy import resolve_managed_relative_path
from app.modules.retrieval.search_profile import _normalize_text


class ManagedFileRevisionService:
    """维护原始文件修订，不在扫描阶段读取正文或计算完整哈希。"""

    def __init__(self, *, db: Session) -> None:
        """保存 worker 事务；调用方必须已经通过受管扫描获得稳定文件元数据。"""

        self.db = db

    def ensure_current_revision(self, *, managed_file: ManagedFile) -> ManagedFileRevision:
        """按快速 fingerprint 创建或复用当前原始文件修订。

        快速 fingerprint 只用于发现变化，不能证明内容相同；SOURCE_ANALYSIS 完成时
        才写入 SHA-256。因此新修订不会在扫描线程中触发慢速的完整文件读取。
        """

        current = (
            self.db.query(ManagedFileRevision)
            .filter(
                ManagedFileRevision.managed_file_id == managed_file.id,
                ManagedFileRevision.is_current.is_(True),
            )
            .order_by(ManagedFileRevision.revision_number.desc())
            .first()
        )
        if current is not None and current.quick_fingerprint == managed_file.fingerprint:
            return current

        next_number = int(
            self.db.query(func.coalesce(func.max(ManagedFileRevision.revision_number), 0))
            .filter(ManagedFileRevision.managed_file_id == managed_file.id)
            .scalar()
            or 0
        ) + 1
        if current is not None:
            current.is_current = False
            current.status = "STALE"
            current.updated_at = utcnow()
        revision = ManagedFileRevision(
            managed_file_id=managed_file.id,
            revision_number=next_number,
            size_bytes=int(managed_file.size_bytes or 0),
            modified_at=managed_file.modified_at,
            file_identity=managed_file.file_identity,
            quick_fingerprint=managed_file.fingerprint,
            content_sha256=None,
            status="ANALYSIS_PENDING",
            analysis_status="PENDING",
            is_current=True,
        )
        self.db.add(revision)
        # 内容变化后，旧工作副本仍可读取但不能与新原始修订静默合并。
        self.db.query(WorkingCopy).filter(
            WorkingCopy.managed_file_id == managed_file.id,
            WorkingCopy.status == "ACTIVE",
        ).update(
            {"sync_status": "ORIGINAL_CHANGED", "updated_at": utcnow()},
            synchronize_session=False,
        )
        self.db.flush()
        log_event(
            "managed_source.revision.discovered",
            status="PENDING",
            managed_file_id=managed_file.id,
            managed_file_revision_id=revision.id,
            message="发现受管原始文件新修订，等待只读分析",
        )
        return revision


class ManagedSourceAnalysisService:
    """执行一个原始文件修订的只读解析、摘要、Chunk 和检索投影生成。"""

    def __init__(self, *, db: Session, settings: Settings | None = None) -> None:
        """注入 worker 依赖；该服务只能从持久化任务调用，不能由聊天请求直接执行。"""

        self.db = db
        self.settings = settings or get_settings()
        self.tokenizer = ChineseLexicalTokenizer(load_default_business_terms())

    def analyze(self, *, revision_id: str, user_id: str | None = None) -> dict[str, Any]:
        """分析当前原始文件修订并原子发布检索资料。

        文件路径只由 ``ManagedRoot + ManagedFile.relative_path`` 在后端解析；任何
        传入的任务负载都不能提供绝对路径，从而防止 worker 成为任意文件读取入口。
        """

        revision = self.db.get(ManagedFileRevision, revision_id)
        if revision is None or not revision.is_current:
            return {"status": "STALE", "idempotent": True, "revision_id": revision_id}
        managed_file = self.db.get(ManagedFile, revision.managed_file_id)
        root = self.db.get(ManagedRoot, managed_file.root_id) if managed_file else None
        if managed_file is None or root is None or managed_file.status != "ACTIVE":
            raise RuntimeError("原始文件修订缺少有效受管文件或目录")
        path = resolve_managed_relative_path(
            root_path=Path(root.container_path), relative_path=managed_file.relative_path
        )
        before = path.stat()
        if not self._matches_revision(revision=revision, stat=before):
            revision.status = "STALE"
            revision.analysis_status = "STALE"
            self.db.flush()
            return {"status": "STALE", "revision_id": revision.id}

        if revision.status == "READY" and revision.analysis_document_version_id:
            return {"status": "READY", "idempotent": True, "revision_id": revision.id}

        owner_id = user_id or root.created_by or self._fallback_user_id()
        if not owner_id:
            raise RuntimeError("原始文件分析缺少可审计用户")
        revision.status = "ANALYZING"
        revision.analysis_status = "RUNNING"
        analysis = ManagedFileAnalysisRun(
            managed_file_revision_id=revision.id,
            status="RUNNING",
            summary_provider=self.settings.document_summary_provider,
            summary_version=self.settings.document_summary_schema_version,
            index_version=INDEX_VERSION,
        )
        self.db.add(analysis)
        self.db.flush()
        log_event(
            "managed_source.analysis.started",
            status="RUNNING",
            managed_file_id=managed_file.id,
            managed_file_revision_id=revision.id,
            message="原始文件只读分析开始",
        )
        try:
            sha256 = _sha256_file(path)
            document, version = self._ensure_analysis_document(
                revision=revision,
                managed_file=managed_file,
                owner_id=str(owner_id),
                sha256=sha256,
            )
            # 解析失败或 worker 重试时也要保留逻辑版本谱系；否则每次失败都会
            # 生成一对无法关联到修订的 Document/DocumentVersion。后续成功时复用
            # 同一逻辑版本并覆盖可重建派生资料，不会泄露或重复原始文件内容。
            revision.analysis_document_id = document.id
            revision.analysis_document_version_id = version.id
            version.source_analysis_run_id = analysis.id
            self.db.flush()
            extraction = extract_document_text(
                file_path=path,
                filename=managed_file.filename,
                content_type=_content_type(managed_file.extension),
            )
            repository = FileExtractionRepository(self.db, str(owner_id))
            run = repository.create_extraction_run(
                document_id=document.id,
                document_version_id=version.id,
                extractor=str(extraction.get("extractor") or "managed-source"),
                parser_name=str(extraction.get("parser_name") or ""),
                parser_version=str(extraction.get("parser_version") or ""),
                parser_config_hash=str(extraction.get("parser_config_hash") or ""),
            )
            if not extraction.get("ok"):
                error = dict(extraction.get("error") or {})
                repository.fail_extraction_run(
                    run=run,
                    error_message=str(error.get("message") or "原始文件解析失败"),
                )
                raise SourceAnalysisBusinessError(
                    str(error.get("code") or "SOURCE_EXTRACTION_FAILED"),
                    str(error.get("message") or "原始文件解析失败"),
                )
            repository.complete_extraction_run(
                run=run,
                pages=list(extraction.get("pages") or []),
                elements=list(extraction.get("elements") or []),
            )
            classification = DocumentClassificationService(db=self.db).classify(
                document_id=document.id,
                document_version_id=version.id,
                extraction_run_id=run.id,
                filename=managed_file.filename,
            )
            index_result = DocumentIndexService(db=self.db, settings=self.settings).build(
                document_id=document.id,
                document_version_id=version.id,
                extraction_run_id=run.id,
            )
            if not index_result.get("ok"):
                error = dict(index_result.get("error") or {})
                raise SourceAnalysisBusinessError(
                    str(error.get("code") or "SOURCE_INDEX_FAILED"),
                    str(error.get("message") or "原始文件索引失败"),
                )
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise SourceAnalysisBusinessError("SOURCE_CHANGED_DURING_ANALYSIS", "原始文件在只读分析期间发生变化")
            revision.content_sha256 = sha256
            managed_file.content_sha256 = sha256
            self._build_source_projections(
                revision=revision,
                analysis=analysis,
                document=document,
                version=version,
                extraction_run=run,
                index_run_id=str(index_result.get("index_run_id") or ""),
                filename=managed_file.filename,
            )
            # 逻辑源侧版本也必须指向本次分析运行，方便后续物化副本、审计和
            # 索引重建都精确追溯到同一份原始文件修订，不能只靠最新记录猜测。
            version.source_analysis_run_id = analysis.id
            revision.status = "READY"
            revision.analysis_status = "READY"
            revision.updated_at = utcnow()
            analysis.status = "COMPLETED"
            analysis.extraction_run_id = run.id
            analysis.index_run_id = str(index_result.get("index_run_id") or "") or None
            analysis.parser_name = run.parser_name
            analysis.parser_version = run.parser_version
            analysis.finished_at = utcnow()
            self._sync_working_copy_status(managed_file=managed_file, source_sha256=sha256)
            self.db.flush()
            log_event(
                "managed_source.analysis.completed",
                status="COMPLETED",
                document_id=document.id,
                document_version_id=version.id,
                managed_file_id=managed_file.id,
                managed_file_revision_id=revision.id,
                message="原始文件只读分析与检索投影完成",
            )
            return {
                "status": "READY",
                "revision_id": revision.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "analysis_run_id": analysis.id,
                "index_run_id": analysis.index_run_id,
                "classification": classification,
            }
        except Exception as exc:
            code = exc.code if isinstance(exc, SourceAnalysisBusinessError) else exc.__class__.__name__
            message = exc.message if isinstance(exc, SourceAnalysisBusinessError) else "原始文件只读分析失败"
            analysis.status = "FAILED"
            analysis.error_code = code
            analysis.error_message = message
            analysis.finished_at = utcnow()
            revision.status = "FAILED" if code != "SOURCE_CHANGED_DURING_ANALYSIS" else "STALE"
            revision.analysis_status = "FAILED" if code != "SOURCE_CHANGED_DURING_ANALYSIS" else "STALE"
            revision.updated_at = utcnow()
            self.db.flush()
            log_event(
                "managed_source.analysis.failed",
                level="ERROR",
                status="FAILED",
                error_code=code,
                managed_file_id=managed_file.id,
                managed_file_revision_id=revision.id,
                message="原始文件只读分析失败",
            )
            raise

    def _ensure_analysis_document(
        self, *, revision: ManagedFileRevision, managed_file: ManagedFile, owner_id: str, sha256: str
    ) -> tuple[Document, DocumentVersion]:
        """为源侧分析创建逻辑 DocumentVersion；它不对应工作副本物理文件。"""

        document = self.db.get(Document, revision.analysis_document_id) if revision.analysis_document_id else None
        version = self.db.get(DocumentVersion, revision.analysis_document_version_id) if revision.analysis_document_version_id else None
        if document is not None and version is not None:
            return document, version
        document = Document(
            user_id=owner_id,
            workspace_id=None,
            original_filename=managed_file.filename,
            content_type=_content_type(managed_file.extension),
            size_bytes=int(managed_file.size_bytes or 0),
            sha256=sha256,
            status="MANAGED_SOURCE_ANALYSIS",
            ingest_status="ANALYZING",
        )
        self.db.add(document)
        self.db.flush()
        version = DocumentVersion(
            document_id=document.id,
            version_number=1,
            storage_tier="MANAGED_SOURCE",
            # 不写真实路径，避免任何通用文件读取器误把原始目录暴露为 FileObject。
            storage_path=f"managed-source://{revision.id}",
            filename=managed_file.filename,
            content_type=document.content_type,
            size_bytes=int(managed_file.size_bytes or 0),
            sha256=sha256,
            source_type="MANAGED_SOURCE_ANALYSIS",
            source_managed_file_id=managed_file.id,
            source_managed_file_revision_id=revision.id,
            created_by=owner_id,
        )
        self.db.add(version)
        self.db.flush()
        return document, version

    def _build_source_projections(
        self,
        *,
        revision: ManagedFileRevision,
        analysis: ManagedFileAnalysisRun,
        document: Document,
        version: DocumentVersion,
        extraction_run: DocumentExtractionRun,
        index_run_id: str,
        filename: str,
    ) -> None:
        """从已验证的通用派生事实复制出源侧检索投影，避免第二次解析文件。"""

        summary = (
            self.db.query(DocumentSummary)
            .filter(
                DocumentSummary.document_id == document.id,
                DocumentSummary.document_version_id == version.id,
                DocumentSummary.status == "COMPLETED",
            )
            .order_by(DocumentSummary.updated_at.desc())
            .first()
        )
        topic = (
            self.db.query(DocumentClassificationSummary)
            .filter(
                DocumentClassificationSummary.document_id == document.id,
                DocumentClassificationSummary.document_version_id == version.id,
                DocumentClassificationSummary.status == "COMPLETED",
            )
            .order_by(DocumentClassificationSummary.updated_at.desc())
            .first()
        )
        summary_json = dict(summary.summary_json or {}) if summary is not None else {}
        topic_json = dict(topic.summary_json or {}) if topic is not None else {}
        # 普通摘要契约没有实体字段；分类主题摘要的 subjects / organizations / keywords
        # 才是本地摘要阶段已经验证的主题词。不能凭空从摘要 JSON 读取不存在的 entities。
        keywords = _string_list(topic_json.get("keywords"))
        entities = _string_list(topic_json.get("subjects")) + _string_list(topic_json.get("organizations"))
        entities = list(dict.fromkeys(entities))[:100]
        years = _years_from_values([filename, summary.summary_text if summary else "", *keywords, *entities])
        pages = (
            self.db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id == extraction_run.id)
            .order_by(DocumentPage.page_number.asc().nullslast(), DocumentPage.created_at.asc())
            .all()
        )
        sheet_names = list(dict.fromkeys(str(page.sheet_name) for page in pages if page.sheet_name))
        profile = (
            self.db.query(ManagedFileSearchProfile)
            .filter(ManagedFileSearchProfile.managed_file_revision_id == revision.id)
            .one_or_none()
        )
        combined_text = "\n".join(
            value for value in [filename, summary.summary_text if summary else "", " ".join(keywords), " ".join(entities)] if value
        )
        search_text = " ".join(self.tokenizer.tokenize(combined_text))
        if profile is None:
            profile = ManagedFileSearchProfile(
                managed_file_revision_id=revision.id,
                analysis_run_id=analysis.id,
            )
            self.db.add(profile)
        profile.analysis_run_id = analysis.id
        profile.normalized_filename = _normalize_text(filename)
        profile.title = filename
        profile.summary = str(summary.summary_text if summary else "")
        profile.topic_summary_json = topic_json
        profile.keywords_json = keywords
        profile.entities_json = entities
        profile.years_json = years
        profile.document_type = Path(filename).suffix.lower().lstrip(".")
        profile.sheet_names_json = sheet_names
        profile.search_text = search_text
        profile.status = "ACTIVE"
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            profile.search_vector = func.to_tsvector("simple", search_text)

        self.db.query(ManagedFileTextChunk).filter(
            ManagedFileTextChunk.managed_file_revision_id == revision.id
        ).delete(synchronize_session=False)
        chunks = (
            self.db.query(DocumentChunk)
            .filter(DocumentChunk.index_run_id == index_run_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )
        for chunk in chunks:
            source_chunk = ManagedFileTextChunk(
                managed_file_revision_id=revision.id,
                document_chunk_id=chunk.id,
                chunk_index=chunk.chunk_index,
                page_number=chunk.page_start,
                sheet_name=chunk.sheet_name,
                cell_range=chunk.cell_range,
                section_title=str((chunk.metadata_json or {}).get("section_title") or "") or None,
                text_content=chunk.text_content,
                search_text=chunk.search_text,
                token_count=chunk.token_count,
            )
            if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
                source_chunk.search_vector = func.to_tsvector("simple", chunk.search_text)
            self.db.add(source_chunk)
        self._replace_table_structures(revision=revision, pages=pages)
        document.ingest_status = "INDEXED"

    def _replace_table_structures(self, *, revision: ManagedFileRevision, pages: list[DocumentPage]) -> None:
        """从确定性 Excel 页元数据提取 Sheet 行列范围，不由 LLM 推断数字。"""

        self.db.query(ManagedFileTableStructure).filter(
            ManagedFileTableStructure.managed_file_revision_id == revision.id
        ).delete(synchronize_session=False)
        for page in pages:
            if not page.sheet_name:
                continue
            ranges = list((page.metadata_json or {}).get("line_cell_ranges") or [])
            row_count, column_count, headers = _table_shape(page.text_content, ranges)
            self.db.add(
                ManagedFileTableStructure(
                    managed_file_revision_id=revision.id,
                    sheet_name=page.sheet_name,
                    row_count=row_count,
                    column_count=column_count,
                    headers_json=headers,
                    column_types_json={},
                    date_ranges_json={},
                    numeric_statistics_json={},
                    sample_values_json=[line[:160] for line in page.text_content.splitlines()[:5] if line][:5],
                )
            )

    @staticmethod
    def _matches_revision(*, revision: ManagedFileRevision, stat: Any) -> bool:
        """用扫描时已固化的快速元数据阻止对已变化原始文件发布旧索引。"""

        expected_mtime = revision.modified_at
        actual_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if int(revision.size_bytes or 0) != int(stat.st_size):
            return False
        if expected_mtime is None:
            return True
        # 秒级比较会漏掉同一秒内的覆盖写入，进而错误发布旧正文索引。扫描与
        # 分析两侧都保存带时区 datetime，使用毫秒级时间戳兼容 SQLite 的精度。
        return int(expected_mtime.timestamp() * 1000) == int(actual_mtime.timestamp() * 1000)

    def _fallback_user_id(self) -> str | None:
        """仅为共享源侧分析选择最早用户作为审计主体，不能伪造新用户。"""

        value = self.db.query(User.id).order_by(User.created_at.asc()).scalar()
        return str(value) if value else None

    def _sync_working_copy_status(self, *, managed_file: ManagedFile, source_sha256: str) -> None:
        """源分析确认 SHA 后更新工作副本同步标记，不复制或覆盖任何工作副本。"""

        for copy in self.db.query(WorkingCopy).filter(WorkingCopy.managed_file_id == managed_file.id).all():
            copy.sync_status = "SYNCED" if copy.imported_source_sha256 == source_sha256 else "ORIGINAL_CHANGED"
            copy.updated_at = utcnow()


class SourceAnalysisBusinessError(RuntimeError):
    """可安全写入任务审计的源侧分析业务失败。"""

    def __init__(self, code: str, message: str) -> None:
        """保存脱敏错误码和面向运维的简短说明。"""

        super().__init__(message)
        self.code = code
        self.message = message


def _sha256_file(path: Path) -> str:
    """流式计算原始文件哈希，避免把大文件一次性载入内存。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_type(extension: str) -> str:
    """以标准库推断解析器输入类型，未知类型安全回退二进制。"""

    return mimetypes.types_map.get(str(extension or "").lower(), "application/octet-stream")


def _string_list(value: Any) -> list[str]:
    """把摘要 JSON 中的字符串或对象列表规范成有限检索词集合。"""

    items: list[str] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            item = item.get("name") or item.get("text") or ""
        text = str(item or "").strip()
        if text:
            items.append(text)
    return list(dict.fromkeys(items))[:100]


def _years_from_values(values: list[str]) -> list[str]:
    """从已验证文件名与摘要文本提取年份；它只是检索字段而非事实结论。"""

    return list(dict.fromkeys(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", "\n".join(values))))[:20]


def _table_shape(text: str, ranges: list[Any]) -> tuple[int, int, list[str]]:
    """基于解析器真实行与单元格范围计算 Sheet 基础结构。"""

    lines = [line for line in text.splitlines() if line]
    headers = lines[0].split("\t")[:100] if lines else []
    max_column = len(headers)
    cell_pattern = re.compile(r"[A-Z]+(\d+):([A-Z]+)(\d+)")
    max_row = len(lines)
    for item in ranges:
        match = cell_pattern.fullmatch(str((item or {}).get("cell_range") or "")) if isinstance(item, dict) else None
        if match:
            max_row = max(max_row, int(match.group(2)))
            max_column = max(max_column, _column_number(match.group(1)))
    return max_row, max_column, headers


def _column_number(value: str) -> int:
    """把 Excel 列字母转为确定性列号。"""

    result = 0
    for char in value:
        result = result * 26 + ord(char) - ord("A") + 1
    return result
