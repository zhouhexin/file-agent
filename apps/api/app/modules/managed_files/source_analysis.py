"""受管原始文件的只读修订、分析与后续物化辅助服务。

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
from app.modules.classification.runtime_factory import ClassificationRuntimeFactory
from app.modules.classification.freshness import (
    ClassificationFreshness,
    current_classification_identity,
    inspect_managed_source_classification,
)
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.extractors import extract_document_text
from app.modules.files.readable_source import (
    ReadableDocumentSourceResolver,
    apply_readable_source_metadata,
)
from app.modules.managed_files.path_policy import resolve_managed_relative_path
from app.modules.retrieval.search_profile import _normalize_text


def managed_source_extraction_is_current(
    *,
    db: Session,
    revision: ManagedFileRevision,
    owner_id: str,
) -> bool:
    """判断当前源修订是否已有与现行 OCR/解析配置完全兼容的成功运行。"""

    document = db.get(Document, revision.analysis_document_id)
    version = db.get(DocumentVersion, revision.analysis_document_version_id)
    if document is None or version is None:
        return False
    expected_parser_hash = ReadableDocumentSourceResolver(db=db).expected_parser_config_hash(
        document=document,
        document_version=version,
    )
    if expected_parser_hash is None:
        return True
    reusable = FileExtractionRepository(db, str(owner_id)).get_latest_successful_extraction(
        document_id=document.id,
        document_version_id=version.id,
        parser_config_hash=expected_parser_hash,
    )
    return reusable is not None


def managed_source_extraction_fingerprint(
    *,
    db: Session,
    revision: ManagedFileRevision,
) -> str:
    """返回源侧分析任务去重使用的完整解析指纹。"""

    document = db.get(Document, revision.analysis_document_id)
    version = db.get(DocumentVersion, revision.analysis_document_version_id)
    if document is None or version is None:
        return "missing-analysis-record"
    expected_parser_hash = ReadableDocumentSourceResolver(db=db).expected_parser_config_hash(
        document=document,
        document_version=version,
    )
    return expected_parser_hash or "parser-config-not-applicable"


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

        owner_id = user_id or root.created_by or self._fallback_user_id()
        if not owner_id:
            raise RuntimeError("原始文件分析缺少可审计用户")
        if revision.status == "READY" and revision.analysis_document_version_id:
            extraction_current = managed_source_extraction_is_current(
                db=self.db,
                revision=revision,
                owner_id=str(owner_id),
            )
            if not extraction_current:
                # OCR Provider、外发授权或解析器配置改变后，同一原始修订也必须重建
                # 正文与索引；不能只刷新分类后继续复用旧 Paddle/空白页面。
                log_event(
                    "managed_source.analysis.parser_stale",
                    status="STALE",
                    document_id=revision.analysis_document_id,
                    document_version_id=revision.analysis_document_version_id,
                    managed_file_id=managed_file.id,
                    managed_file_revision_id=revision.id,
                    message="原始文件解析配置已变化，将重新生成正文和检索资料",
                )
            else:
                identity = current_classification_identity(
                    db=self.db,
                    settings=self.settings,
                    user_id=str(owner_id),
                )
                freshness = inspect_managed_source_classification(
                    db=self.db,
                    revision=revision,
                    identity=identity,
                )
                if freshness is ClassificationFreshness.CURRENT:
                    return {
                        "status": "READY",
                        "idempotent": True,
                        "revision_id": revision.id,
                    }
                return self.refresh_classification(
                    revision_id=revision.id,
                    user_id=str(owner_id),
                )

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
            extraction = _extract_managed_source_document(
                db=self.db,
                document=document,
                document_version=version,
                source_path=path,
            )
            extraction = _prepare_image_metadata_extraction(
                extraction=extraction,
                filename=managed_file.filename,
                content_type=document.content_type,
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
            metadata_only = bool(extraction.get("metadata_only"))
            if metadata_only:
                identity = current_classification_identity(
                    db=self.db,
                    settings=self.settings,
                    user_id=str(owner_id),
                )
                classification = {
                    "status": "SKIPPED_METADATA_ONLY",
                    "categories": [],
                    "warnings": list(extraction.get("warnings") or []),
                    "taxonomy_key": identity.taxonomy_key,
                    "taxonomy_version": identity.taxonomy_version,
                    "classifier_version": identity.classifier_version,
                    "source": "managed_source_metadata_only",
                }
                index_result = {
                    "ok": True,
                    "status": "SKIPPED_METADATA_ONLY",
                    "index_run_id": None,
                }
            else:
                classification = ClassificationRuntimeFactory(self.settings).create(
                    db=self.db,
                    user_id=str(owner_id),
                ).classify(
                    document_id=document.id,
                    document_version_id=version.id,
                    extraction_run_id=run.id,
                    filename=managed_file.filename,
                    default_organization_root="学院",
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
                index_run_id=str(index_result.get("index_run_id") or "") or None,
                filename=managed_file.filename,
                relative_path=managed_file.relative_path,
                metadata_notice=str(extraction.get("metadata_notice") or ""),
            )
            self._persist_source_classification(
                owner_id=str(owner_id),
                revision=revision,
                managed_file=managed_file,
                document=document,
                version=version,
                classification=classification,
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
            analysis.converter_name = str(extraction.get("conversion_converter") or "")
            analysis.converter_version = str(extraction.get("conversion_converter_version") or "")
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
                "warnings": list(extraction.get("warnings") or []),
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

    def refresh_classification(
        self,
        *,
        revision_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """复用持久化页面按当前运行时身份刷新分类，不重新解析原文件。"""

        revision = self.db.get(ManagedFileRevision, revision_id)
        if revision is None or not revision.is_current:
            return {"status": "STALE", "idempotent": True, "revision_id": revision_id}
        managed_file = self.db.get(ManagedFile, revision.managed_file_id)
        root = self.db.get(ManagedRoot, managed_file.root_id) if managed_file else None
        if managed_file is None or root is None or managed_file.status != "ACTIVE":
            return {"status": "STALE", "idempotent": True, "revision_id": revision_id}
        if not revision.analysis_document_id or not revision.analysis_document_version_id:
            raise RuntimeError("源分类刷新缺少已持久化分析文档")
        path = resolve_managed_relative_path(
            root_path=Path(root.container_path),
            relative_path=managed_file.relative_path,
        )
        if not self._matches_revision(revision=revision, stat=path.stat()):
            revision.status = "STALE"
            revision.analysis_status = "STALE"
            self.db.flush()
            return {"status": "STALE", "revision_id": revision.id}
        owner_id = user_id or root.created_by or self._fallback_user_id()
        if not owner_id:
            raise RuntimeError("源分类刷新缺少可审计用户")
        identity = current_classification_identity(
            db=self.db,
            settings=self.settings,
            user_id=str(owner_id),
        )
        freshness = inspect_managed_source_classification(
            db=self.db,
            revision=revision,
            identity=identity,
        )
        if freshness is ClassificationFreshness.CURRENT:
            return {
                "status": "READY",
                "revision_id": revision.id,
                "document_id": revision.analysis_document_id,
                "document_version_id": revision.analysis_document_version_id,
                "classification_refreshed": False,
                "reused_extraction": True,
                "idempotent": True,
            }
        extraction_run = (
            self.db.query(DocumentExtractionRun)
            .filter(
                DocumentExtractionRun.document_id == revision.analysis_document_id,
                DocumentExtractionRun.document_version_id
                == revision.analysis_document_version_id,
                DocumentExtractionRun.status == "COMPLETED",
            )
            .order_by(DocumentExtractionRun.updated_at.desc())
            .first()
        )
        if extraction_run is None:
            raise RuntimeError("源分类刷新缺少已完成的正文解析运行")
        document = self.db.get(Document, revision.analysis_document_id)
        version = self.db.get(DocumentVersion, revision.analysis_document_version_id)
        if document is None or version is None:
            raise RuntimeError("源分类刷新缺少分析 Document 或 DocumentVersion")
        classification = ClassificationRuntimeFactory(self.settings).create(
            db=self.db,
            user_id=str(owner_id),
        ).classify(
            document_id=document.id,
            document_version_id=version.id,
            extraction_run_id=extraction_run.id,
            filename=managed_file.filename,
            force_reprocess=True,
            default_organization_root="学院",
        )
        self._persist_source_classification(
            owner_id=str(owner_id),
            revision=revision,
            managed_file=managed_file,
            document=document,
            version=version,
            classification=classification,
        )
        self.db.flush()
        log_event(
            "managed_source.classification.refreshed",
            status="COMPLETED",
            document_id=document.id,
            document_version_id=version.id,
            managed_file_id=managed_file.id,
            managed_file_revision_id=revision.id,
            taxonomy_key=classification.get("taxonomy_key"),
            taxonomy_version=classification.get("taxonomy_version"),
            classifier_version=classification.get("classifier_version"),
            message="受管源文件已复用持久化正文刷新分类",
        )
        return {
            "status": "READY",
            "revision_id": revision.id,
            "document_id": document.id,
            "document_version_id": version.id,
            "classification_refreshed": True,
            "reused_extraction": True,
            "classification": classification,
        }

    def _persist_source_classification(
        self,
        *,
        owner_id: str,
        revision: ManagedFileRevision,
        managed_file: ManagedFile,
        document: Document,
        version: DocumentVersion,
        classification: dict[str, Any],
    ) -> None:
        """把外部自动导入产生的分类建议写入正式审计与建议表。

        SOURCE_ANALYSIS 过去只把 ``classification`` 放进异步任务回执，导致算法虽然
        执行成功，分类树、文件详情和后续反馈链路却无法读取结果。这里复用生命周期
        审计边界创建内部 AgentRun/ChangeSet，再持久化 SUGGESTED 建议；外部原始文件
        仍保持只读，不产生移动、改名或覆盖动作。
        """

        categories = [
            item
            for item in classification.get("categories", [])
            if isinstance(item, dict)
        ]
        # 延迟导入避免 managed-files worker 与 file-lifecycle service 的模块初始化环。
        from app.modules.classification.service import (
            persist_document_results_classifications,
        )
        from app.modules.file_lifecycle.service import create_lifecycle_audit
        from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id

        document_result = {
            **classification,
            "document_id": document.id,
            "document_version_id": version.id,
            "extraction_status": "COMPLETED",
        }
        changeset, _ = create_lifecycle_audit(
            db=self.db,
            user_id=owner_id,
            workspace_id=get_shared_workspace_id(self.db),
            conversation_id=None,
            tool_name="managed-source-auto-classification",
            message_content=(
                f"外部自动导入文件“{managed_file.filename}”的只读分析和分类已完成。"
            ),
            change_type="CATEGORY_SUGGESTED",
            target_type="managed_file_revision",
            target_id=revision.id,
            target_document_id=document.id,
            after_value={
                "managed_file_revision_id": revision.id,
                "document_version_id": version.id,
                "category_count": len(categories),
                "external_source_unchanged": True,
            },
            graph_document_results=[document_result],
            visible_in_conversation=False,
        )
        persist_document_results_classifications(
            db=self.db,
            agent_run_id=changeset.agent_run_id,
            document_results=[document_result],
            persist_empty_runs=True,
        )
        classification["agent_run_id"] = changeset.agent_run_id
        classification["changeset_id"] = changeset.id

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
        index_run_id: str | None,
        filename: str,
        relative_path: str,
        metadata_notice: str = "",
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
            value
            for value in [
                filename,
                relative_path,
                Path(filename).suffix.lower().lstrip("."),
                summary.summary_text if summary else "",
                " ".join(keywords),
                " ".join(entities),
            ]
            if value
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
        profile.summary = str(summary.summary_text if summary else metadata_notice)
        profile.topic_summary_json = {
            **topic_json,
            **(
                {"analysis_notice": metadata_notice, "metadata_only": True}
                if metadata_notice
                else {}
            ),
        }
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
        chunks = []
        if index_run_id:
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
        if expected_mtime.tzinfo is None:
            # SQLite 测试和部分历史迁移会丢失 timestamptz 的时区标记；数据库值
            # 仍按项目统一约定表示 UTC，不能让宿主机本地时区把同一文件误判为变化。
            expected_mtime = expected_mtime.replace(tzinfo=timezone.utc)
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


def _extract_managed_source_document(
    *,
    db: Session,
    document: Document,
    document_version: DocumentVersion,
    source_path: Path,
) -> dict[str, Any]:
    """通过统一可读源解析受管原件，使旧 DOC/XLS 复用版本级持久化派生件。"""

    readable_source = ReadableDocumentSourceResolver(db=db).resolve(
        document=document,
        document_version=document_version,
        original_path=source_path,
    )
    extraction = extract_document_text(
        file_path=readable_source.parse_path,
        filename=readable_source.parse_filename,
        content_type=readable_source.parse_content_type,
    )
    return apply_readable_source_metadata(extraction, source=readable_source)


def _prepare_image_metadata_extraction(
    *,
    extraction: dict[str, Any],
    filename: str,
    content_type: str,
) -> dict[str, Any]:
    """让无文字或 OCR 异常图片保留空正文，并发布可重建元数据投影。"""

    suffix = Path(filename).suffix.lower()
    if not (
        str(content_type or "").startswith("image/")
        or suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}
    ):
        return extraction

    pages = [dict(page) for page in list(extraction.get("pages") or [])]
    has_text = any(str(page.get("text") or "").strip() for page in pages)
    if extraction.get("ok") and has_text:
        return extraction

    if extraction.get("ok"):
        warning = {
            "code": "IMAGE_NO_TEXT",
            "message": "图片未识别到可用文字，已按文件名、相对目录和文件类型建立元数据索引。",
            "retryable": False,
        }
        notice = "图片无可识别文字；已保留目录关系和元数据检索能力。"
        if not pages:
            pages = [{"page_number": 1, "sheet_name": None, "text": "", "metadata": {}}]
        extractor = str(extraction.get("extractor") or "image-metadata")
        read_profile = dict(extraction.get("read_profile") or {})
    else:
        error = dict(extraction.get("error") or {})
        warning = {
            "code": str(error.get("code") or "OCR_FAILED"),
            "message": "图片 OCR 处理失败，已按文件名、相对目录和文件类型建立元数据索引。",
            "retryable": True,
        }
        notice = "图片 OCR 处理失败；已保留目录关系和元数据检索能力。"
        pages = [{"page_number": 1, "sheet_name": None, "text": "", "metadata": {}}]
        extractor = "image-metadata"
        read_profile = {
            "file_type": "image",
            "page_count": 1,
            "sheet_count": 0,
            "char_count": 0,
            "has_text": False,
            "requires_ocr": True,
            "ocr_used": True,
        }

    warning_payload = {"code": warning["code"], "message": warning["message"]}
    profiled_pages = []
    for page in pages:
        metadata = dict(page.get("metadata") or {})
        metadata.update(
            {
                "metadata_only": True,
                "image_text_status": (
                    "NO_TEXT" if warning["code"] == "IMAGE_NO_TEXT" else "OCR_FAILED"
                ),
                "analysis_warnings": [warning_payload],
                "read_quality": "PARTIAL",
            }
        )
        profiled_pages.append({**page, "text": "", "metadata": metadata})

    warnings = [
        *[dict(item) for item in list(extraction.get("warnings") or []) if isinstance(item, dict)],
        warning,
    ]
    return {
        **extraction,
        "ok": True,
        "status": "COMPLETED",
        "extractor": extractor,
        "read_quality": "PARTIAL",
        "read_profile": read_profile,
        "pages": profiled_pages,
        "elements": [],
        "warnings": warnings,
        "error": None,
        "metadata_only": True,
        "metadata_notice": notice,
    }


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
    # 解析器通常返回 ``A1:D8``，历史数据也可能包含绝对引用、小写列名或
    # 单个单元格。范围只是可重建结构提示，任何畸形值都必须被忽略，不能让
    # 一份可正常读取的 Excel 因辅助统计失败而终止整个源侧分析。
    cell_pattern = re.compile(
        r"\$?([A-Z]+)\$?(\d+)(?::\$?([A-Z]+)\$?(\d+))?",
        re.IGNORECASE,
    )
    max_row = len(lines)
    for item in ranges:
        if not isinstance(item, dict):
            continue
        match = cell_pattern.fullmatch(
            str((item or {}).get("cell_range") or "").strip()
        )
        if match is None:
            continue
        start_column, start_row, end_column, end_row = match.groups()
        max_row = max(max_row, int(start_row), int(end_row or start_row))
        max_column = max(
            max_column,
            _column_number(start_column.upper()),
            _column_number((end_column or start_column).upper()),
        )
    return max_row, max_column, headers


def _column_number(value: str) -> int:
    """把 Excel 列字母转为确定性列号。"""

    result = 0
    for char in value:
        result = result * 26 + ord(char) - ord("A") + 1
    return result
