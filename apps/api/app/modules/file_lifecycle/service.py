"""三层文件生命周期业务服务。

HTTP 请求只创建状态和持久化任务；查重、归档、导入和清理均由 worker 调用本模块的处理方法。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    Conversation,
    Document,
    DocumentVersion,
    FileObject,
    FileRenameReviewItem,
    FilesystemJob,
    ManagedFile,
    ManagedRoot,
    Message,
    ToolInvocation,
    UploadArchiveRecord,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    TrashEntry,
    User,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.file_lifecycle.repository import FileLifecycleRepository
from app.modules.file_lifecycle.organizer import InitialWorkingCopyOrganizer
from app.modules.file_lifecycle.schemas import (
    ArchiveStatusResponse,
    DocumentVersionResponse,
    DuplicateCandidateResponse,
    DuplicateDecisionRequest,
    DuplicateDecisionResponse,
    DuplicateReviewResponse,
    WorkingCopyLineageResponse,
    WorkingCopyPathRecordResponse,
    WorkingCopyResponse,
    TrashEntryResponse,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.file_lifecycle.risk import inspect_basic_file_risks
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.path_policy import resolve_managed_relative_path
from app.modules.classification.service import persist_document_results_classifications
from app.modules.chunks.service import DocumentIndexService


@dataclass(slots=True)
class InitialWorkingPathResolution:
    """首次导入的安全目标；冲突时只返回待确认位置，不自动生成版本后缀。"""

    relative_path: str
    filename: str
    conflict: dict[str, Any] | None = None


class UploadLifecycleService:
    """上传请求侧的生命周期服务，只写数据库状态并创建异步任务。"""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        """注入数据库会话和配置。"""

        self.db = db
        self.settings = settings or get_settings()
        self.repository = FileLifecycleRepository(db)

    def register_upload(
        self,
        *,
        document: Document,
        storage_path: str,
        conversation_id: str | None,
    ) -> tuple[DocumentVersion, UploadArchiveRecord, UploadDuplicateReview, FilesystemJob]:
        """登记上传版本并在同一事务创建查重任务。

        生产环境禁止关闭查重后继续归档；配置异常必须显式失败，不能静默绕过确认。
        """

        if not self.settings.filesystem_async_jobs_enabled:
            raise RuntimeError("FILESYSTEM_ASYNC_JOBS_ENABLED 必须开启")
        if not self.settings.upload_duplicate_check_enabled:
            raise RuntimeError("UPLOAD_DUPLICATE_CHECK_ENABLED 必须开启")
        version = self.repository.create_upload_version(
            document=document,
            storage_path=storage_path,
            created_by=document.user_id,
        )
        archive, review = self.repository.create_upload_lifecycle(
            version=version,
            document=document,
            conversation_id=conversation_id,
            ttl_hours=self.settings.upload_duplicate_confirmation_ttl_hours,
        )
        job = FilesystemJobQueue(self.db).create_job(
            job_type="CHECK_UPLOAD_DUPLICATES",
            queue_name="DUPLICATE_CHECK",
            root_id=None,
            created_by=document.user_id,
            deduplication_key=f"upload-duplicate:{version.id}",
            payload={
                "upload_document_version_id": version.id,
                "duplicate_review_id": review.id,
                "user_id": document.user_id,
                "workspace_id": document.workspace_id,
            },
        )
        review.duplicate_check_job_id = job.id
        archive.filesystem_job_id = job.id
        self.db.flush()
        return version, archive, review, job

    def get_review(self, *, upload_version_id: str, current_user: User) -> DuplicateReviewResponse:
        """查询当前用户上传版本的重复确认卡。"""

        review = self.repository.get_review_by_version(upload_version_id)
        if review is None or review.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Duplicate review not found")
        return self.to_review_response(review)

    def decide(
        self,
        *,
        upload_version_id: str,
        request: DuplicateDecisionRequest,
        current_user: User,
    ) -> DuplicateDecisionResponse:
        """幂等保存显式决策，并只为允许归档的选择创建后续任务。"""

        review = (
            self.db.query(UploadDuplicateReview)
            .filter(
                UploadDuplicateReview.id == request.duplicate_review_id,
                UploadDuplicateReview.user_id == current_user.id,
            )
            .with_for_update()
            .one_or_none()
        )
        if review is None or review.upload_document_version_id != upload_version_id:
            raise HTTPException(status_code=404, detail="Duplicate review not found")
        archive = (
            self.db.query(UploadArchiveRecord)
            .filter(UploadArchiveRecord.upload_document_version_id == upload_version_id)
            .with_for_update()
            .one_or_none()
        )
        if archive is None:
            raise HTTPException(status_code=409, detail="Upload archive state not found")
        if review.status == "RESOLVED":
            if review.decision != request.decision:
                raise HTTPException(status_code=409, detail="Duplicate review already resolved")
            return DuplicateDecisionResponse(
                review=self.to_review_response(review),
                archive_status=archive.status,
                filesystem_job_id=archive.filesystem_job_id,
                selected_existing_document_id=self._selected_document_id(review),
            )
        if review.status != "WAITING_CONFIRMATION":
            raise HTTPException(status_code=409, detail="Duplicate review is not waiting for confirmation")
        expires_at = review.expires_at
        if expires_at.tzinfo is None:
            # SQLite 测试会丢失 timezone 信息；生产 PostgreSQL 保持 timestamptz。
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < utcnow():
            review.status = "EXPIRED"
            self.db.commit()
            raise HTTPException(status_code=409, detail="Duplicate review expired")

        selected_copy: WorkingCopy | None = None
        if request.decision == "USE_EXISTING_FILE":
            selected_copy = self._validate_existing_candidate(
                review=review,
                working_copy_id=str(request.selected_existing_working_copy_id),
            )
            review.selected_existing_working_copy_id = selected_copy.id
            archive.status = "EXISTING_FILE_SELECTED"
            cleanup_job = self._enqueue_cleanup(review=review)
            archive.filesystem_job_id = cleanup_job.id
        elif request.decision == "CONTINUE_UPLOAD":
            archive.status = "PENDING"
            job = self._enqueue_archive(review=review, archive=archive)
            archive.filesystem_job_id = job.id
        else:
            archive.status = "CANCELLED"
            job = self._enqueue_cleanup(review=review)
            archive.filesystem_job_id = job.id

        review.status = "RESOLVED"
        review.decision = request.decision
        review.decided_at = utcnow()
        upload_version = self.db.get(DocumentVersion, review.upload_document_version_id)
        upload_document = self.db.get(Document, upload_version.document_id) if upload_version else None
        if upload_document is not None and request.decision in {"USE_EXISTING_FILE", "CANCEL_UPLOAD"}:
            upload_document.status = (
                "UPLOAD_REPLACED_BY_EXISTING"
                if request.decision == "USE_EXISTING_FILE"
                else "UPLOAD_CANCELLED"
            )
        if review.conversation_id:
            conversation = self.db.get(Conversation, review.conversation_id)
            if conversation is not None and conversation.user_id == current_user.id:
                # 前端确认按钮也是一次明确用户输入，必须形成消息审计，不能只改状态字段。
                confirmation_message = Message(
                    conversation_id=conversation.id,
                    user_id=current_user.id,
                    role="user",
                    content=f"重复上传处理：{request.decision}",
                    attachments_json=[
                        {
                            "type": "duplicate_upload_decision",
                            "duplicate_review_id": review.id,
                            "upload_document_version_id": review.upload_document_version_id,
                            "decision": request.decision,
                        }
                    ],
                )
                self.db.add(confirmation_message)
                self.db.flush()
                review.confirmation_message_id = confirmation_message.id
        self._append_audit(
            review=review,
            change_type="UPLOAD_DUPLICATE_DECISION_RECORDED",
            summary=f"已记录重复上传决策：{request.decision}",
            after_value={
                "decision": request.decision,
                "selected_existing_working_copy_id": selected_copy.id if selected_copy else None,
            },
        )
        self.db.commit()
        return DuplicateDecisionResponse(
            review=self.to_review_response(review),
            archive_status=archive.status,
            filesystem_job_id=archive.filesystem_job_id,
            selected_existing_document_id=selected_copy.document_id if selected_copy else None,
        )

    def cancel_unsent_upload(self, *, document: Document) -> FilesystemJob | None:
        """取消尚未发送的上传，并异步清理暂存文件。

        已归档原始文件和工作副本不受影响；这里只终止上传暂存生命周期。
        """

        version = (
            self.db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == document.id, DocumentVersion.storage_tier == "UPLOAD")
            .order_by(DocumentVersion.version_number.desc())
            .first()
        )
        if version is None:
            return None
        review = (
            self.db.query(UploadDuplicateReview)
            .filter(UploadDuplicateReview.upload_document_version_id == version.id)
            .with_for_update()
            .one_or_none()
        )
        archive = (
            self.db.query(UploadArchiveRecord)
            .filter(UploadArchiveRecord.upload_document_version_id == version.id)
            .with_for_update()
            .one_or_none()
        )
        if archive and archive.status not in {"ARCHIVED", "EXISTING_FILE_SELECTED", "CANCELLED"}:
            archive.status = "CANCELLED"
        if review and review.status not in {"RESOLVED", "EXPIRED"}:
            review.status = "RESOLVED"
            review.decision = "CANCEL_UPLOAD"
            review.decided_at = utcnow()
        return self._enqueue_cleanup(review=review) if review else None

    def get_archive_status(self, *, upload_version_id: str, current_user: User) -> ArchiveStatusResponse:
        """查询上传版本归档和工作副本导入结果。"""

        version = self.repository.get_upload_version(upload_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="Upload version not found")
        document = self.db.get(Document, version.document_id)
        if document is None or document.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Upload version not found")
        archive = self.repository.get_archive_by_version(upload_version_id)
        if archive is None:
            raise HTTPException(status_code=404, detail="Archive status not found")
        working_copy = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.managed_file_id == archive.managed_file_id, WorkingCopy.workspace_id == document.workspace_id)
            .first()
            if archive.managed_file_id
            else None
        )
        return ArchiveStatusResponse(
            upload_document_version_id=upload_version_id,
            status=archive.status,
            managed_file_id=archive.managed_file_id,
            working_copy_id=working_copy.id if working_copy else None,
            filesystem_job_id=archive.filesystem_job_id,
            error_code=archive.last_error_code,
            error_message=archive.last_error_message,
        )

    def to_review_response(self, review: UploadDuplicateReview) -> DuplicateReviewResponse:
        """把内部候选转换为脱敏 API 响应。"""

        version = self.db.get(DocumentVersion, review.upload_document_version_id)
        if version is None:
            raise RuntimeError("重复确认对应的上传版本不存在")
        candidates = self.repository.list_candidates(review.id)
        response_candidates: list[DuplicateCandidateResponse] = []
        can_use_existing = False
        for candidate in candidates:
            visible_working_copy_id = (
                candidate.candidate_working_copy_id
                if candidate.match_scope in {"SAME_WORKSPACE", "SAME_USER"}
                and self._candidate_accessible(review=review, candidate=candidate)
                else None
            )
            can_use_existing = can_use_existing or visible_working_copy_id is not None
            response_candidates.append(
                DuplicateCandidateResponse(
                    id=candidate.id,
                    match_type=candidate.match_type,
                    match_scope=candidate.match_scope,
                    similarity_score=candidate.similarity_score,
                    summary=dict(candidate.user_visible_summary_json or {}),
                    existing_working_copy_id=visible_working_copy_id,
                    existing_document_id=(
                        self.db.get(WorkingCopy, visible_working_copy_id).document_id
                        if visible_working_copy_id
                        else None
                    ),
                )
            )
        allowed_decisions = ["CONTINUE_UPLOAD", "CANCEL_UPLOAD"]
        if can_use_existing:
            allowed_decisions.insert(1, "USE_EXISTING_FILE")
        return DuplicateReviewResponse(
            id=review.id,
            upload_document_version_id=review.upload_document_version_id,
            document_id=version.document_id,
            filename=version.filename,
            status=review.status,
            decision=review.decision,
            expires_at=review.expires_at,
            candidates=response_candidates,
            allowed_decisions=allowed_decisions,
            duplicate_check_job_id=review.duplicate_check_job_id,
        )

    def _selected_document_id(self, review: UploadDuplicateReview) -> str | None:
        """把已选择工作副本转换为当前用户可使用的 Document ID。"""

        if not review.selected_existing_working_copy_id:
            return None
        working_copy = self.db.get(WorkingCopy, review.selected_existing_working_copy_id)
        return working_copy.document_id if working_copy else None

    def _validate_existing_candidate(self, *, review: UploadDuplicateReview, working_copy_id: str) -> WorkingCopy:
        """重新校验候选仍属于当前用户工作区且处于活动状态。"""

        candidate = (
            self.db.query(UploadDuplicateCandidate)
            .filter(
                UploadDuplicateCandidate.duplicate_review_id == review.id,
                UploadDuplicateCandidate.candidate_working_copy_id == working_copy_id,
                UploadDuplicateCandidate.match_scope.in_({"SAME_WORKSPACE", "SAME_USER"}),
            )
            .one_or_none()
        )
        if candidate is None or not self._candidate_accessible(review=review, candidate=candidate):
            raise HTTPException(status_code=403, detail="Existing working copy is not accessible")
        working_copy = self.db.get(WorkingCopy, working_copy_id)
        if working_copy is None or working_copy.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="Existing working copy is no longer active")
        return working_copy

    def _candidate_accessible(self, *, review: UploadDuplicateReview, candidate: UploadDuplicateCandidate) -> bool:
        """候选只有在同工作区且 Document 属于当前用户时才能被选择。"""

        working_copy = self.db.get(WorkingCopy, candidate.candidate_working_copy_id) if candidate.candidate_working_copy_id else None
        document = self.db.get(Document, working_copy.document_id) if working_copy else None
        return bool(
            working_copy
            and document
            and working_copy.workspace_id == review.workspace_id
            and document.user_id == review.user_id
            and working_copy.status == "ACTIVE"
        )

    def _enqueue_archive(self, *, review: UploadDuplicateReview, archive: UploadArchiveRecord) -> FilesystemJob:
        """为已允许归档的上传创建幂等归档任务。"""

        return FilesystemJobQueue(self.db).create_job(
            job_type="ARCHIVE_UPLOAD_TO_MANAGED_ROOT",
            queue_name="ARCHIVE",
            root_id=None,
            created_by=review.user_id,
            deduplication_key=f"upload-archive:{review.upload_document_version_id}",
            payload={
                "upload_document_version_id": review.upload_document_version_id,
                "user_id": review.user_id,
                "workspace_id": review.workspace_id,
            },
        )

    def _enqueue_cleanup(self, *, review: UploadDuplicateReview) -> FilesystemJob:
        """创建异步上传暂存清理任务，避免删除请求执行文件 I/O。"""

        return FilesystemJobQueue(self.db).create_job(
            job_type="CLEANUP_UPLOAD_TEMP",
            queue_name="FILE_OPERATION",
            root_id=None,
            created_by=review.user_id,
            deduplication_key=f"upload-cleanup:{review.upload_document_version_id}",
            payload={"upload_document_version_id": review.upload_document_version_id, "user_id": review.user_id},
        )

    def _append_audit(
        self,
        *,
        review: UploadDuplicateReview,
        change_type: str,
        summary: str,
        after_value: dict[str, Any],
    ) -> None:
        """为用户确认创建可追溯的系统 AgentRun、ToolInvocation 和 ChangeSet。"""

        create_lifecycle_audit(
            db=self.db,
            user_id=review.user_id,
            workspace_id=review.workspace_id,
            conversation_id=review.conversation_id,
            tool_name="upload-duplicate-decision-record",
            message_content=summary,
            change_type=change_type,
            target_type="upload_document_version",
            target_id=review.upload_document_version_id,
            target_document_id=self.db.get(DocumentVersion, review.upload_document_version_id).document_id,
            after_value=after_value,
        )


class FileLifecycleJobProcessor:
    """worker 侧文件生命周期处理器；API 和 AgentGraph 不得直接调用这些 I/O 方法。"""

    def __init__(self, db: Session, settings: Settings | None = None) -> None:
        """注入 worker 数据库会话与存储配置。"""

        self.db = db
        self.settings = settings or get_settings()
        self.repository = FileLifecycleRepository(db)
        self.storage = FileLifecycleStorageService(self.settings)

    def process(self, job: FilesystemJob) -> bool:
        """处理已知生命周期任务，返回是否由本处理器消费。"""

        handlers = {
            "CHECK_UPLOAD_DUPLICATES": self._check_upload_duplicates,
            "ARCHIVE_UPLOAD_TO_MANAGED_ROOT": self._archive_upload,
            "IMPORT_WORKING_COPIES": self._import_working_copy,
            "CLEANUP_UPLOAD_TEMP": self._cleanup_upload_temp,
            "RECONCILE_UPLOAD_ARCHIVES": self._reconcile_upload_archives,
            "RECONCILE_MANAGED_ROOT": self._reconcile_managed_root,
        }
        handler = handlers.get(job.job_type)
        if handler is None:
            return False
        handler(job)
        return True

    @staticmethod
    def supports(job_type: str) -> bool:
        """判断任务是否属于三层文件生命周期。"""

        return job_type in {
            "CHECK_UPLOAD_DUPLICATES",
            "ARCHIVE_UPLOAD_TO_MANAGED_ROOT",
            "IMPORT_WORKING_COPIES",
            "CLEANUP_UPLOAD_TEMP",
            "RECONCILE_UPLOAD_ARCHIVES",
            "RECONCILE_MANAGED_ROOT",
        }

    def record_failure(self, *, job: FilesystemJob, error_message: str, retrying: bool) -> None:
        """把 worker 失败同步到上传归档状态，运行日志不能替代业务状态。"""

        version_id = str((job.payload_json or {}).get("upload_document_version_id") or "")
        if not version_id:
            return
        archive = self.repository.get_archive_by_version(version_id)
        review = self.repository.get_review_by_version(version_id)
        if archive is not None and job.job_type in {"CHECK_UPLOAD_DUPLICATES", "ARCHIVE_UPLOAD_TO_MANAGED_ROOT"}:
            archive.status = "RETRY_WAIT" if retrying else "FAILED"
            archive.last_error_code = "FILESYSTEM_JOB_FAILED"
            archive.last_error_message = error_message
            archive.next_retry_at = (
                utcnow() + timedelta(seconds=self.settings.upload_archive_retry_interval_seconds)
                if retrying
                else None
            )
        if review is not None and job.job_type == "CHECK_UPLOAD_DUPLICATES":
            review.status = "CHECKING" if retrying else "FAILED"
        self.db.flush()

    def _reconcile_upload_archives(self, job: FilesystemJob) -> None:
        """补偿待查重、待归档和可重试失败上传，绝不越过待确认状态。"""

        records = (
            self.db.query(UploadArchiveRecord)
            .filter(
                UploadArchiveRecord.status.in_(
                    {"DUPLICATE_CHECK_PENDING", "PENDING", "RETRY_WAIT", "FAILED"}
                )
            )
            .order_by(UploadArchiveRecord.updated_at.asc())
            .all()
        )
        queued: list[str] = []
        for archive in records:
            review = self.repository.get_review_by_version(archive.upload_document_version_id)
            if review is None:
                continue
            if archive.status == "DUPLICATE_CHECK_PENDING":
                child = FilesystemJobQueue(self.db).create_job(
                    job_type="CHECK_UPLOAD_DUPLICATES",
                    queue_name="DUPLICATE_CHECK",
                    root_id=None,
                    created_by=review.user_id,
                    deduplication_key=f"upload-duplicate:{archive.upload_document_version_id}",
                    payload={
                        "upload_document_version_id": archive.upload_document_version_id,
                        "duplicate_review_id": review.id,
                        "user_id": review.user_id,
                        "workspace_id": review.workspace_id,
                    },
                )
            elif review.status == "WAITING_CONFIRMATION":
                # 防御性检查：状态表偶发不一致时以待用户确认作为更严格边界。
                continue
            elif review.decision != "CONTINUE_UPLOAD":
                # 取消上传或使用已有文件是终态；补偿任务不得把它重新送入归档。
                continue
            elif archive.next_retry_at and archive.next_retry_at > utcnow():
                continue
            else:
                archive.status = "PENDING"
                child = UploadLifecycleService(self.db, self.settings)._enqueue_archive(
                    review=review,
                    archive=archive,
                )
            archive.filesystem_job_id = child.id
            queued.append(child.id)
        FilesystemJobQueue(self.db).mark_completed(
            job=job,
            result={"records_checked": len(records), "queued_job_ids": queued},
        )

    def _reconcile_managed_root(self, job: FilesystemJob) -> None:
        """把全量同步转换为独立扫描任务，当前任务不直接遍历目录。"""

        if not job.root_id:
            raise RuntimeError("RECONCILE_MANAGED_ROOT 缺少 root_id")
        child = FilesystemJobQueue(self.db).create_job(
            job_type="SCAN_MANAGED_ROOT",
            queue_name="RECONCILE",
            root_id=job.root_id,
            created_by=job.created_by,
            deduplication_key=f"managed-root-scan:{job.id}",
            payload={"reconcile_job_id": job.id},
        )
        FilesystemJobQueue(self.db).mark_completed(job=job, result={"scan_job_id": child.id})

    def _check_upload_duplicates(self, job: FilesystemJob) -> None:
        """执行完整 SHA-256 查重，并在有候选时创建对话确认。"""

        version, document, archive, review = self._load_upload_context(job)
        if archive.status not in {"DUPLICATE_CHECK_PENDING", "DUPLICATE_CHECKING"}:
            FilesystemJobQueue(self.db).mark_completed(job=job, result={"status": archive.status, "idempotent": True})
            return
        archive.status = "DUPLICATE_CHECKING"
        review.status = "CHECKING"
        candidates = self.repository.replace_exact_candidates(
            review=review,
            upload_document_id=document.id,
            sha256=version.sha256,
            max_candidates=self.settings.upload_duplicate_max_candidates,
        )
        candidates.extend(self._append_near_duplicate_candidates(review=review, version=version, exact=candidates))
        if candidates:
            archive.status = "WAITING_DUPLICATE_CONFIRMATION"
            review.status = "WAITING_CONFIRMATION"
            self._create_duplicate_notification(review=review, version=version, candidates=candidates)
            result = {"status": archive.status, "duplicate_review_id": review.id, "candidate_count": len(candidates)}
        else:
            archive.status = "PENDING"
            archive_job = UploadLifecycleService(self.db, self.settings)._enqueue_archive(review=review, archive=archive)
            archive.filesystem_job_id = archive_job.id
            review.status = "RESOLVED"
            review.decision = "CONTINUE_UPLOAD"
            review.decided_at = utcnow()
            result = {"status": archive.status, "candidate_count": 0, "archive_job_id": archive_job.id}
        FilesystemJobQueue(self.db).mark_completed(job=job, result=result)

    def _archive_upload(self, job: FilesystemJob) -> None:
        """把已允许归档的上传暂存原子复制到不可变原始目录。"""

        version, document, archive, review = self._load_upload_context(job)
        if archive.status == "ARCHIVED" and archive.managed_file_id:
            FilesystemJobQueue(self.db).mark_completed(job=job, result={"managed_file_id": archive.managed_file_id, "idempotent": True})
            return
        if archive.status != "PENDING":
            raise RuntimeError(f"上传状态 {archive.status} 不允许归档")
        if not self.settings.upload_archive_enabled or not self.settings.managed_root_archive_enabled:
            raise RuntimeError("上传归档未启用")
        archive.status = "ARCHIVING"
        archive.attempt_count += 1
        upload_path = self.storage.upload_path(version.storage_path)
        risk_assessment = inspect_basic_file_risks(
            file_path=upload_path,
            filename=version.filename,
            content_type=version.content_type,
        )
        archive.risk_assessment_json = risk_assessment.to_dict()
        created_at = version.created_at.astimezone(timezone.utc)
        relative_path = "/".join(
            ["uploads", f"{created_at.year:04d}", f"{created_at.month:02d}", version.id, self.storage.sanitize_filename(version.filename)]
        )
        target = self.storage.archive_upload(
            source_storage_path=version.storage_path,
            archive_relative_path=relative_path,
            expected_sha256=version.sha256,
        )
        stat = target.stat()
        root = self.repository.get_or_create_archive_root(container_path=str(Path(self.settings.managed_root_archive_write_path).resolve()))
        managed_file = self.repository.create_archived_managed_file(
            root=root,
            version=version,
            relative_path=relative_path,
            relative_path_hash=hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
            file_identity=f"{stat.st_dev}:{stat.st_ino}",
        )
        archive.managed_root_id = root.id
        archive.managed_file_id = managed_file.id
        archive.archive_relative_path = relative_path
        archive.status = "ARCHIVED"
        archive.archived_at = utcnow()
        if risk_assessment.status == "NEEDS_REVIEW":
            archive.status = "NEEDS_REVIEW"
            document.ingest_status = "NEEDS_REVIEW"
            risk_pending_decision = {
                "type": "encrypted_file_review",
                "reason": "ENCRYPTED_FILE",
                "document_id": document.id,
                "filename": version.filename,
                "message": "文件已加密，系统不会尝试破解。请上传可读取版本后继续整理。",
                "allowed_decisions": ["UPLOAD_READABLE_COPY"],
            }
            risk_file_receipt = {
                "document_id": document.id,
                "document_version_id": version.id,
                "filename": version.filename,
                "organization_status": "NEEDS_REVIEW",
                "extraction_status": "SKIPPED",
                "page_count": 0,
                "char_count": 0,
                "categories": [],
                "warnings": list(risk_assessment.warnings),
                "errors": [],
                "managed_original_unchanged": True,
                "pending_decision": risk_pending_decision,
            }
            audit = create_lifecycle_audit(
                db=self.db,
                user_id=document.user_id,
                workspace_id=str(document.workspace_id),
                conversation_id=review.conversation_id,
                tool_name="basic-file-risk-check",
                message_content=f"文件“{version.filename}”已保护原件，但文件已加密，需要你提供可读取版本后再继续整理。",
                change_type="FILE_RISK_REVIEW_REQUIRED",
                target_type="managed_file",
                target_id=managed_file.id,
                target_document_id=document.id,
                after_value={
                    **risk_file_receipt,
                    "managed_file_id": managed_file.id,
                    "risk_assessment": risk_assessment.to_dict(),
                },
                graph_document_results=[risk_file_receipt],
            )
            archive.changeset_id = audit[0].id
            FilesystemJobQueue(self.db).mark_completed(
                job=job,
                result={
                    "managed_file_id": managed_file.id,
                    "status": "NEEDS_REVIEW",
                    "risk_assessment": risk_assessment.to_dict(),
                },
            )
            return
        audit = create_lifecycle_audit(
            db=self.db,
            user_id=document.user_id,
            workspace_id=str(document.workspace_id),
            conversation_id=review.conversation_id,
            tool_name="upload-archive",
            message_content=f"文件“{version.filename}”的原件已归档，正在创建工作副本。",
            change_type="ORIGINAL_FILE_ARCHIVED",
            target_type="managed_file",
            target_id=managed_file.id,
            target_document_id=document.id,
            after_value={
                "managed_file_id": managed_file.id,
                "source_type": "UPLOAD_ARCHIVE",
                "sha256": version.sha256,
                "risk_assessment": risk_assessment.to_dict(),
            },
        )
        archive.changeset_id = audit[0].id
        import_job = FilesystemJobQueue(self.db).create_job(
            job_type="IMPORT_WORKING_COPIES",
            queue_name="IMPORT",
            root_id=root.id,
            created_by=document.user_id,
            deduplication_key=f"working-copy-import:{document.workspace_id}:{managed_file.id}",
            payload={
                "managed_file_id": managed_file.id,
                "workspace_id": document.workspace_id,
                "user_id": document.user_id,
                "source_upload_document_id": document.id,
            },
        )
        archive.filesystem_job_id = import_job.id
        FilesystemJobQueue(self.db).mark_completed(
            job=job,
            result={"managed_file_id": managed_file.id, "import_job_id": import_job.id},
        )

    def _import_working_copy(self, job: FilesystemJob) -> None:
        """分析原始文件并只以最终名称和分类目录创建工作副本。

        内部临时文件不会形成 WorkingCopy 业务对象；正式文件提交后才写入活动工作副本。
        """

        payload = dict(job.payload_json or {})
        managed_file = self.db.get(ManagedFile, str(payload.get("managed_file_id") or ""))
        workspace_id = str(payload.get("workspace_id") or "")
        user_id = str(payload.get("user_id") or job.created_by or "")
        if managed_file is None or managed_file.status != "ACTIVE" or not workspace_id or not user_id:
            raise RuntimeError("IMPORT_WORKING_COPIES 缺少有效原始文件、工作区或用户")
        managed_root = self.db.get(ManagedRoot, managed_file.root_id)
        if managed_root is None:
            raise RuntimeError("原始文件目录不存在")
        working_root = self.repository.get_or_create_working_root(workspace_id=workspace_id, managed_root=managed_root)
        existing = self.repository.find_primary_working_copy(
            working_root_id=working_root.id,
            managed_file_id=managed_file.id,
        )
        if existing is not None:
            FilesystemJobQueue(self.db).mark_completed(job=job, result={"working_copy_id": existing.id, "idempotent": True})
            return
        source = resolve_managed_relative_path(
            root_path=Path(managed_root.container_path),
            relative_path=managed_file.relative_path,
        )
        source_stat_before = source.stat()
        source_sha256 = managed_file.content_sha256 or self.storage.sha256_file(source)
        # 内部暂存路径只承载任务唯一性；完整 UUID 和用户文件名会放大 Windows 路径长度，
        # 因而必须由 StorageService 生成固定上界的私有名称。
        staged_relative_path = self.storage.internal_staging_relative_path(
            working_root_relative_path=working_root.relative_storage_path,
            job_id=job.id,
            managed_file_id=managed_file.id,
            filename=managed_file.filename,
        )
        final_storage_relative_path = ""
        final_target: Path | None = None
        final_target_created = False
        try:
            self.storage.import_working_copy(
                source=source,
                relative_path=staged_relative_path,
                expected_sha256=source_sha256,
            )
            source_stat_after = source.stat()
            if (source_stat_before.st_size, source_stat_before.st_mtime_ns) != (
                source_stat_after.st_size,
                source_stat_after.st_mtime_ns,
            ):
                raise RuntimeError("原始文件在导入期间发生变化")

            document = Document(
                user_id=user_id,
                workspace_id=workspace_id,
                original_filename=managed_file.filename,
                content_type=_guess_content_type(managed_file.extension),
                size_bytes=managed_file.size_bytes,
                sha256=source_sha256,
                status="WORKING_COPY",
                ingest_status="INGESTING",
            )
            self.db.add(document)
            self.db.flush()
            file_object = FileObject(
                document_id=document.id,
                storage_backend="working_copy_local",
                storage_path=staged_relative_path,
                size_bytes=managed_file.size_bytes,
                sha256=source_sha256,
            )
            self.db.add(file_object)
            version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                working_copy_id=None,
                storage_tier="WORKING_COPY",
                storage_path=staged_relative_path,
                filename=managed_file.filename,
                content_type=document.content_type,
                size_bytes=managed_file.size_bytes,
                sha256=source_sha256,
                source_type="IMPORT",
                source_managed_file_id=managed_file.id,
                created_by=user_id,
            )
            self.db.add(version)
            self.db.flush()

            decision = InitialWorkingCopyOrganizer(
                db=self.db,
                user_id=user_id,
                settings=self.settings,
            ).decide(document=document, version=version, managed_file=managed_file)
            extraction_run_id = str((decision.extraction_result or {}).get("extraction_run_id") or "")
            index_result = (
                DocumentIndexService(db=self.db, settings=self.settings).build(
                    document_id=document.id,
                    document_version_id=version.id,
                    extraction_run_id=extraction_run_id,
                )
                if extraction_run_id
                else {
                    "ok": False,
                    "status": "FAILED",
                    "chunk_count": 0,
                    "evidence_count": 0,
                    "embedding_status": "DISABLED",
                    "error": {"code": "EXTRACTION_NOT_READY", "message": "正文解析未完成，检索索引尚未建立。"},
                }
            )
            path_resolution = self._working_path_resolution(
                working_root=working_root,
                managed_file=managed_file,
                preferred_relative_path=decision.relative_path,
            )
            relative_path = path_resolution.relative_path
            decision.filename = path_resolution.filename
            decision.relative_path = relative_path
            if path_resolution.conflict is not None:
                decision.rename_status = "NEEDS_REVIEW"
            final_storage_relative_path = f"{working_root.relative_storage_path}/{relative_path}"
            final_target, final_target_created = self.storage.publish_working_copy(
                staged_relative_path=staged_relative_path,
                target_relative_path=final_storage_relative_path,
                expected_sha256=source_sha256,
            )

            document.original_filename = decision.filename
            document.ingest_status = "INGESTED"
            version.storage_path = final_storage_relative_path
            version.filename = decision.filename
            file_object.storage_path = final_storage_relative_path
            working_copy = WorkingCopy(
                working_copy_root_id=working_root.id,
                workspace_id=workspace_id,
                managed_file_id=managed_file.id,
                document_id=document.id,
                relative_path=relative_path,
                relative_path_hash=hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                filename=decision.filename,
                extension=Path(decision.filename).suffix.lower(),
                size_bytes=managed_file.size_bytes,
                content_sha256=source_sha256,
                imported_source_sha256=source_sha256,
                is_primary_import=True,
                status="ACTIVE",
                sync_status="SYNCED",
            )
            self.db.add(working_copy)
            self.db.flush()
            version.working_copy_id = working_copy.id
            working_copy.current_version_id = version.id
            working_root.status = "READY"
            working_root.last_imported_at = utcnow()

            category_name = (
                "/".join(str(item) for item in decision.primary_category.get("category_path", []))
                if decision.primary_category is not None
                else "待整理"
            )
            pending_decision = self._initial_organization_pending_decision(
                decision=decision,
                working_copy=working_copy,
                path_resolution=path_resolution,
            )
            extraction_pages = list((decision.extraction_result or {}).get("pages") or [])
            file_receipt = {
                **decision.document_result(
                    document_id=document.id,
                    document_version_id=version.id,
                ),
                "document_version_id": version.id,
                "working_copy_id": working_copy.id,
                "filename": decision.filename,
                "page_count": len(extraction_pages),
                "char_count": sum(int(item.get("char_count") or 0) for item in extraction_pages),
                "organization_status": "NEEDS_REVIEW" if pending_decision or not index_result.get("ok") else "READY",
                "search_status": "READY" if index_result.get("ok") else "NEEDS_REVIEW",
                "evidence_count": int(index_result.get("evidence_count") or 0),
                "year": decision.rename_metadata.get("year"),
                "document_type": decision.summary_metadata.get("document_type"),
                "keywords": list(decision.summary_metadata.get("keywords") or []),
                "entities": list(decision.summary_metadata.get("entities") or []),
                "managed_original_unchanged": True,
                "risk_warnings": self._risk_warnings_for_managed_file(managed_file),
                "pending_decision": pending_decision,
            }
            message_content = self._initial_organization_message(
                filename=decision.filename,
                category_name=category_name,
                pending_decision=pending_decision,
            )
            changeset, _ = create_lifecycle_audit(
                db=self.db,
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=self._conversation_for_upload(managed_file),
                tool_name="working-copy-initial-organize",
                message_content=message_content,
                change_type="WORKING_COPY_IMPORTED",
                target_type="working_copy",
                target_id=working_copy.id,
                target_document_id=document.id,
                after_value={
                    "working_copy_id": working_copy.id,
                    "managed_file_id": managed_file.id,
                    "relative_path": relative_path,
                    "document_version_id": version.id,
                    "document_summary_id": decision.document_summary_id,
                    "classification_summary_id": decision.classification_summary_id,
                    "document_index_run_id": index_result.get("index_run_id"),
                    "search_status": file_receipt["search_status"],
                    "chunk_count": int(index_result.get("chunk_count") or 0),
                    "evidence_count": int(index_result.get("evidence_count") or 0),
                    "embedding_status": index_result.get("embedding_status") or "DISABLED",
                    "primary_category": category_name,
                    "rename_status": decision.rename_status,
                    "organization_status": file_receipt["organization_status"],
                    "categories": decision.categories,
                    "year": decision.rename_metadata.get("year"),
                    "document_type": decision.summary_metadata.get("document_type"),
                    "keywords": list(decision.summary_metadata.get("keywords") or []),
                    "entities": list(decision.summary_metadata.get("entities") or []),
                    "pending_decision": pending_decision,
                    "managed_original_unchanged": True,
                },
                graph_document_results=[file_receipt],
            )
            index_error = index_result.get("error") if isinstance(index_result.get("error"), dict) else {}
            # 即使预校验失败且尚未创建 index_run，也必须以 DocumentVersion 为目标留下失败审计。
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="document_index_run" if index_result.get("index_run_id") else "document_version",
                    target_id=str(index_result.get("index_run_id") or version.id),
                    target_document_id=document.id,
                    change_type=(
                        "DOCUMENT_INDEX_CREATED" if index_result.get("ok") else "DOCUMENT_INDEX_FAILED"
                    ),
                    before_value_json={},
                    after_value_json={
                        "document_version_id": version.id,
                        "chunk_count": int(index_result.get("chunk_count") or 0),
                        "evidence_count": int(index_result.get("evidence_count") or 0),
                        "embedding_status": index_result.get("embedding_status") or "DISABLED",
                        # 审计只保存安全错误码，不能把内部异常消息或正文写入 ChangeSet。
                        "error_code": index_error.get("code"),
                    },
                    source="document-index-service",
                    confidence=1.0 if index_result.get("ok") else 0.0,
                    evidence_json={},
                    execution_status="COMPLETED" if index_result.get("ok") else "FAILED",
                )
            )
            if pending_decision:
                self.db.add(
                    FileRenameReviewItem(
                        conversation_id=changeset.conversation_id,
                        agent_run_id=changeset.agent_run_id,
                        user_id=user_id,
                        managed_file_id=managed_file.id,
                        document_id=document.id,
                        root_key=managed_root.root_key,
                        original_relative_path=managed_file.relative_path,
                        original_filename=managed_file.filename,
                        source_sha256=source_sha256,
                        status="NEEDS_REVIEW",
                        review_context_json=pending_decision,
                        decision_json={},
                    )
                )
            persist_document_results_classifications(
                db=self.db,
                agent_run_id=changeset.agent_run_id,
                document_results=[
                    decision.document_result(
                        document_id=document.id,
                        document_version_id=version.id,
                    )
                ],
            )
            if decision.document_summary_id:
                self.db.add(
                    ChangeItem(
                        changeset_id=changeset.id,
                        target_type="document_summary",
                        target_id=decision.document_summary_id,
                        target_document_id=document.id,
                        change_type="DOCUMENT_SUMMARY_CREATED",
                        before_value_json={},
                        after_value_json={"document_summary_id": decision.document_summary_id},
                        source="working-copy-initial-organize",
                        confidence=1.0,
                        evidence_json={},
                        execution_status="COMPLETED",
                    )
                )
            if decision.classification_summary_id:
                self.db.add(
                    ChangeItem(
                        changeset_id=changeset.id,
                        target_type="classification_summary",
                        target_id=decision.classification_summary_id,
                        target_document_id=document.id,
                        change_type="CLASSIFICATION_SUMMARY_CREATED",
                        before_value_json={},
                        after_value_json={
                            "classification_summary_id": decision.classification_summary_id,
                            "primary_category": category_name,
                        },
                        source="working-copy-initial-organize",
                        confidence=(
                            float(decision.primary_category.get("confidence") or 0)
                            if decision.primary_category is not None
                            else None
                        ),
                        evidence_json=(
                            {"items": list(decision.primary_category.get("evidence_items") or [])}
                            if decision.primary_category is not None
                            else {}
                        ),
                        execution_status="COMPLETED",
                    )
                )
            self.db.add(
                WorkingCopyPathRecord(
                    working_copy_id=working_copy.id,
                    sequence_number=1,
                    operation_type="INITIAL_IMPORT",
                    before_relative_path=managed_file.relative_path,
                    after_relative_path=relative_path,
                    before_filename=managed_file.filename,
                    after_filename=decision.filename,
                    document_version_id=version.id,
                    content_sha256=source_sha256,
                    agent_run_id=changeset.agent_run_id,
                    changeset_id=changeset.id,
                    status="COMPLETED",
                    executed_by=user_id,
                )
            )
            FilesystemJobQueue(self.db).mark_completed(
                job=job,
                result={
                    "working_copy_id": working_copy.id,
                    "document_id": document.id,
                    "document_version_id": version.id,
                    "filename": decision.filename,
                    "relative_path": relative_path,
                    "primary_category": category_name,
                },
            )
        except Exception:
            # 数据库事务失败时清理本任务创建的隐藏临时文件或未登记最终文件，避免孤儿文件。
            self.storage.working_copy_path(staged_relative_path).unlink(missing_ok=True)
            if final_target is not None and final_storage_relative_path and final_target_created:
                final_target.unlink(missing_ok=True)
            raise

    def _cleanup_upload_temp(self, job: FilesystemJob) -> None:
        """异步清理已取消或已经使用已有文件的上传暂存。"""

        version_id = str((job.payload_json or {}).get("upload_document_version_id") or "")
        version = self.repository.get_upload_version(version_id)
        archive = self.repository.get_archive_by_version(version_id)
        if version is None or archive is None:
            FilesystemJobQueue(self.db).mark_completed(job=job, result={"cleaned": False, "reason": "not_found"})
            return
        if archive.status not in {"CANCELLED", "EXISTING_FILE_SELECTED", "ARCHIVED"}:
            raise RuntimeError("当前上传状态不允许清理暂存")
        self.storage.upload_path(version.storage_path).unlink(missing_ok=True)
        FilesystemJobQueue(self.db).mark_completed(job=job, result={"cleaned": True})

    def _load_upload_context(
        self,
        job: FilesystemJob,
    ) -> tuple[DocumentVersion, Document, UploadArchiveRecord, UploadDuplicateReview]:
        """从任务 payload 解析确定上传对象，禁止猜测附件范围。"""

        version_id = str((job.payload_json or {}).get("upload_document_version_id") or "")
        version = self.repository.get_upload_version(version_id)
        document = self.db.get(Document, version.document_id) if version else None
        archive = (
            self.db.query(UploadArchiveRecord)
            .filter(UploadArchiveRecord.upload_document_version_id == version_id)
            .with_for_update()
            .one_or_none()
            if version
            else None
        )
        review = (
            self.db.query(UploadDuplicateReview)
            .filter(UploadDuplicateReview.upload_document_version_id == version_id)
            .with_for_update()
            .one_or_none()
            if version
            else None
        )
        if version is None or document is None or archive is None or review is None:
            raise RuntimeError("上传生命周期任务引用不存在的业务对象")
        return version, document, archive, review

    def _append_near_duplicate_candidates(
        self,
        *,
        review: UploadDuplicateReview,
        version: DocumentVersion,
        exact: list[UploadDuplicateCandidate],
    ) -> list[UploadDuplicateCandidate]:
        """对可安全读取的小型文本使用本地 token Jaccard 生成近似候选。"""

        if len(exact) >= self.settings.upload_duplicate_max_candidates:
            return []
        source_tokens = _text_tokens(self.storage.upload_path(version.storage_path), version.filename)
        if not source_tokens:
            return []
        exact_managed_ids = {item.candidate_managed_file_id for item in exact}
        rows = (
            self.db.query(WorkingCopy, WorkingCopyRoot, ManagedFile, Document)
            .join(WorkingCopyRoot, WorkingCopy.working_copy_root_id == WorkingCopyRoot.id)
            .join(ManagedFile, WorkingCopy.managed_file_id == ManagedFile.id)
            .join(Document, WorkingCopy.document_id == Document.id)
            .filter(WorkingCopy.status == "ACTIVE", ManagedFile.status == "ACTIVE")
            .filter(~ManagedFile.id.in_(exact_managed_ids) if exact_managed_ids else ManagedFile.id != "")
            .order_by(WorkingCopy.updated_at.desc())
            .limit(100)
            .all()
        )
        scored: list[tuple[float, WorkingCopy, ManagedFile, Document]] = []
        for working_copy, working_root, managed_file, document in rows:
            candidate_path = self.storage.working_copy_path(
                f"{working_root.relative_storage_path}/{working_copy.relative_path}"
            )
            candidate_tokens = _text_tokens(candidate_path, working_copy.filename)
            if not candidate_tokens:
                continue
            score = len(source_tokens & candidate_tokens) / max(1, len(source_tokens | candidate_tokens))
            if score >= self.settings.upload_duplicate_similarity_threshold:
                scored.append((score, working_copy, managed_file, document))
        scored.sort(key=lambda item: item[0], reverse=True)
        candidates: list[UploadDuplicateCandidate] = []
        remaining = self.settings.upload_duplicate_max_candidates - len(exact)
        for offset, (score, working_copy, managed_file, candidate_document) in enumerate(scored[:remaining], start=1):
            scope = self.repository._candidate_scope(
                review=review,
                working_copy=working_copy,
                candidate_document=candidate_document,
            )
            accessible = working_copy.workspace_id == review.workspace_id and candidate_document.user_id == review.user_id
            summary = (
                {"message": "系统检测到高度相似内容", "similarity_bucket": _similarity_bucket(score)}
                if scope == "CROSS_USER"
                else {
                    "message": "检测到当前账号可访问的高度相似文件",
                    "filename": working_copy.filename,
                    "relative_path": working_copy.relative_path if accessible else None,
                    "similarity_bucket": _similarity_bucket(score),
                }
            )
            candidate = UploadDuplicateCandidate(
                duplicate_review_id=review.id,
                candidate_managed_file_id=managed_file.id,
                candidate_working_copy_id=working_copy.id,
                match_type="NEAR_DUPLICATE",
                match_scope=scope,
                similarity_score=score,
                match_evidence_json={"method": "local_token_jaccard_v1"},
                user_visible_summary_json=summary,
                rank=len(exact) + offset,
            )
            self.db.add(candidate)
            candidates.append(candidate)
        self.db.flush()
        return candidates

    def _create_duplicate_notification(
        self,
        *,
        review: UploadDuplicateReview,
        version: DocumentVersion,
        candidates: list[UploadDuplicateCandidate],
    ) -> None:
        """在原会话创建脱敏 Agent 消息和审计 ChangeSet。"""

        cross_user_only = all(item.match_scope == "CROSS_USER" for item in candidates)
        content = (
            f"系统检测到“{version.filename}”存在相同或高度相似内容。请选择继续上传或取消上传。"
            if cross_user_only
            else f"检测到“{version.filename}”已有相同或高度相似文件。请选择继续上传、使用已有文件或取消上传。"
        )
        changeset, message = create_lifecycle_audit(
            db=self.db,
            user_id=review.user_id,
            workspace_id=review.workspace_id,
            conversation_id=review.conversation_id,
            tool_name="upload-duplicate-check",
            message_content=content,
            change_type="UPLOAD_DUPLICATE_REVIEW_CREATED",
            target_type="upload_document_version",
            target_id=review.upload_document_version_id,
            target_document_id=version.document_id,
            after_value={
                "duplicate_review_id": review.id,
                "candidate_count": len(candidates),
                "has_cross_user_candidate": any(item.match_scope == "CROSS_USER" for item in candidates),
            },
            attachment_metadata={
                "duplicate_review_id": review.id,
                "upload_document_version_id": review.upload_document_version_id,
                "type": "duplicate_upload_review",
            },
        )
        review.notification_message_id = message.id
        archive = self.repository.get_archive_by_version(review.upload_document_version_id)
        if archive:
            archive.changeset_id = changeset.id

    def _working_path_resolution(
        self,
        *,
        working_root: WorkingCopyRoot,
        managed_file: ManagedFile,
        preferred_relative_path: str | None = None,
    ) -> InitialWorkingPathResolution:
        """解析首次目标；不同内容冲突必须进入对话待确认，不能自动追加版本。"""

        candidate = preferred_relative_path or managed_file.relative_path
        path_hash = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        conflict = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.working_copy_root_id == working_root.id,
                WorkingCopy.relative_path_hash == path_hash,
                WorkingCopy.status == "ACTIVE",
            )
            .first()
        )
        storage_candidate = self.storage.working_copy_path(
            f"{working_root.relative_storage_path}/{candidate}"
        )
        if conflict is None:
            if not storage_candidate.exists():
                return InitialWorkingPathResolution(
                    relative_path=candidate,
                    filename=Path(candidate).name,
                )
            if (
                managed_file.content_sha256
                and self.storage.sha256_file(storage_candidate) == managed_file.content_sha256
            ):
                # 上一次尝试可能已提交文件但数据库事务未完成，继续采用同一路径幂等收敛。
                return InitialWorkingPathResolution(
                    relative_path=candidate,
                    filename=Path(candidate).name,
                )
        pending_filename = self.storage.sanitize_filename(managed_file.filename)
        pending_path = f"待确认/{managed_file.id}/{pending_filename}"
        return InitialWorkingPathResolution(
            relative_path=pending_path,
            filename=pending_filename,
            conflict={
                "type": "filename_conflict",
                "reason": "FILENAME_CONFLICT",
                "target_filename": Path(candidate).name,
                "existing_working_copy_ids": [conflict.id] if conflict is not None else [],
                "existing_filenames": [conflict.filename] if conflict is not None else [Path(candidate).name],
                "message": "整理后的目标文件名已存在，请确认是否同时保留两个文件。",
                "allowed_decisions": [
                    "KEEP_BOTH",
                    "KEEP_EXISTING",
                    "REPLACE_EXISTING_WORKING_COPY",
                    "DELETE_EXISTING_WORKING_COPY",
                ],
            },
        )

    @staticmethod
    def _initial_organization_pending_decision(
        *,
        decision: InitialOrganizationDecision,
        working_copy: WorkingCopy,
        path_resolution: InitialWorkingPathResolution,
    ) -> dict[str, Any] | None:
        """把低置信度或同名冲突转换为普通用户可以理解的待决策项。"""

        if path_resolution.conflict is not None:
            return {
                **path_resolution.conflict,
                "working_copy_id": working_copy.id,
                "filename": working_copy.filename,
            }
        if decision.rename_status not in {"READY", "NO_CHANGE"}:
            return {
                "type": "rename_review",
                "reason": "LOW_CONFIDENCE_RENAME",
                "working_copy_id": working_copy.id,
                "filename": working_copy.filename,
                "proposed_filename": decision.rename_metadata.get("proposed_filename"),
                "message": "命名依据不足，已保留上传时的文件名，请通过对话确认或更正。",
                "allowed_decisions": ["CONFIRM_CURRENT_NAME", "PROVIDE_NEW_NAME"],
            }
        return None

    @staticmethod
    def _initial_organization_message(
        *,
        filename: str,
        category_name: str,
        pending_decision: dict[str, Any] | None,
    ) -> str:
        """生成不包含 Skill、Tool 或服务器路径的首次整理消息。"""

        if pending_decision and pending_decision.get("type") == "filename_conflict":
            return (
                f"文件已读取并分类，当前保留为“{filename}”。整理后的目标名称已存在，"
                "请确认是否需要同时保留两个文件；如同时保留，确认后再分配版本后缀。"
            )
        if pending_decision:
            return f"文件已读取并分类，当前保留为“{filename}”。命名依据不足，请确认或告诉我新的文件名。"
        return f"已整理文件：{filename}\n分类：{category_name}"

    def _conversation_for_upload(self, managed_file: ManagedFile) -> str | None:
        """从上传归档关系恢复原会话，部署文件没有会话时返回 None。"""

        if not managed_file.source_upload_version_id:
            return None
        review = self.repository.get_review_by_version(managed_file.source_upload_version_id)
        return review.conversation_id if review else None

    def _risk_warnings_for_managed_file(self, managed_file: ManagedFile) -> list[dict[str, str]]:
        """读取上传归档的基础风险警告，绝不把它解释成病毒扫描结果。"""

        if not managed_file.source_upload_version_id:
            return []
        archive = self.repository.get_archive_by_version(managed_file.source_upload_version_id)
        if archive is None:
            return []
        assessment = dict(archive.risk_assessment_json or {})
        return [dict(item) for item in assessment.get("warnings", []) if isinstance(item, dict)]


class WorkingCopyQueryService:
    """工作副本只读查询服务，所有查询都强制限制当前默认工作区。"""

    def __init__(self, db: Session) -> None:
        """注入数据库会话。"""

        self.db = db
        self.repository = FileLifecycleRepository(db)

    def list(self, current_user: User) -> list[WorkingCopyResponse]:
        """列出当前工作区工作副本。"""

        if not current_user.default_workspace_id:
            return []
        return [self._to_response(copy, root) for copy, root in self.repository.list_owned_working_copies(workspace_id=current_user.default_workspace_id)]

    def get(self, *, working_copy_id: str, current_user: User) -> WorkingCopyResponse:
        """读取当前工作区工作副本元数据。"""

        copy = self._owned(working_copy_id=working_copy_id, current_user=current_user)
        root = self.db.get(WorkingCopyRoot, copy.working_copy_root_id)
        return self._to_response(copy, root)

    def lineage(self, *, working_copy_id: str, current_user: User) -> WorkingCopyLineageResponse:
        """返回工作副本、原始文件与导入哈希的关系。"""

        copy = self._owned(working_copy_id=working_copy_id, current_user=current_user)
        root = self.db.get(WorkingCopyRoot, copy.working_copy_root_id)
        managed_file = self.db.get(ManagedFile, copy.managed_file_id)
        managed_root = self.db.get(ManagedRoot, managed_file.root_id) if managed_file else None
        if root is None or managed_file is None or managed_root is None:
            raise HTTPException(status_code=409, detail="Working copy lineage is incomplete")
        return WorkingCopyLineageResponse(
            working_copy=self._to_response(copy, root),
            managed_root_key=managed_root.root_key,
            managed_file_relative_path=managed_file.relative_path,
            managed_file_source_type=managed_file.source_type,
            managed_file_status=managed_file.status,
            imported_source_sha256=copy.imported_source_sha256,
        )

    def versions(self, *, working_copy_id: str, current_user: User) -> list[DocumentVersionResponse]:
        """读取当前工作副本文档版本。"""

        copy = self._owned(working_copy_id=working_copy_id, current_user=current_user)
        return [
            DocumentVersionResponse(
                id=item.id,
                version_number=item.version_number,
                filename=item.filename,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                source_type=item.source_type,
                created_at=item.created_at,
            )
            for item in self.repository.list_versions(copy.document_id)
        ]

    def path_records(self, *, working_copy_id: str, current_user: User) -> list[WorkingCopyPathRecordResponse]:
        """读取工作副本路径历史。"""

        copy = self._owned(working_copy_id=working_copy_id, current_user=current_user)
        return [WorkingCopyPathRecordResponse(**{
            "id": item.id,
            "sequence_number": item.sequence_number,
            "operation_type": item.operation_type,
            "before_relative_path": item.before_relative_path,
            "after_relative_path": item.after_relative_path,
            "before_filename": item.before_filename,
            "after_filename": item.after_filename,
            "document_version_id": item.document_version_id,
            "content_sha256": item.content_sha256,
            "status": item.status,
            "error_code": item.error_code,
            "error_message": item.error_message,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }) for item in self.repository.list_path_records(copy.id)]

    def trash_entries(self, *, current_user: User) -> list[TrashEntryResponse]:
        """列出当前默认工作区的回收站条目，不返回物理回收站路径。"""

        if not current_user.default_workspace_id:
            return []
        entries = (
            self.db.query(TrashEntry)
            .filter(TrashEntry.workspace_id == current_user.default_workspace_id)
            .order_by(TrashEntry.deleted_at.desc())
            .all()
        )
        return [
            TrashEntryResponse(
                id=entry.id,
                working_copy_id=entry.working_copy_id,
                document_version_id=entry.document_version_id,
                entry_type=entry.entry_type,
                original_relative_path=entry.original_relative_path,
                status=entry.status,
                deleted_at=entry.deleted_at,
                retention_until=entry.retention_until,
                restored_at=entry.restored_at,
            )
            for entry in entries
        ]

    def download_path(self, *, working_copy_id: str, current_user: User) -> tuple[Path, str, str]:
        """解析工作副本下载路径；只使用版本中由后端生成的相对路径。"""

        copy = self._owned(working_copy_id=working_copy_id, current_user=current_user)
        version = self.db.get(DocumentVersion, copy.current_version_id) if copy.current_version_id else None
        document = self.db.get(Document, copy.document_id)
        if version is None or document is None:
            raise HTTPException(status_code=404, detail="Working copy content not found")
        path = FileLifecycleStorageService().working_copy_path(version.storage_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Working copy content not found")
        return path, copy.filename, document.content_type

    def _owned(self, *, working_copy_id: str, current_user: User) -> WorkingCopy:
        """校验工作副本属于当前默认工作区。"""

        if not current_user.default_workspace_id:
            raise HTTPException(status_code=404, detail="Working copy not found")
        copy = self.repository.get_owned_working_copy(
            working_copy_id=working_copy_id,
            workspace_id=current_user.default_workspace_id,
        )
        if copy is None:
            raise HTTPException(status_code=404, detail="Working copy not found")
        return copy

    @staticmethod
    def _to_response(copy: WorkingCopy, root: WorkingCopyRoot) -> WorkingCopyResponse:
        """转换安全响应。"""

        return WorkingCopyResponse(
            id=copy.id,
            workspace_id=copy.workspace_id,
            managed_file_id=copy.managed_file_id,
            document_id=copy.document_id,
            current_version_id=copy.current_version_id,
            root_key=root.root_key,
            relative_path=copy.relative_path,
            filename=copy.filename,
            extension=copy.extension,
            size_bytes=copy.size_bytes,
            content_sha256=copy.content_sha256,
            status=copy.status,
            sync_status=copy.sync_status,
            created_at=copy.created_at,
            updated_at=copy.updated_at,
        )


def create_lifecycle_audit(
    *,
    db: Session,
    user_id: str,
    workspace_id: str,
    conversation_id: str | None,
    tool_name: str,
    message_content: str,
    change_type: str,
    target_type: str,
    target_id: str | None,
    target_document_id: str | None,
    after_value: dict[str, Any],
    before_value: dict[str, Any] | None = None,
    execution_status: str = "COMPLETED",
    attachment_metadata: dict[str, Any] | None = None,
    graph_document_results: list[dict[str, Any]] | None = None,
) -> tuple[ChangeSet, Message]:
    """创建系统生命周期调用的 AgentRun、ToolInvocation、ChangeSet 和逐文件 ChangeItem。"""

    safe_conversation_id = conversation_id or f"lifecycle-{user_id.replace('-', '')[:26]}"
    conversation = db.get(Conversation, safe_conversation_id)
    if conversation is None:
        conversation = Conversation(
            id=safe_conversation_id,
            user_id=user_id,
            workspace_id=workspace_id,
            title="文件生命周期通知",
        )
        db.add(conversation)
        db.flush()
    elif conversation.user_id != user_id:
        raise RuntimeError("生命周期审计会话不属于当前用户")
    message = Message(
        conversation_id=conversation.id,
        user_id=user_id,
        role="assistant",
        content=message_content,
        attachments_json=[attachment_metadata] if attachment_metadata else [],
    )
    db.add(message)
    db.flush()
    run = AgentRun(
        conversation_id=conversation.id,
        message_id=message.id,
        user_id=user_id,
        intent="SYSTEM_FILE_LIFECYCLE",
        status=execution_status,
        selected_skills_json=["change-report"],
        plan_json={"system_lifecycle": True, "tool_name": tool_name},
        graph_state_json={
            "status": "COMPLETED",
            "final_response": message_content,
            "document_results": graph_document_results or [],
        },
        final_response=message_content,
    )
    db.add(run)
    db.flush()
    invocation = ToolInvocation(
        agent_run_id=run.id,
        tool_name=tool_name,
        input_json={"target_type": target_type, "target_id": target_id},
        output_json={"status": execution_status, **after_value},
        status=execution_status,
        finished_at=utcnow(),
    )
    db.add(invocation)
    db.flush()
    changeset = ChangeSet(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        agent_run_id=run.id,
        user_id=user_id,
        status="COMPLETED" if execution_status == "COMPLETED" else "PARTIAL",
        summary=message_content,
    )
    db.add(changeset)
    db.flush()
    item = ChangeItem(
        changeset_id=changeset.id,
        target_type=target_type,
        target_id=target_id,
        target_document_id=target_document_id,
        change_type=change_type,
        before_value_json=before_value or {},
        after_value_json=after_value,
        source=tool_name,
        confidence=1.0,
        evidence_json={},
        execution_status=execution_status,
    )
    db.add(item)
    run.changeset_id = changeset.id
    invocation.changeset_id = changeset.id
    db.flush()
    return changeset, message


def _text_tokens(path: Path, filename: str) -> set[str]:
    """只对小型纯文本类文件生成本地 token 集合，其他格式安全降级。"""

    if Path(filename).suffix.lower() not in {".txt", ".md", ".csv", ".tsv"}:
        return set()
    if not path.is_file() or path.stat().st_size > 5 * 1024 * 1024:
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    return {token for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", text) if len(token) <= 80}


def _similarity_bucket(score: float) -> str:
    """把精确分数收敛为脱敏区间，避免跨用户暴露过多推断信息。"""

    lower = int(score * 20) * 5
    return f"{lower}-{min(100, lower + 5)}%"


def _guess_content_type(extension: str) -> str:
    """为导入工作副本提供稳定 MIME；未知类型保持二进制。"""

    return {
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".csv": "text/csv",
        ".pdf": "application/pdf",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(extension.lower(), "application/octet-stream")
