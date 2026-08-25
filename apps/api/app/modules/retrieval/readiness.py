"""检索就绪协调服务。

双范围检索优先使用已经完成的源侧分析或活动工作副本。本服务只在相关源文件
尚未分析时提升 ``SOURCE_ANALYSIS``，而不是为一次读取抢先复制工作副本；源侧
分析完成后可直接回答，工作副本由全量后台同步创建，最终相关文件会提升同一任务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentVersion,
    FilesystemJob,
    ManagedFile,
    ManagedRoot,
    UploadArchiveRecord,
    WorkingCopy,
)
from app.modules.file_lifecycle.service import working_copy_search_artifact_status
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.source_analysis import ManagedFileRevisionService
from app.modules.retrieval.synonym_service import (
    FileSearchSynonymService,
    expand_scope_entity_phrases,
    split_entity_topic_phrase,
)


@dataclass(frozen=True)
class CanonicalDocumentScope:
    """上传附件 ID 到共享工作副本 ID 的解析结果。"""

    document_ids: list[str]
    unresolved_document_ids: list[str]


class WorkingCopySearchReadinessService:
    """协调活动工作副本检索与尚未完成的后台准备任务。"""

    def __init__(self, *, db: Session, user_id: str, workspace_id: str) -> None:
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id

    def canonicalize_document_ids(self, document_ids: list[str]) -> CanonicalDocumentScope:
        """把上传 Document ID 映射为当前活动共享工作副本 Document ID。"""

        requested = list(dict.fromkeys(str(value) for value in document_ids if str(value)))
        if not requested:
            return CanonicalDocumentScope([], [])
        direct = {
            str(value)
            for (value,) in self.db.query(WorkingCopy.document_id)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.status == "ACTIVE",
                WorkingCopy.document_id.in_(requested),
            )
            .all()
        }
        unresolved = [value for value in requested if value not in direct]
        mapped: dict[str, str] = {}
        if unresolved:
            versions = (
                self.db.query(DocumentVersion)
                .join(Document, Document.id == DocumentVersion.document_id)
                .filter(
                    DocumentVersion.document_id.in_(unresolved),
                    DocumentVersion.storage_tier == "UPLOAD",
                    Document.user_id == self.user_id,
                )
                .order_by(DocumentVersion.document_id, DocumentVersion.version_number.desc())
                .all()
            )
            latest: dict[str, DocumentVersion] = {}
            for version in versions:
                latest.setdefault(str(version.document_id), version)
            archives = (
                self.db.query(UploadArchiveRecord)
                .filter(
                    UploadArchiveRecord.upload_document_version_id.in_(
                        [item.id for item in latest.values()]
                    ),
                    UploadArchiveRecord.managed_file_id.is_not(None),
                )
                .all()
                if latest
                else []
            )
            archive_by_version = {str(item.upload_document_version_id): item for item in archives}
            managed_ids = [str(item.managed_file_id) for item in archives if item.managed_file_id]
            copies = (
                self.db.query(WorkingCopy)
                .filter(
                    WorkingCopy.workspace_id == self.workspace_id,
                    WorkingCopy.status == "ACTIVE",
                    WorkingCopy.managed_file_id.in_(managed_ids),
                )
                .all()
                if managed_ids
                else []
            )
            copy_by_managed = {str(item.managed_file_id): item for item in copies}
            for upload_document_id, version in latest.items():
                archive = archive_by_version.get(str(version.id))
                copy = copy_by_managed.get(str(archive.managed_file_id)) if archive else None
                if copy is not None:
                    mapped[upload_document_id] = str(copy.document_id)
        # 保留用户附件顺序，避免多文件确认或后续摘要的展示顺序因集合遍历而漂移。
        canonical = [
            value if value in direct else mapped[value]
            for value in requested
            if value in direct or value in mapped
        ]
        still_unresolved = [value for value in unresolved if value not in mapped]
        return CanonicalDocumentScope(list(dict.fromkeys(canonical)), still_unresolved)

    def prepare_on_miss(
        self,
        *,
        query: str,
        parsed: Any,
        unresolved_upload_document_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """静默创建或提升本次检索可能依赖的源侧分析任务。

        返回值只供 Agent Runtime 进入通用处理中状态，禁止包含候选文件名、原始路径、
        “待准备”或队列阶段等用户可见信息。
        """

        upload_document_ids = unresolved_upload_document_ids or []
        log_event(
            "retrieval.readiness.started",
            tool_name="hybrid-search",
            status="RUNNING",
            workspace_id=self.workspace_id,
            unresolved_upload_count=len(upload_document_ids),
            has_year=getattr(parsed, "year", None) is not None,
            query_term_count=len(list(getattr(parsed, "terms", []) or [])),
            message="工作副本检索就绪检查开始",
        )
        job_ids = self._pending_upload_job_ids(upload_document_ids)
        managed_files = self._managed_files_for_uploads(upload_document_ids)
        # 明确附件尚未形成工作副本时只能沿该附件血缘推进，不能再用查询文本匹配
        # 其他受管文件，否则会静默准备与用户附件无关的候选。
        if not upload_document_ids and not managed_files:
            managed_files = self._managed_candidates(query=query, parsed=parsed)
        log_event(
            "retrieval.readiness.candidates_resolved",
            tool_name="hybrid-search",
            status="COMPLETED",
            workspace_id=self.workspace_id,
            pending_upload_dependency_count=len(job_ids),
            managed_candidate_count=len(managed_files),
            attachment_lineage_only=bool(upload_document_ids),
            message="静默准备候选解析完成",
        )
        for managed_file in managed_files:
            job = self._ensure_ready_job(managed_file)
            if job is not None and job.status in {"PENDING", "RUNNING"}:
                job_ids.append(str(job.id))
        if not job_ids:
            log_event(
                "retrieval.readiness.completed",
                level="WARNING",
                tool_name="hybrid-search",
                status="SKIPPED",
                workspace_id=self.workspace_id,
                dependency_count=0,
                error_code="NO_PREPARABLE_DEPENDENCY",
                message="没有可等待或可提升的检索准备任务",
            )
            return None
        unique_job_ids = list(dict.fromkeys(job_ids))
        log_event(
            "retrieval.readiness.completed",
            tool_name="hybrid-search",
            status="WAITING_FOR_ASYNC_JOB",
            workspace_id=self.workspace_id,
            dependency_count=len(unique_job_ids),
            message="检索准备任务已创建或提升优先级",
        )
        return {
            "kind": "filesystem_job",
            "ok": True,
            "status": "PROCESSING",
            # 仅供普通回执投影识别并隐藏任务 ID；不包含候选文件或路径。
            "source": "search-readiness",
            "job_id": job_ids[0],
            "job_ids": unique_job_ids,
        }

    def _pending_upload_job_ids(self, document_ids: list[str]) -> list[str]:
        """提升未完成的上传生命周期任务，且不把阶段名称暴露给用户。"""

        if not document_ids:
            return []
        version_ids = [
            str(value)
            for (value,) in self.db.query(DocumentVersion.id)
            .join(Document, Document.id == DocumentVersion.document_id)
            .filter(
                DocumentVersion.document_id.in_(document_ids),
                DocumentVersion.storage_tier == "UPLOAD",
                Document.user_id == self.user_id,
            )
            .all()
        ]
        archives = (
            self.db.query(UploadArchiveRecord)
            .filter(
                UploadArchiveRecord.upload_document_version_id.in_(version_ids),
                UploadArchiveRecord.filesystem_job_id.is_not(None),
            )
            .all()
            if version_ids
            else []
        )
        queue = FilesystemJobQueue(self.db)
        job_ids: list[str] = []
        for archive in archives:
            job = self.db.get(FilesystemJob, archive.filesystem_job_id)
            if (
                job is None
                or job.status not in {"PENDING", "RUNNING"}
                or job.attempt_count >= job.max_attempts
            ):
                continue
            queue.promote_pending_job(job=job, priority=10)
            job_ids.append(str(job.id))
        return job_ids

    def prepare_after_miss(
        self,
        *,
        parsed_query: Any,
        unresolved_document_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """兼容检索 Tool 的最小入口，所有候选仍保持内部不可见。"""

        return self.prepare_on_miss(
            query=str(getattr(parsed_query, "original", "") or ""),
            parsed=parsed_query,
            unresolved_upload_document_ids=unresolved_document_ids,
        )

    def _managed_files_for_uploads(self, document_ids: list[str]) -> list[ManagedFile]:
        """通过上传归档血缘定位尚未形成活动副本的受管文件。"""

        if not document_ids:
            return []
        versions = (
            self.db.query(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .filter(
                DocumentVersion.document_id.in_(document_ids),
                DocumentVersion.storage_tier == "UPLOAD",
                Document.user_id == self.user_id,
            )
            .all()
        )
        archives = (
            self.db.query(UploadArchiveRecord)
            .filter(
                UploadArchiveRecord.upload_document_version_id.in_([item.id for item in versions]),
                UploadArchiveRecord.managed_file_id.is_not(None),
            )
            .all()
            if versions
            else []
        )
        managed_ids = [str(item.managed_file_id) for item in archives if item.managed_file_id]
        return (
            self.db.query(ManagedFile)
            .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
            .filter(
                ManagedFile.id.in_(managed_ids),
                ManagedFile.status == "ACTIVE",
                ManagedRoot.enabled.is_(True),
            )
            .all()
            if managed_ids
            else []
        )

    def _managed_candidates(self, *, query: str, parsed: Any) -> list[ManagedFile]:
        """仅用受管元数据发现候选；这些记录不会直接进入普通用户结果。"""

        year = getattr(parsed, "year", None)
        fact_anchors = [
            str(value).strip()
            for value in list(getattr(parsed, "fact_anchor_phrases", []) or [])
            if str(value).strip()
        ]
        fact_entities = {
            str(value).strip()
            for value in list(getattr(parsed, "fact_entity_phrases", []) or [])
            if str(value).strip()
        }
        metadata_fact_anchors = [
            value for value in fact_anchors if value not in fact_entities
        ]
        cleaned = str(
            (metadata_fact_anchors[0] if metadata_fact_anchors else None)
            or getattr(parsed, "cleaned", "")
            or ""
        )
        synonym_service = FileSearchSynonymService()
        equivalent_mention = synonym_service.find_equivalent_mention(cleaned)
        # 摘要回退路径没有 Jieba tokenizer，需把复合查询按稳定业务条件处理。
        # 正式机构别名按同一实体匹配，但机构与主题必须同时命中，不能用 OR
        # 静默准备其他学院的同主题文件。
        entity_terms: list[str] = []
        topic_phrase = ""
        if equivalent_mention is not None:
            group, matched_name = equivalent_mention
            entity_terms = [value.lower() for value in group.phrases]
            topic_phrase = cleaned.replace(matched_name, " ", 1).strip().lower()
            topic_phrase = re.sub(
                r"^[的与和及\s]+|[的与和及\s]+$",
                "",
                topic_phrase,
            )
        else:
            entity_topic = split_entity_topic_phrase(cleaned)
            if entity_topic is not None:
                entity_phrase, topic_phrase = entity_topic
                topic_phrase = topic_phrase.lower()
                # 受管源侧只能用元数据找待物化候选，但范围实体仍必须与主题同时
                # 命中。学校不是“搜索全部工作区”的别名，不能绕过这个条件。
                entity_terms = [
                    value.lower()
                    for value in expand_scope_entity_phrases(entity_phrase)
                ]
        equivalent_phrases = synonym_service.expand_equivalent_mentions(cleaned) or (
            cleaned,
        )
        fragments = [
            fragment
            for phrase in equivalent_phrases
            for fragment in re.findall(
                r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}",
                re.sub(r"的", " ", phrase.lower()),
            )
        ]
        terms = list(
            dict.fromkeys(
                [
                    *metadata_fact_anchors,
                    *fragments,
                    *getattr(parsed, "terms", []),
                ]
            )
        )
        terms = [str(term).strip().lower() for term in terms if len(str(term).strip()) >= 2]
        metadata = func.lower(ManagedFile.filename + " " + ManagedFile.relative_path)
        filters = []
        if year is not None:
            filters.append(metadata.contains(str(year)))
        if entity_terms:
            filters.append(
                or_(*(metadata.contains(term) for term in entity_terms))
            )
            if len(topic_phrase) >= 2:
                filters.append(metadata.contains(topic_phrase))
        elif len(topic_phrase) >= 2:
            filters.append(metadata.contains(topic_phrase))
        elif terms:
            filters.append(or_(*(metadata.contains(term) for term in terms[:12])))
        if not filters:
            return []
        relevance = (
            sum(
                (
                    case((metadata.contains(term), 1), else_=0)
                    for term in terms[:12]
                ),
                0,
            )
            if terms
            else None
        )
        ordering = [ManagedFile.updated_at.desc()]
        if relevance is not None:
            ordering.insert(0, relevance.desc())
        rows = (
            self.db.query(ManagedFile)
            .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
            .filter(
                ManagedFile.status == "ACTIVE",
                ManagedRoot.enabled.is_(True),
                *filters,
            )
            # 同时命中机构名称和主题的文件优先创建任务，避免泛化“工作总结”
            # 候选排在真实学院文件之前，导致页面长时间保持处理中。
            .order_by(*ordering)
            .limit(40)
            .all()
        )
        candidates = [
            item
            for item in rows
            if "/.internal/" not in f"/{item.relative_path}/"
        ]
        log_event(
            "retrieval.readiness.managed_candidates.completed",
            level="WARNING" if not candidates else "INFO",
            tool_name="hybrid-search",
            status="COMPLETED",
            workspace_id=self.workspace_id,
            candidate_count=len(candidates),
            entity_alias_mode=bool(entity_terms),
            has_topic_condition=bool(topic_phrase),
            has_year=year is not None,
            term_count=len(terms),
            message="受管元数据静默候选检索完成",
        )
        return candidates

    def _ensure_ready_job(self, managed_file: ManagedFile) -> FilesystemJob | None:
        """保证候选拥有可回答的源侧索引或活动副本，终态失败任务绝不自动重开。

        新受管目录的首次访问必须优先等待 ``SOURCE_ANALYSIS``，而不是抢先复制
        工作副本。待源侧分析完成后，双范围检索会直接使用其证据回答；全量后台
        同步会创建 ``MATERIALIZE_WORKING_COPY``，最终相关文件只提升其优先级。
        """

        queue = FilesystemJobQueue(self.db)
        working_copy = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.workspace_id == self.workspace_id,
                WorkingCopy.managed_file_id == managed_file.id,
                WorkingCopy.status == "ACTIVE",
            )
            .one_or_none()
        )
        if working_copy is None:
            settings = get_settings()
            if (
                settings.managed_file_initialization_mode == "source_index_first"
                and settings.managed_source_analysis_enabled
            ):
                revision = ManagedFileRevisionService(db=self.db).ensure_current_revision(
                    managed_file=managed_file
                )
                if revision.status == "READY":
                    # 检索将于本轮后续重试中直接读取源侧资料，无需提前复制。
                    return None
                if revision.status == "FAILED":
                    # 已知终态失败不能由用户重复查询隐式重开，管理员重处理才可重试。
                    return None
                job = queue.create_job(
                    job_type="ANALYZE_MANAGED_FILE_REVISION",
                    queue_name="SOURCE_ANALYSIS",
                    root_id=managed_file.root_id,
                    created_by=self.user_id,
                    deduplication_key=f"managed-source-analysis:{revision.id}",
                    priority=settings.managed_source_analysis_on_demand_priority,
                    max_attempts=3,
                    payload={
                        "managed_file_revision_id": revision.id,
                        "user_id": self.user_id,
                    },
                )
                if job.status == "PENDING":
                    queue.promote_pending_job(
                        job=job,
                        priority=settings.managed_source_analysis_on_demand_priority,
                    )
                return job
            # 兼容部署显式启用 eager_working_copy 的旧迁移模式；默认路径不会进入。
            job = queue.create_job(
                job_type="IMPORT_WORKING_COPIES",
                queue_name="IMPORT",
                root_id=managed_file.root_id,
                created_by=self.user_id,
                deduplication_key=f"working-copy-import:{self.workspace_id}:{managed_file.id}",
                priority=10,
                max_attempts=3,
                # 任务显示完成但活动副本不存在，通常表示开发存储被清理；允许一致性
                # 补偿同一任务。FAILED 终态仍不会被此路径重新激活。
                reuse_completed=True,
                payload={
                    "managed_file_id": managed_file.id,
                    "workspace_id": self.workspace_id,
                    "user_id": self.user_id,
                },
            )
            promoted = queue.promote_pending_job(job=job, priority=10)
            log_event(
                "retrieval.readiness.import_dependency",
                tool_name="hybrid-search",
                document_id=None,
                status=promoted.status,
                workspace_id=self.workspace_id,
                managed_file_id=managed_file.id,
                filesystem_job_id=promoted.id,
                attempt_count=promoted.attempt_count,
                max_attempts=promoted.max_attempts,
                message="候选尚无活动工作副本，已确认导入依赖",
            )
            return promoted
        status = working_copy_search_artifact_status(self.db, working_copy)
        if status["ready"] or status["repair_blocked"] or not working_copy.current_version_id:
            log_event(
                "retrieval.readiness.artifact_checked",
                level="WARNING" if status["repair_blocked"] else "INFO",
                tool_name="hybrid-search",
                document_id=working_copy.document_id,
                status=(
                    "READY"
                    if status["ready"]
                    else (
                        "BLOCKED"
                        if status["repair_blocked"]
                        else "SKIPPED"
                    )
                ),
                workspace_id=self.workspace_id,
                working_copy_id=working_copy.id,
                profile_ready=status["profile_ready"],
                index_ready=status["index_ready"],
                repair_blocked=status["repair_blocked"],
                message="活动工作副本检索派生状态检查完成",
            )
            return None
        job = queue.create_job(
            job_type="ANALYZE_DOCUMENT_VERSION",
            queue_name="ANALYSIS",
            root_id=managed_file.root_id,
            created_by=self.user_id,
            deduplication_key=f"document-analysis:{working_copy.current_version_id}",
            priority=10,
            max_attempts=3,
            payload={
                "managed_file_id": managed_file.id,
                "working_copy_id": working_copy.id,
                "document_id": working_copy.document_id,
                "document_version_id": working_copy.current_version_id,
                "user_id": self.user_id,
            },
        )
        promoted = queue.promote_pending_job(job=job, priority=10)
        log_event(
            "retrieval.readiness.analysis_dependency",
            tool_name="hybrid-search",
            document_id=working_copy.document_id,
            status=promoted.status,
            workspace_id=self.workspace_id,
            working_copy_id=working_copy.id,
            filesystem_job_id=promoted.id,
            profile_ready=status["profile_ready"],
            index_ready=status["index_ready"],
            attempt_count=promoted.attempt_count,
            max_attempts=promoted.max_attempts,
            message="活动工作副本缺少检索派生数据，已确认分析依赖",
        )
        return promoted
