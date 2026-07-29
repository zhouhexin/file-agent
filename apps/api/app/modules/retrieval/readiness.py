"""工作副本检索就绪协调服务。

普通问答和文件检索仍只读取活动工作副本。本服务只在活动范围没有命中时，用
``managed_files`` 元数据发现可能尚未导入的文件，并静默提升对应导入/分析任务；
受管原件不能直接作为回答或“已找到文件”的依据。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

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
        """静默创建或提升本次检索可能依赖的导入、分析任务。

        返回值只供 Agent Runtime 进入通用处理中状态，禁止包含候选文件名、原始路径、
        “待准备”或队列阶段等用户可见信息。
        """

        upload_document_ids = unresolved_upload_document_ids or []
        job_ids = self._pending_upload_job_ids(upload_document_ids)
        managed_files = self._managed_files_for_uploads(upload_document_ids)
        # 明确附件尚未形成工作副本时只能沿该附件血缘推进，不能再用查询文本匹配
        # 其他受管文件，否则会静默准备与用户附件无关的候选。
        if not upload_document_ids and not managed_files:
            managed_files = self._managed_candidates(query=query, parsed=parsed)
        for managed_file in managed_files:
            job = self._ensure_ready_job(managed_file)
            if job is not None and job.status in {"PENDING", "RUNNING"}:
                job_ids.append(str(job.id))
        if not job_ids:
            return None
        return {
            "kind": "filesystem_job",
            "ok": True,
            "status": "PROCESSING",
            # 仅供普通回执投影识别并隐藏任务 ID；不包含候选文件或路径。
            "source": "search-readiness",
            "job_id": job_ids[0],
            "job_ids": list(dict.fromkeys(job_ids)),
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
        cleaned = str(getattr(parsed, "cleaned", "") or "")
        # 摘要回退路径没有 Jieba tokenizer，需把“计算机学院的工作总结”按中文
        # 连接词拆成稳定业务短语，不能要求文件名连续包含无信息量的“的”。
        fragments = re.findall(
            r"[a-z0-9]{2,}|[\u4e00-\u9fff]{2,}",
            re.sub(r"的", " ", cleaned.lower()),
        )
        terms = list(dict.fromkeys([*fragments, *getattr(parsed, "terms", [])]))
        terms = [str(term).strip().lower() for term in terms if len(str(term).strip()) >= 2]
        metadata = func.lower(ManagedFile.filename + " " + ManagedFile.relative_path)
        filters = []
        if year is not None:
            filters.append(metadata.contains(str(year)))
        if terms:
            filters.append(or_(*(metadata.contains(term) for term in terms[:12])))
        if not filters:
            return []
        rows = (
            self.db.query(ManagedFile)
            .join(ManagedRoot, ManagedRoot.id == ManagedFile.root_id)
            .filter(
                ManagedFile.status == "ACTIVE",
                ManagedRoot.enabled.is_(True),
                *filters,
            )
            .order_by(ManagedFile.updated_at.desc())
            .limit(40)
            .all()
        )
        return [item for item in rows if "/.internal/" not in f"/{item.relative_path}/"]

    def _ensure_ready_job(self, managed_file: ManagedFile) -> FilesystemJob | None:
        """保证候选拥有活动副本及检索派生数据，终态失败任务绝不自动重开。"""

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
            return queue.promote_pending_job(job=job, priority=10)
        status = working_copy_search_artifact_status(self.db, working_copy)
        if status["ready"] or status["repair_blocked"] or not working_copy.current_version_id:
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
        return queue.promote_pending_job(job=job, priority=10)
