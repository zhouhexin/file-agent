"""三层文件生命周期业务服务。

HTTP 请求只创建状态和持久化任务；查重、归档、后台物化、导入和清理均由 worker
调用本模块的处理方法，API 与 Agent 不得直接执行文件 I/O。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    Conversation,
    Document,
    DocumentCategory,
    DocumentExtractionRun,
    DocumentIndexRun,
    DocumentOrganizationDecision,
    DocumentSearchProfile,
    DocumentVersion,
    FileObject,
    FileRenameReviewItem,
    FilesystemJob,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
    Message,
    RelevantFileSetItem,
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
from app.modules.file_lifecycle.layout_repair import WorkingCopyLayoutRepairService
from app.modules.file_lifecycle.organizer import (
    InitialOrganizationDecision,
    InitialWorkingCopyOrganizer,
    rename_metadata_for_initial_organization,
)
from app.modules.file_rename.filename_builder import replace_year_prefix_with_date
from app.modules.file_lifecycle.schemas import (
    ArchiveStatusResponse,
    DocumentVersionResponse,
    DuplicateCandidateResponse,
    DuplicateDecisionRequest,
    DuplicateDecisionResponse,
    DuplicateReviewResponse,
    UploadProcessingStartResponse,
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
from app.modules.classification.auto_placement_policy import (
    AutoPlacementPolicy,
    AutoPlacementPolicyResult,
)
from app.modules.classification.evidence_reader import CurrentClassificationEvidenceReader
from app.modules.classification.freshness import (
    ClassificationFreshness,
    classification_refresh_deduplication_key,
    classification_refresh_priority,
    current_classification_identity,
    inspect_managed_source_classification,
)
from app.modules.classification.organization_path import (
    CategoryOrganizationPathError,
    CategoryOrganizationPathResolver,
)
from app.modules.classification.organization_repository import OrganizationDecisionRepository
from app.modules.classification.image_date_policy import (
    IMAGE_DATE_CATEGORY_ROOT_ID,
    IMAGE_DATE_CLASSIFIER_VERSION,
    IMAGE_DATE_RELATION_SOURCE,
    MANAGED_SOURCE_MODIFIED_DATE_CLASSIFIER_VERSION,
    MANAGED_SOURCE_MODIFIED_DATE_RELATION_SOURCE,
    image_date_category_path,
    image_upload_date_label,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.chunks.service import DocumentIndexService, INDEX_VERSION
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.content_types import detect_image_content_type, infer_content_type
from app.modules.retrieval.search_profile import DocumentSearchProfileService
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


@dataclass(slots=True)
class InitialWorkingPathResolution:
    """首次导入的安全目标。

    同名事实只保存到索引并在用户查询、上传或实际使用相关文件时提示；后台同步
    不得通过“待确认”物理目录向用户提前制造决策任务。
    """

    relative_path: str
    filename: str
    storage_collision: bool = False


def working_copy_search_artifact_status(
    db: Session,
    working_copy: WorkingCopy,
) -> dict[str, bool]:
    """返回工作副本的阶段四检索派生数据状态。

    物理文件存在不代表可以被对话检索。扫描器必须据此补发导入修复任务，
    避免历史工作副本因为幂等短路永久缺少 SearchProfile 或 Chunk。
    """

    if working_copy.status != "ACTIVE" or not working_copy.current_version_id:
        return {
            "working_copy_active": working_copy.status == "ACTIVE",
            "current_version_ready": bool(working_copy.current_version_id),
            "profile_ready": False,
            "index_ready": False,
            "repair_blocked": False,
            "ready": False,
        }
    profile_exists = (
        db.query(DocumentSearchProfile.id)
        .filter(
            DocumentSearchProfile.working_copy_id == working_copy.id,
            DocumentSearchProfile.document_version_id == working_copy.current_version_id,
            DocumentSearchProfile.status == "ACTIVE",
        )
        .first()
        is not None
    )
    index_exists = (
        db.query(DocumentIndexRun.id)
        .filter(
            DocumentIndexRun.document_version_id == working_copy.current_version_id,
            DocumentIndexRun.status == "COMPLETED",
            DocumentIndexRun.index_version == INDEX_VERSION,
        )
        .first()
        is not None
    )
    # 当前版本已经留下确定性解析失败时，自动扫描不能每轮再次创建相同修复任务。
    # 管理员显式重处理会创建新的解析运行；成功后此阻塞条件自然消失。
    successful_extraction_exists = (
        db.query(DocumentExtractionRun.id)
        .filter(
            DocumentExtractionRun.document_id == working_copy.document_id,
            DocumentExtractionRun.document_version_id == working_copy.current_version_id,
            DocumentExtractionRun.status == "COMPLETED",
        )
        .first()
        is not None
    )
    repair_blocked = (
        not index_exists
        and not successful_extraction_exists
        and db.query(DocumentExtractionRun.id)
        .filter(
            DocumentExtractionRun.document_id == working_copy.document_id,
            DocumentExtractionRun.document_version_id == working_copy.current_version_id,
            DocumentExtractionRun.status == "FAILED",
        )
        .order_by(DocumentExtractionRun.updated_at.desc())
        .first()
        is not None
    )
    return {
        "working_copy_active": True,
        "current_version_ready": True,
        "profile_ready": profile_exists,
        "index_ready": index_exists,
        "repair_blocked": repair_blocked,
        "ready": profile_exists and index_exists,
    }


def working_copy_search_artifacts_ready(db: Session, working_copy: WorkingCopy) -> bool:
    """兼容返回布尔值的扫描器调用，真实判断统一由状态函数完成。"""

    return working_copy_search_artifact_status(db, working_copy)["ready"]


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
    ) -> tuple[DocumentVersion, UploadArchiveRecord, UploadDuplicateReview]:
        """登记未发送的上传版本，不创建查重或其他处理任务。

        处理任务只能由用户点击发送后调用 ``start_processing`` 创建。
        """

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
        self.db.flush()
        return version, archive, review

    def start_processing(
        self,
        *,
        upload_version_id: str,
        current_user: User,
    ) -> UploadProcessingStartResponse:
        """幂等启动一个已发送暂存文件的查重任务。"""

        if not self.settings.filesystem_async_jobs_enabled:
            raise RuntimeError("FILESYSTEM_ASYNC_JOBS_ENABLED 必须开启")
        if not self.settings.upload_duplicate_check_enabled:
            raise RuntimeError("UPLOAD_DUPLICATE_CHECK_ENABLED 必须开启")
        review = (
            self.db.query(UploadDuplicateReview)
            .filter(
                UploadDuplicateReview.upload_document_version_id == upload_version_id,
                UploadDuplicateReview.user_id == current_user.id,
            )
            .with_for_update()
            .one_or_none()
        )
        if review is None:
            raise HTTPException(status_code=404, detail="Upload version not found")
        version = self.db.get(DocumentVersion, upload_version_id)
        document = self.db.get(Document, version.document_id) if version else None
        archive = (
            self.db.query(UploadArchiveRecord)
            .filter(UploadArchiveRecord.upload_document_version_id == upload_version_id)
            .with_for_update()
            .one_or_none()
        )
        if version is None or document is None or document.user_id != current_user.id or archive is None:
            raise HTTPException(status_code=404, detail="Upload version not found")
        if review.status == "STAGED" and archive.status == "STAGED":
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
            review.status = "CHECKING"
            review.expires_at = utcnow() + timedelta(
                hours=self.settings.upload_duplicate_confirmation_ttl_hours
            )
            archive.filesystem_job_id = job.id
            archive.status = "DUPLICATE_CHECK_PENDING"
            document.ingest_status = "DUPLICATE_CHECK_PENDING"
            self.db.commit()
        elif not review.duplicate_check_job_id or not archive.filesystem_job_id:
            raise HTTPException(status_code=409, detail="Upload is not staged for processing")
        return UploadProcessingStartResponse(
            upload_document_version_id=upload_version_id,
            document_id=document.id,
            duplicate_review_id=review.id,
            filesystem_job_id=str(review.duplicate_check_job_id),
            archive_status=archive.status,
            duplicate_review_status=review.status,
        )

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

        upload_version = self.db.get(DocumentVersion, review.upload_document_version_id)
        upload_document = self.db.get(Document, upload_version.document_id) if upload_version else None
        if (
            upload_document is not None
            and upload_document.status == "USED_IN_MESSAGE"
            and request.decision in {"USE_EXISTING_FILE", "CANCEL_UPLOAD"}
        ):
            raise HTTPException(
                status_code=409,
                detail="Upload already used in a message; it can no longer be replaced or cancelled",
            )

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
        if upload_document is not None and request.decision in {"USE_EXISTING_FILE", "CANCEL_UPLOAD"}:
            upload_document.status = (
                "UPLOAD_REPLACED_BY_EXISTING"
                if request.decision == "USE_EXISTING_FILE"
                else "UPLOAD_CANCELLED"
            )
        if review.conversation_id:
            conversation = self.db.get(Conversation, review.conversation_id)
            if conversation is not None and conversation.user_id == current_user.id:
                # 前端确认按钮是明确用户输入，必须形成消息审计，但内部决策枚举不能显示在聊天流。
                confirmation_message = Message(
                    conversation_id=conversation.id,
                    user_id=current_user.id,
                    role="SYSTEM_AUDIT",
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
                # 原重复确认卡完成后退出普通消息流；审计消息和 review 记录继续保留。
                notification_message = (
                    self.db.get(Message, review.notification_message_id)
                    if review.notification_message_id
                    else None
                )
                if notification_message is not None:
                    notification_message.role = "SYSTEM_AUDIT"
        self._append_audit(
            review=review,
            change_type="UPLOAD_DUPLICATE_DECISION_RECORDED",
            summary=f"已记录重复上传决策：{request.decision}",
            after_value={
                "decision": request.decision,
                "selected_existing_working_copy_id": selected_copy.id if selected_copy else None,
            },
            visible_in_conversation=False,
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
        shared_workspace_id = get_shared_workspace_id(self.db)
        review = self.repository.get_review_by_version(upload_version_id)
        working_copy = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.managed_file_id == archive.managed_file_id, WorkingCopy.workspace_id == shared_workspace_id)
            .first()
            if archive.managed_file_id
            else None
        )
        if (
            working_copy is None
            and review is not None
            and review.selected_existing_working_copy_id
        ):
            working_copy = (
                self.db.query(WorkingCopy)
                .filter(
                    WorkingCopy.id == review.selected_existing_working_copy_id,
                    WorkingCopy.workspace_id == shared_workspace_id,
                    WorkingCopy.status == "ACTIVE",
                )
                .one_or_none()
            )
        classification = (
            CurrentClassificationEvidenceReader(
                db=self.db,
                user_id=None,
                workspace_id=shared_workspace_id,
            ).read(document_ids=[working_copy.document_id])[0]
            if working_copy is not None
            else None
        )
        decision = (
            self.db.query(DocumentOrganizationDecision)
            .filter(
                DocumentOrganizationDecision.working_copy_id == working_copy.id,
                DocumentOrganizationDecision.document_version_id
                == str(working_copy.current_version_id or ""),
            )
            .order_by(DocumentOrganizationDecision.created_at.desc())
            .first()
            if working_copy is not None
            else None
        )
        primary_relation = (
            self.db.query(DocumentCategory)
            .filter(
                DocumentCategory.working_copy_id == working_copy.id,
                DocumentCategory.document_version_id
                == str(working_copy.current_version_id or ""),
                DocumentCategory.relation_role == "PRIMARY",
                DocumentCategory.status.in_(["AUTO_APPLIED", "CONFIRMED"]),
            )
            .one_or_none()
            if working_copy is not None
            else None
        )
        rename_review = (
            self.db.query(FileRenameReviewItem)
            .filter(
                FileRenameReviewItem.document_id == working_copy.document_id,
                FileRenameReviewItem.status == "NEEDS_REVIEW",
            )
            .order_by(FileRenameReviewItem.created_at.desc())
            .first()
            if working_copy is not None
            else None
        )
        pending_decision = (
            dict(rename_review.review_context_json or {})
            if rename_review is not None
            else None
        )
        classification_status = (
            str(classification.get("status") or "PROCESSING")
            if classification is not None
            else "PROCESSING"
        )
        categories = self._upload_status_categories(classification)
        if (
            primary_relation is not None
            and primary_relation.source == IMAGE_DATE_RELATION_SOURCE
        ):
            # 图片日期目录是用户明确指定的组织规则，不依赖 OCR 正文语义；批次回执
            # 必须投影最终生效路径，不能继续展示后台候选分类或误报“缺少证据”。
            category_path = list(primary_relation.category_path_json or [])
            date_label = category_path[-1] if category_path else ""
            categories = [
                {
                    "category_id": primary_relation.category_id,
                    "name": date_label or "上传日期",
                    "category_path": category_path,
                    "confidence": 1.0,
                    "status": primary_relation.status,
                    "source": IMAGE_DATE_RELATION_SOURCE,
                    "evidence_items": [
                        {
                            "type": "upload_metadata",
                            "quote": "按图片上传日期自动归档",
                            "source": IMAGE_DATE_RELATION_SOURCE,
                            "upload_date": date_label,
                        }
                    ],
                    "evidence": ["按图片上传日期自动归档"],
                }
            ]
            classification_status = "COMPLETED"
        review_reasons = self._upload_status_review_reasons(
            archive=archive,
            decision=decision,
            pending_decision=pending_decision,
        )
        classification_missing = (
            working_copy is not None
            and working_copy.status == "ACTIVE"
            and (classification_status != "COMPLETED" or not categories)
        )
        if classification_missing:
            review_reasons.append("当前工作副本没有可展示的分类证据，需要人工复核。")
            review_reasons = list(dict.fromkeys(review_reasons))
        processing_status = (
            "FAILED"
            if archive.status == "FAILED"
            else "NEEDS_REVIEW"
            if archive.status == "NEEDS_REVIEW"
            or rename_review is not None
            or classification_missing
            or (decision is not None and decision.decision == "NEEDS_REVIEW")
            else "COMPLETED"
            if working_copy is not None and working_copy.status == "ACTIVE"
            else "PROCESSING"
        )
        # ORGANIZING 阶段的工作副本仍使用暂存名称，不能把它投影成最终重命名结果。
        working_copy_ready = working_copy is not None and working_copy.status == "ACTIVE"
        renamed_filename = working_copy.filename if working_copy_ready else None
        rename_status = (
            "FAILED"
            if archive.status == "FAILED"
            else "NEEDS_REVIEW"
            if archive.status == "NEEDS_REVIEW"
            else "PROCESSING"
            if not working_copy_ready
            else "NEEDS_REVIEW"
            if rename_review is not None
            else "COMPLETED"
            if renamed_filename != document.original_filename
            else "NO_CHANGE"
        )
        return ArchiveStatusResponse(
            upload_document_version_id=upload_version_id,
            document_id=working_copy.document_id if working_copy else document.id,
            status=archive.status,
            managed_file_id=archive.managed_file_id,
            working_copy_id=working_copy.id if working_copy else None,
            working_copy_status=working_copy.status if working_copy else None,
            original_filename=document.original_filename,
            renamed_filename=renamed_filename,
            processing_status=processing_status,
            rename_status=rename_status,
            classification_status=(
                "FAILED"
                if archive.status == "FAILED"
                else "NEEDS_REVIEW"
                if archive.status == "NEEDS_REVIEW" and classification is None
                else classification_status
            ),
            categories=categories,
            organization_status=(
                decision.decision
                if decision is not None
                else "NEEDS_REVIEW"
                if processing_status == "NEEDS_REVIEW"
                else None
            ),
            review_reasons=review_reasons,
            pending_decision=pending_decision,
            filesystem_job_id=archive.filesystem_job_id,
            error_code=archive.last_error_code,
            error_message=archive.last_error_message,
        )

    @staticmethod
    def _upload_status_review_reasons(
        *,
        archive: UploadArchiveRecord,
        decision: DocumentOrganizationDecision | None,
        pending_decision: dict[str, Any] | None,
    ) -> list[str]:
        """把后台审计原因投影为上传批次可直接展示的用户文案。"""

        reasons: list[str] = []
        if archive.status == "NEEDS_REVIEW":
            reasons.append("文件存在需要人工处理的格式或安全风险，原件未被修改。")
        reason_messages = {
            "PARSE_FAILED": "文件正文解析未完成，分类结果需要复核。",
            "RISK_CHECK_FAILED": "文件风险检查未通过，暂未自动归入正式分类。",
            "NO_TAXONOMY_CANDIDATE": "没有找到可靠的具体分类。",
            "OTHER_CATEGORY": "只能确定为其他分类，需要人工确认。",
            "FREE_PATH_NOT_ALLOWED": "分类候选不属于当前正式分类目录。",
            "EVIDENCE_MISSING": "正文中缺少可定位的分类证据。",
            "POLICY_VERSION_UNAVAILABLE": "分类规则版本信息不完整。",
            "TARGET_NAME_CONFLICT": "标准文件名与现有文件冲突，已保留在待复核位置。",
            "TARGET_PATH_UNAVAILABLE": "目标分类路径当前不可用。",
        }
        if decision is not None:
            reasons.extend(
                reason_messages.get(code, "文件分类需要人工复核。")
                for code in list(decision.reason_codes_json or [])
            )
        if pending_decision:
            reasons.append(
                str(pending_decision.get("message") or "标准文件名依据不足，需要人工复核。")
            )
        if archive.status == "FAILED" and archive.last_error_message:
            reasons.append(str(archive.last_error_message))
        return list(dict.fromkeys(reason for reason in reasons if reason))

    @staticmethod
    def _upload_status_categories(
        classification: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """补齐现有分类卡依赖的兼容证据字段，同时保留可定位证据。"""

        projected: list[dict[str, Any]] = []
        for category in list((classification or {}).get("categories") or []):
            if not isinstance(category, dict):
                continue
            evidence: list[str] = []
            for item in list(category.get("evidence_items") or []):
                if isinstance(item, str) and item.strip():
                    evidence.append(item.strip())
                    continue
                if not isinstance(item, dict):
                    continue
                evidence.extend(
                    str(signal).strip()
                    for signal in list(item.get("signals") or [])
                    if str(signal).strip()
                )
                quote = str(item.get("quote") or "").strip()
                if quote:
                    evidence.append(quote)
            projected.append(
                {
                    **category,
                    "evidence": list(dict.fromkeys(evidence)),
                }
            )
        return projected

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
        """重新校验候选仍属于共享工作目录且处于活动状态。"""

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
        """候选只要是共享目录中的活动工作副本，就允许任意登录用户选择。"""

        working_copy = self.db.get(WorkingCopy, candidate.candidate_working_copy_id) if candidate.candidate_working_copy_id else None
        document = self.db.get(Document, working_copy.document_id) if working_copy else None
        return bool(
            working_copy
            and document
            and working_copy.workspace_id == get_shared_workspace_id(self.db)
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
        visible_in_conversation: bool = True,
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
            visible_in_conversation=visible_in_conversation,
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
            "MATERIALIZE_WORKING_COPY": self._materialize_working_copy,
            "ANALYZE_DOCUMENT_VERSION": self._analyze_document_version,
            "CLEANUP_UPLOAD_TEMP": self._cleanup_upload_temp,
            "RECONCILE_UPLOAD_ARCHIVES": self._reconcile_upload_archives,
            "RECONCILE_MANAGED_ROOT": self._reconcile_managed_root,
            "REPAIR_WORKING_COPY_LAYOUT": self._repair_working_copy_layout,
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
            "MATERIALIZE_WORKING_COPY",
            "ANALYZE_DOCUMENT_VERSION",
            "CLEANUP_UPLOAD_TEMP",
            "RECONCILE_UPLOAD_ARCHIVES",
            "RECONCILE_MANAGED_ROOT",
            "REPAIR_WORKING_COPY_LAYOUT",
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
        if job.job_type == "MATERIALIZE_WORKING_COPY":
            # 相关文件集合是“本轮最终相关范围”的审计事实。物化失败不能只留在
            # FilesystemJob 中，否则管理员无法区分“尚未消费”和“副本创建失败”。
            # 后台任务可能先于用户检索被领取，payload 中没有集合 ID，因此必须按
            # 稳定 revision_id 回写全部等待集合，不能让它们永久停在 MATERIALIZING。
            self._mark_relevant_file_set_item(
                payload=dict(job.payload_json or {}),
                status="RETRY_WAIT" if retrying else "FAILED",
            )
        self.db.flush()

    def _reconcile_upload_archives(self, job: FilesystemJob) -> None:
        """补偿待查重、待归档和可重试失败上传，绝不越过待确认状态。"""

        records = (
            self.db.query(UploadArchiveRecord)
            .filter(
                UploadArchiveRecord.status.in_(
                    {"STAGED", "DUPLICATE_CHECK_PENDING", "PENDING", "RETRY_WAIT", "FAILED"}
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
            current_job = (
                self.db.get(FilesystemJob, archive.filesystem_job_id)
                if archive.filesystem_job_id
                else None
            )
            if archive.status == "STAGED":
                expires_at = review.expires_at
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at > utcnow():
                    # 未发送且仍在有效暂存期内的文件绝不能被补偿任务自动处理。
                    continue
                review.status = "RESOLVED"
                review.decision = "CANCEL_UPLOAD"
                review.decided_at = utcnow()
                archive.status = "CANCELLED"
                version = self.db.get(DocumentVersion, archive.upload_document_version_id)
                document = self.db.get(Document, version.document_id) if version else None
                if document is not None:
                    document.status = "UPLOAD_CANCELLED"
                child = UploadLifecycleService(self.db, self.settings)._enqueue_cleanup(
                    review=review
                )
            elif archive.status == "DUPLICATE_CHECK_PENDING":
                if current_job is not None and current_job.status in {"PENDING", "RUNNING"}:
                    continue
                if current_job is not None and current_job.status == "FAILED":
                    archive.status = "FAILED"
                    review.status = "FAILED"
                    archive.next_retry_at = None
                    continue
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
            elif archive.status == "FAILED":
                # Terminal queue failures require an explicit retry. Reconciliation must not
                # expose a PENDING business state when no runnable queue job exists.
                continue
            elif archive.status == "RETRY_WAIT":
                if current_job is not None and current_job.status in {"PENDING", "RUNNING"}:
                    continue
                archive.status = "FAILED"
                archive.next_retry_at = None
                continue
            elif archive.next_retry_at and archive.next_retry_at > utcnow():
                continue
            elif current_job is not None and current_job.status in {"PENDING", "RUNNING"}:
                continue
            elif current_job is not None and current_job.status == "FAILED":
                archive.status = "FAILED"
                archive.next_retry_at = None
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
        child = (
            self.db.query(FilesystemJob)
            .filter(
                FilesystemJob.root_id == job.root_id,
                FilesystemJob.job_type == "SCAN_MANAGED_ROOT",
                FilesystemJob.status.in_({"PENDING", "RUNNING"}),
            )
            .order_by(FilesystemJob.created_at.desc())
            .first()
        )
        previous_result = dict(job.result_json or {})
        try:
            previous_generation = max(
                0,
                int(previous_result.get("scan_generation") or 0),
            )
        except (TypeError, ValueError):
            previous_generation = 0
        scan_generation = previous_generation
        scan_reused = child is not None
        if child is None:
            scan_generation += 1
            child = FilesystemJobQueue(self.db).create_job(
                job_type="SCAN_MANAGED_ROOT",
                # 扫描与 IMPORT 使用独立队列；部署时由不同 worker 消费，避免大目录
                # 扫描占住同一 worker 后让已发现文件迟迟无法进入工作副本。
                queue_name="SCAN",
                root_id=job.root_id,
                created_by=job.created_by,
                # 每个协调周期使用独立代次。已完成或失败的历史扫描保持终态，
                # 不会被重置，也不会阻断配置修复或服务重启后的下一轮扫描。
                deduplication_key=(
                    f"managed-root-scan:{job.root_id}:{scan_generation}"
                ),
                payload={
                    "reconcile_job_id": job.id,
                    "scan_generation": scan_generation,
                },
            )
        FilesystemJobQueue(self.db).mark_completed(
            job=job,
            result={
                "scan_job_id": child.id,
                "scan_generation": scan_generation,
                "scan_reused": scan_reused,
            },
        )

    def _repair_working_copy_layout(self, job: FilesystemJob) -> None:
        """迁移旧共享根和历史待整理路径，不触碰受管原始目录。"""

        if not job.root_id:
            raise RuntimeError("REPAIR_WORKING_COPY_LAYOUT 缺少 root_id")
        result = WorkingCopyLayoutRepairService(self.db).repair_managed_root(
            managed_root_id=job.root_id,
        )
        FilesystemJobQueue(self.db).mark_completed(job=job, result=result)

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
            filename=version.filename,
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
        if archive.status not in {"PENDING", "RETRY_WAIT"}:
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
            # 归档是后台生命周期事件；普通用户只在任务回执中看到最终处理结果，
            # 不在对话流中展示“正在创建工作副本”的内部中间状态。
            visible_in_conversation=False,
        )
        archive.changeset_id = audit[0].id
        import_job = FilesystemJobQueue(self.db).create_job(
            job_type="IMPORT_WORKING_COPIES",
            queue_name="IMPORT",
            root_id=root.id,
            created_by=document.user_id,
            deduplication_key=f"working-copy-import:{get_shared_workspace_id(self.db)}:{managed_file.id}",
            payload={
                "managed_file_id": managed_file.id,
                "workspace_id": get_shared_workspace_id(self.db),
                "user_id": document.user_id,
                "source_upload_document_id": document.id,
            },
        )
        archive.filesystem_job_id = import_job.id
        FilesystemJobQueue(self.db).mark_completed(
            job=job,
            result={"managed_file_id": managed_file.id, "import_job_id": import_job.id},
        )

    def _enqueue_document_analysis(
        self,
        *,
        job: FilesystemJob,
        managed_file: ManagedFile,
        working_copy: WorkingCopy,
        document: Document,
        version: DocumentVersion,
        user_id: str,
    ) -> FilesystemJob:
        """为已可用工作副本提交独立分析任务，失败终态不会被自动扫描重开。"""

        return FilesystemJobQueue(self.db).create_job(
            job_type="ANALYZE_DOCUMENT_VERSION",
            queue_name="ANALYSIS",
            root_id=managed_file.root_id,
            created_by=user_id,
            deduplication_key=f"document-analysis:{version.id}",
            priority=job.priority,
            max_attempts=3,
            payload={
                "managed_file_id": managed_file.id,
                "working_copy_id": working_copy.id,
                "document_id": document.id,
                "document_version_id": version.id,
                "user_id": user_id,
            },
        )

    def _import_working_copy(self, job: FilesystemJob) -> None:
        """快速复制并登记活动工作副本，把解析、摘要和索引交给 ANALYSIS 队列。

        IMPORT 队列只执行文件可用性所需的最小事务，避免大文件解析长期阻塞后续
        文件。文件名级检索投影与工作副本在同一事务创建，正文能力随后异步补齐。
        """

        payload = dict(job.payload_json or {})
        managed_file = self.db.get(ManagedFile, str(payload.get("managed_file_id") or ""))
        workspace_id = get_shared_workspace_id(self.db)
        user_id = str(payload.get("user_id") or job.created_by or "")
        if managed_file is None or managed_file.status != "ACTIVE" or not workspace_id or not user_id:
            raise RuntimeError("IMPORT_WORKING_COPIES 缺少有效原始文件、共享工作区或用户")
        managed_root = self.db.get(ManagedRoot, managed_file.root_id)
        if managed_root is None:
            raise RuntimeError("原始文件目录不存在")

        working_root = self.repository.get_or_create_working_root(
            workspace_id=workspace_id,
            managed_root=managed_root,
        )
        existing = self.repository.find_primary_working_copy(
            working_root_id=working_root.id,
            managed_file_id=managed_file.id,
        )
        if existing is not None:
            if existing.status != "ACTIVE":
                FilesystemJobQueue(self.db).mark_completed(
                    job=job,
                    result={
                        "working_copy_id": existing.id,
                        "idempotent": True,
                        "skipped_status": existing.status,
                    },
                )
                return
            document = self.db.get(Document, existing.document_id)
            version = self.db.get(DocumentVersion, existing.current_version_id)
            if document is None or version is None:
                raise RuntimeError("现有工作副本缺少 Document 或 DocumentVersion")
            storage_relative_path = f"{working_root.relative_storage_path}/{existing.relative_path}"
            target = self.storage.working_copy_path(storage_relative_path)
            physical_copy_repaired = False
            if not target.is_file():
                if target.exists():
                    raise RuntimeError("工作副本目标路径被非文件对象占用，禁止自动覆盖")
                source = resolve_managed_relative_path(
                    root_path=Path(managed_root.container_path),
                    relative_path=managed_file.relative_path,
                )
                source_sha256 = managed_file.content_sha256 or self.storage.sha256_file(source)
                expected_sha256 = existing.imported_source_sha256 or existing.content_sha256
                if expected_sha256 and source_sha256 != expected_sha256:
                    existing.sync_status = "ORIGINAL_CHANGED"
                    raise RuntimeError("原件内容已变化，不能用新内容修复旧版本工作副本")
                self.storage.import_working_copy(
                    source=source,
                    relative_path=storage_relative_path,
                    expected_sha256=source_sha256,
                )
                existing.content_sha256 = source_sha256
                existing.imported_source_sha256 = source_sha256
                existing.size_bytes = managed_file.size_bytes
                existing.sync_status = "SYNCED"
                existing.updated_at = utcnow()
                physical_copy_repaired = True
            DocumentSearchProfileService(db=self.db).upsert_current_profile(existing.id)
            analysis_job = (
                None
                if bool(payload.get("skip_document_analysis"))
                else self._enqueue_document_analysis(
                    job=job,
                    managed_file=managed_file,
                    working_copy=existing,
                    document=document,
                    version=version,
                    user_id=user_id,
                )
            )
            FilesystemJobQueue(self.db).mark_completed(
                job=job,
                result={
                    "working_copy_id": existing.id,
                    "document_id": document.id,
                    "document_version_id": version.id,
                    "analysis_job_id": analysis_job.id if analysis_job is not None else None,
                    "idempotent": True,
                    "physical_copy_repaired": physical_copy_repaired,
                },
            )
            return

        source = resolve_managed_relative_path(
            root_path=Path(managed_root.container_path),
            relative_path=managed_file.relative_path,
        )
        source_stat_before = source.stat()
        source_sha256 = managed_file.content_sha256
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
            copy_started_at = time.perf_counter()
            _, copied_sha256 = self.storage.stage_working_copy(
                source=source,
                relative_path=staged_relative_path,
                expected_sha256=source_sha256,
            )
            source_sha256 = copied_sha256
            managed_file.content_sha256 = copied_sha256
            log_event(
                "working_copy.import.copy_completed",
                status="COMPLETED",
                duration_ms=int((time.perf_counter() - copy_started_at) * 1000),
                managed_file_id=managed_file.id,
                root_id=managed_file.root_id,
                size_bytes=managed_file.size_bytes,
                message="工作副本暂存复制完成",
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
                # 工作副本与上传、受管快照共享稳定 MIME 映射；图片不能再回落为通用二进制。
                content_type=infer_content_type(filename=managed_file.filename),
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
                source_managed_file_revision_id=str(payload.get("source_managed_file_revision_id") or "") or None,
                source_analysis_run_id=str(payload.get("source_analysis_run_id") or "") or None,
                created_by=user_id,
            )
            self.db.add(version)
            self.db.flush()

            gated_initial_placement = self._gated_initial_placement_enabled(
                payload=payload,
                managed_file=managed_file,
            )
            if gated_initial_placement:
                # 隐藏期路径仅用于 Worker 定位，不是用户可见归档结果；最终路径必须在
                # 分类门槛和 taxonomy 安全路径校验后由同一 StorageService 原子发布。
                root_prefix = f"{working_root.relative_storage_path}/"
                relative_path = staged_relative_path.removeprefix(root_prefix)
                filename = self.storage.sanitize_filename(managed_file.filename)
                path_resolution = InitialWorkingPathResolution(
                    relative_path=relative_path,
                    filename=filename,
                )
            else:
                path_resolution = self._working_path_resolution(
                    working_root=working_root,
                    managed_file=managed_file,
                    preferred_relative_path=managed_file.relative_path,
                )
                relative_path = path_resolution.relative_path
                filename = path_resolution.filename
                final_storage_relative_path = f"{working_root.relative_storage_path}/{relative_path}"
                publish_started_at = time.perf_counter()
                final_target, final_target_created = self.storage.publish_working_copy(
                    staged_relative_path=staged_relative_path,
                    target_relative_path=final_storage_relative_path,
                    expected_sha256=source_sha256,
                    staged_hash_verified=True,
                )
                log_event(
                    "working_copy.import.publish_completed",
                    document_id=document.id,
                    status="COMPLETED",
                    duration_ms=int((time.perf_counter() - publish_started_at) * 1000),
                    managed_file_id=managed_file.id,
                    root_id=managed_file.root_id,
                    message="工作副本原子发布完成",
                )

            document.ingest_status = "INGESTED"
            version.storage_path = (
                staged_relative_path if gated_initial_placement else final_storage_relative_path
            )
            version.filename = filename
            file_object.storage_path = version.storage_path
            working_copy = WorkingCopy(
                working_copy_root_id=working_root.id,
                workspace_id=workspace_id,
                managed_file_id=managed_file.id,
                document_id=document.id,
                relative_path=relative_path,
                relative_path_hash=hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                filename=filename,
                extension=Path(filename).suffix.lower(),
                size_bytes=managed_file.size_bytes,
                content_sha256=source_sha256,
                imported_source_sha256=source_sha256,
                is_primary_import=True,
                status="ORGANIZING" if gated_initial_placement else "ACTIVE",
                sync_status="SYNCED",
            )
            self.db.add(working_copy)
            self.db.flush()
            version.working_copy_id = working_copy.id
            working_copy.current_version_id = version.id
            working_root.status = "READY"
            working_root.last_imported_at = utcnow()
            if not gated_initial_placement:
                DocumentSearchProfileService(db=self.db).upsert_current_profile(working_copy.id)

            changeset, _ = create_lifecycle_audit(
                db=self.db,
                user_id=user_id,
                workspace_id=workspace_id,
                conversation_id=self._conversation_for_upload(managed_file),
                tool_name="working-copy-fast-import",
                message_content=(
                    f"文件“{filename}”正在完成首次分类整理。"
                    if gated_initial_placement
                    else f"文件“{filename}”已进入共享工作目录，后台分析任务已排队。"
                ),
                change_type=(
                    "WORKING_COPY_ORGANIZING"
                    if gated_initial_placement
                    else "WORKING_COPY_IMPORTED"
                ),
                target_type="working_copy",
                target_id=working_copy.id,
                target_document_id=document.id,
                after_value={
                    "working_copy_id": working_copy.id,
                    "managed_file_id": managed_file.id,
                    "relative_path": relative_path,
                    "document_version_id": version.id,
                    "analysis_status": "ORGANIZING" if gated_initial_placement else "PENDING",
                    "storage_collision": path_resolution.storage_collision,
                    "managed_original_unchanged": True,
                },
                graph_document_results=[],
                visible_in_conversation=False,
            )
            if not gated_initial_placement:
                self.db.add(WorkingCopyPathRecord(
                    working_copy_id=working_copy.id,
                    sequence_number=1,
                    operation_type="INITIAL_IMPORT",
                    before_relative_path=managed_file.relative_path,
                    after_relative_path=relative_path,
                    before_filename=managed_file.filename,
                    after_filename=filename,
                    document_version_id=version.id,
                    content_sha256=source_sha256,
                    agent_run_id=changeset.agent_run_id,
                    changeset_id=changeset.id,
                    status="COMPLETED",
                    executed_by=user_id,
                ))
            analysis_job = (
                None
                if bool(payload.get("skip_document_analysis"))
                else self._enqueue_document_analysis(
                    job=job,
                    managed_file=managed_file,
                    working_copy=working_copy,
                    document=document,
                    version=version,
                    user_id=user_id,
                )
            )
            FilesystemJobQueue(self.db).mark_completed(
                job=job,
                result={
                    "working_copy_id": working_copy.id,
                    "document_id": document.id,
                    "document_version_id": version.id,
                    "filename": filename,
                    "relative_path": relative_path,
                    "analysis_job_id": analysis_job.id if analysis_job is not None else None,
                    "working_copy_status": working_copy.status,
                    "agent_run_id": changeset.agent_run_id,
                    "changeset_id": changeset.id,
                },
            )
        except Exception:
            self.storage.working_copy_path(staged_relative_path).unlink(missing_ok=True)
            if final_target is not None and final_storage_relative_path and final_target_created:
                final_target.unlink(missing_ok=True)
            raise

    def _materialize_working_copy(self, job: FilesystemJob) -> None:
        """按需把已分析原始文件修订物化为工作副本。

        物化允许复制只读原件，但绝不改变它。成功后把源侧页面、摘要与 Chunk 复用到
        新工作副本，避免旧版 Office 文件再次触发 LibreOffice 全文转换。
        """

        payload = dict(job.payload_json or {})
        revision = self.db.get(
            ManagedFileRevision,
            str(payload.get("managed_file_revision_id") or ""),
        )
        if revision is None or not revision.is_current or revision.status != "READY":
            raise RuntimeError("MATERIALIZE_WORKING_COPY 缺少已完成分析的当前原始文件修订")
        if not revision.analysis_document_id or not revision.analysis_document_version_id:
            raise RuntimeError("当前原始文件修订缺少可复用分析结果")
        managed_file = self.db.get(ManagedFile, revision.managed_file_id)
        if managed_file is None or managed_file.status != "ACTIVE":
            raise RuntimeError("当前原始文件已不存在，不能物化工作副本")
        root = self.db.get(ManagedRoot, managed_file.root_id)
        classification_user_id = str(
            (root.created_by if root is not None else "")
            or job.created_by
            or self.db.query(User.id).order_by(User.created_at.asc()).scalar()
            or ""
        )
        if not classification_user_id or root is None:
            raise RuntimeError("工作副本物化缺少分类新鲜度校验用户或受管根")
        identity = current_classification_identity(
            db=self.db,
            settings=self.settings,
            user_id=classification_user_id,
        )
        freshness = inspect_managed_source_classification(
            db=self.db,
            revision=revision,
            identity=identity,
        )
        if freshness is not ClassificationFreshness.CURRENT:
            queue = FilesystemJobQueue(self.db)
            refresh_job = queue.create_job(
                job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION",
                queue_name="SOURCE_ANALYSIS",
                root_id=root.id,
                created_by=classification_user_id,
                priority=classification_refresh_priority(self.settings),
                deduplication_key=classification_refresh_deduplication_key(
                    revision_id=revision.id,
                    identity=identity,
                ),
                reuse_completed=True,
                payload={
                    "managed_file_revision_id": revision.id,
                    "user_id": classification_user_id,
                    "taxonomy_key": identity.taxonomy_key,
                    "taxonomy_version": identity.taxonomy_version,
                    "classifier_version": identity.classifier_version,
                },
            )
            refresh_job = queue.promote_pending_job(
                job=refresh_job,
                priority=classification_refresh_priority(self.settings),
            )
            queue.mark_completed(
                job=job,
                result={
                    "status": "DEFERRED_CLASSIFICATION_REFRESH",
                    "managed_file_revision_id": revision.id,
                    "classification_freshness": freshness.value,
                    "classification_refresh_job_id": refresh_job.id,
                },
            )
            log_event(
                "working_copy.materialization.classification_refresh_deferred",
                status="PENDING",
                managed_file_id=managed_file.id,
                managed_file_revision_id=revision.id,
                filesystem_job_id=refresh_job.id,
                taxonomy_key=identity.taxonomy_key,
                taxonomy_version=identity.taxonomy_version,
                classifier_version=identity.classifier_version,
                message="源分类身份已过期，工作副本物化已延迟",
            )
            return
        existing_copy = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopy.managed_file_id == managed_file.id,
                WorkingCopy.status == "ACTIVE",
            )
            .one_or_none()
        )
        if (
            existing_copy is not None
            and existing_copy.imported_source_sha256
            and existing_copy.imported_source_sha256 != revision.content_sha256
        ):
            # 工作副本可能已经被用户改名、移动或作为后续操作对象。新原始修订绝不
            # 自动覆盖它，也不能伪造“该副本来自新修订”的来源关系。
            self._mark_relevant_file_set_item(
                payload=payload,
                status="SOURCE_CHANGED",
                working_copy_id=existing_copy.id,
            )
            FilesystemJobQueue(self.db).mark_completed(
                job=job,
                result={
                    "status": "SOURCE_CHANGED",
                    "working_copy_id": existing_copy.id,
                    "managed_file_revision_id": revision.id,
                    "message": "已有工作副本与当前原始文件内容不同，未自动覆盖。",
                },
            )
            log_event(
                "working_copy.materialization.source_changed",
                level="WARNING",
                status="SOURCE_CHANGED",
                working_copy_id=existing_copy.id,
                managed_file_id=managed_file.id,
                managed_file_revision_id=revision.id,
                message="原始文件已变化，保护现有工作副本不被自动覆盖",
            )
            return
        # 复用既有导入的安全复制、路径审计与 ChangeSet 流程；额外来源字段使后续
        # 不再把同一内容送入 ANALYZE_DOCUMENT_VERSION。
        payload.update(
            {
                "managed_file_id": managed_file.id,
                "source_managed_file_revision_id": revision.id,
                "source_analysis_run_id": self._latest_source_analysis_run_id(revision.id),
                "skip_document_analysis": True,
            }
        )
        job.payload_json = payload
        self._import_working_copy(job)
        result = dict(job.result_json or {})
        working_copy_id = str(result.get("working_copy_id") or "")
        if working_copy_id:
            working_copy = self.db.get(WorkingCopy, working_copy_id)
            if working_copy is None:
                raise RuntimeError("工作副本物化结果不存在")
            document = self.db.get(Document, working_copy.document_id)
            version = self.db.get(DocumentVersion, working_copy.current_version_id)
            if document is None or version is None:
                raise RuntimeError("工作副本物化结果缺少 Document 或 DocumentVersion")
            changeset = self._initial_import_changeset(
                working_copy_id=working_copy.id,
                changeset_id=str(result.get("changeset_id") or "") or None,
            )
            organization_decision = self._reuse_source_analysis_into_working_copy(
                revision=revision,
                working_copy_id=working_copy_id,
                classification_agent_run_id=(
                    changeset.agent_run_id if changeset is not None else None
                ),
            )
            decision_row = None
            if changeset is not None:
                decision_row = self._finalize_initial_organization(
                    working_copy=working_copy,
                    managed_file=managed_file,
                    document=document,
                    version=version,
                    organization_decision=organization_decision,
                    changeset=changeset,
                    extraction_status="COMPLETED",
                )
            result["source_analysis_reused"] = True
            result["source_managed_file_revision_id"] = revision.id
            result["working_copy_status"] = working_copy.status
            result["relative_path"] = working_copy.relative_path
            result["organization_decision"] = (
                decision_row.decision if decision_row is not None else None
            )
            # ``_import_working_copy`` 已经以同一任务写入一次完成事件。此处只补充
            # 源侧复用审计，避免同一物化任务产生两条“完成”事件。
            job.result_json = result
            job.updated_at = utcnow()
            self._mark_relevant_file_set_item(
                payload=payload,
                status="MATERIALIZED",
                working_copy_id=working_copy_id,
            )
            self.db.flush()
            log_event(
                "working_copy.materialization.analysis_reused",
                status="COMPLETED",
                working_copy_id=working_copy_id,
                managed_file_id=managed_file.id,
                managed_file_revision_id=revision.id,
                message="工作副本已复用原始文件分析结果",
            )
        else:
            # IMPORT 的幂等分支也应明确结束 MATERIALIZE 任务，避免异常数据让
            # worker 将一个没有目标副本的任务误显示为成功。
            raise RuntimeError("工作副本物化未返回活动副本标识")

    def _mark_relevant_file_set_item(
        self,
        *,
        payload: dict[str, Any],
        status: str,
        working_copy_id: str | None = None,
    ) -> None:
        """按稳定源修订同步全部相关集合项，不依赖可变的任务关联负载。

        全量后台任务可能在用户检索前已经 RUNNING，不能再安全补写单个
        ``relevant_file_set_id``。因此完成、失败和源变化均按 revision_id 回写，
        让同一源修订对应的所有待处理集合收敛到真实终态。
        """

        revision_id = str(payload.get("managed_file_revision_id") or "")
        if not revision_id:
            return
        values: dict[str, Any] = {"status": status}
        if working_copy_id:
            values["working_copy_id"] = working_copy_id
        self.db.query(RelevantFileSetItem).filter(
            RelevantFileSetItem.managed_file_revision_id == revision_id,
            RelevantFileSetItem.status.in_(
                {"READY", "MATERIALIZING", "RETRY_WAIT"}
            ),
        ).update(values, synchronize_session=False)

    def _latest_source_analysis_run_id(self, revision_id: str) -> str | None:
        """读取当前修订最新完成分析运行，用于双重来源审计。"""

        from app.db.models import ManagedFileAnalysisRun

        row = (
            self.db.query(ManagedFileAnalysisRun)
            .filter(
                ManagedFileAnalysisRun.managed_file_revision_id == revision_id,
                ManagedFileAnalysisRun.status == "COMPLETED",
            )
            .order_by(ManagedFileAnalysisRun.finished_at.desc())
            .first()
        )
        return row.id if row is not None else None

    def _reuse_source_analysis_into_working_copy(
        self,
        *,
        revision: ManagedFileRevision,
        working_copy_id: str,
        classification_agent_run_id: str | None = None,
    ) -> InitialOrganizationDecision | None:
        """复制源侧持久化派生事实和分类建议，不重新读取或转换原始文件。

        外部自动导入必须复用已完成的正文分类结果；这里把建议投影到工作副本
        ``DocumentVersion``，供统一置信度门槛和后续文件详情读取。源文件始终只读。
        """

        from app.db.models import (
            DocumentClassificationSummary,
            DocumentElement,
            DocumentPage,
            DocumentSummary,
        )

        target_copy = self.db.get(WorkingCopy, working_copy_id)
        source_document = self.db.get(Document, revision.analysis_document_id)
        source_version = self.db.get(DocumentVersion, revision.analysis_document_version_id)
        target_document = self.db.get(Document, target_copy.document_id) if target_copy else None
        target_version = self.db.get(DocumentVersion, target_copy.current_version_id) if target_copy else None
        if not all([target_copy, source_document, source_version, target_document, target_version]):
            raise RuntimeError("工作副本或源侧分析谱系缺失，不能复用分析结果")
        organization_decision = self._reuse_source_classification_into_working_copy(
            source_document=source_document,
            source_version=source_version,
            target_document=target_document,
            target_version=target_version,
            target_filename=target_copy.filename,
            agent_run_id=classification_agent_run_id,
        )
        if self.db.query(DocumentIndexRun.id).filter(
            DocumentIndexRun.document_version_id == target_version.id,
            DocumentIndexRun.status == "COMPLETED",
        ).first():
            # 幂等物化即使遇到已准备完成的旧工作副本，也必须补上来源关系；
            # 否则双范围去重无法判断它是否覆盖当前原始文件修订。
            target_version.source_managed_file_revision_id = revision.id
            target_version.source_analysis_run_id = self._latest_source_analysis_run_id(revision.id)
            self.db.flush()
            return self._reuse_persisted_rename_into_initial_decision(
                document=target_document,
                decision=organization_decision,
            )
        source_run = (
            self.db.query(DocumentExtractionRun)
            .filter(
                DocumentExtractionRun.document_id == source_document.id,
                DocumentExtractionRun.document_version_id == source_version.id,
                DocumentExtractionRun.status == "COMPLETED",
            )
            .order_by(DocumentExtractionRun.updated_at.desc())
            .first()
        )
        if source_run is None:
            raise RuntimeError("源侧成功解析运行不存在，不能复用分析结果")
        clone_run = DocumentExtractionRun(
            document_id=target_document.id,
            document_version_id=target_version.id,
            status="COMPLETED",
            extractor=source_run.extractor,
            parser_name=source_run.parser_name,
            parser_version=source_run.parser_version,
            parser_config_hash=source_run.parser_config_hash,
        )
        self.db.add(clone_run)
        self.db.flush()
        for page in self.db.query(DocumentPage).filter(DocumentPage.extraction_run_id == source_run.id).all():
            self.db.add(
                DocumentPage(
                    document_id=target_document.id,
                    extraction_run_id=clone_run.id,
                    page_number=page.page_number,
                    sheet_name=page.sheet_name,
                    text_content=page.text_content,
                    metadata_json=dict(page.metadata_json or {}),
                )
            )
        for element in self.db.query(DocumentElement).filter(DocumentElement.extraction_run_id == source_run.id).all():
            self.db.add(
                DocumentElement(
                    document_id=target_document.id,
                    extraction_run_id=clone_run.id,
                    element_index=element.element_index,
                    label=element.label,
                    text_content=element.text_content,
                    page_number=element.page_number,
                    bbox_json=dict(element.bbox_json or {}),
                    content_layer=element.content_layer,
                    parent_ref=element.parent_ref,
                    metadata_json=dict(element.metadata_json or {}),
                )
            )
        self.db.flush()
        # 建索引只读取已克隆页面，因此不会再次调用 LibreOffice 或文件解析器。
        # 图片没有文字或 OCR 技术失败时，源侧只保存空正文与结构化警告；此时
        # 工作副本仍需物化并建立文件名/目录元数据投影，不能因空正文再次失败。
        has_indexable_text = any(
            str(page.text_content or "").strip()
            for page in self.db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id == clone_run.id)
            .all()
        )
        if has_indexable_text:
            index_result = DocumentIndexService(db=self.db, settings=self.settings).build(
                document_id=target_document.id,
                document_version_id=target_version.id,
                extraction_run_id=clone_run.id,
            )
            if not index_result.get("ok"):
                raise RuntimeError("工作副本复用源侧页面后建立索引失败")
        for source_summary in self.db.query(DocumentSummary).filter(
            DocumentSummary.document_id == source_document.id,
            DocumentSummary.document_version_id == source_version.id,
            DocumentSummary.status == "COMPLETED",
        ).all():
            self.db.add(
                DocumentSummary(
                    document_id=target_document.id,
                    document_version_id=target_version.id,
                    extraction_run_id=clone_run.id,
                    input_sha256=target_version.sha256,
                    summary_text=source_summary.summary_text,
                    summary_json=dict(source_summary.summary_json or {}),
                    coverage_json=dict(source_summary.coverage_json or {}),
                    model_provider=source_summary.model_provider,
                    model_name=source_summary.model_name,
                    prompt_version=source_summary.prompt_version,
                    schema_version=source_summary.schema_version,
                    status="COMPLETED",
                )
            )
        for source_summary in self.db.query(DocumentClassificationSummary).filter(
            DocumentClassificationSummary.document_id == source_document.id,
            DocumentClassificationSummary.document_version_id == source_version.id,
            DocumentClassificationSummary.status == "COMPLETED",
        ).all():
            self.db.add(
                DocumentClassificationSummary(
                    document_id=target_document.id,
                    document_version_id=target_version.id,
                    extraction_run_id=clone_run.id,
                    input_sha256=target_version.sha256,
                    summary_json=dict(source_summary.summary_json or {}),
                    model_provider=source_summary.model_provider,
                    model_name=source_summary.model_name,
                    prompt_version=source_summary.prompt_version,
                    schema_version=source_summary.schema_version,
                    status="COMPLETED",
                )
            )
        target_version.source_managed_file_revision_id = revision.id
        target_version.source_analysis_run_id = self._latest_source_analysis_run_id(revision.id)
        target_document.ingest_status = "INDEXED"
        self.db.flush()
        DocumentSearchProfileService(db=self.db).upsert_current_profile(target_copy.id)
        return self._reuse_persisted_rename_into_initial_decision(
            document=target_document,
            decision=organization_decision,
        )

    def _reuse_persisted_rename_into_initial_decision(
        self,
        *,
        document: Document,
        decision: InitialOrganizationDecision,
    ) -> InitialOrganizationDecision:
        """复用工作副本已克隆正文生成首次命名建议，不触发第二次文件解析。"""

        # 延迟导入保持文件生命周期与重命名 OperationPlan 模块之间的既有边界。
        from app.modules.file_rename.uploaded_suggestion_service import (
            UploadedRenameSuggestionService,
        )

        suggestion, extraction_result = UploadedRenameSuggestionService(
            db=self.db,
            user_id=document.user_id,
        ).suggest_for_initial_import(
            document=document,
            reuse_persisted_extraction_only=True,
        )
        decision.extraction_result = extraction_result or decision.extraction_result
        decision.rename_status = str(suggestion.get("status") or "FAILED")
        decision.rename_metadata = rename_metadata_for_initial_organization(suggestion)
        return decision

    def _reuse_source_classification_into_working_copy(
        self,
        *,
        source_document: Document,
        source_version: DocumentVersion,
        target_document: Document,
        target_version: DocumentVersion,
        target_filename: str,
        agent_run_id: str | None,
    ) -> InitialOrganizationDecision | None:
        """把最新源侧分类建议转换为工作副本分类运行和首次组织输入。"""

        from app.db.models import DocumentCategorySuggestion, DocumentClassificationRun

        source_run = (
            self.db.query(DocumentClassificationRun)
            .filter(
                DocumentClassificationRun.document_id == source_document.id,
                DocumentClassificationRun.status == "COMPLETED",
            )
            .order_by(DocumentClassificationRun.created_at.desc())
            .first()
        )
        if source_run is None:
            categories: list[dict[str, Any]] = []
        else:
            suggestions = (
                self.db.query(DocumentCategorySuggestion)
                .filter(
                    DocumentCategorySuggestion.classification_run_id == source_run.id,
                    DocumentCategorySuggestion.document_version_id == source_version.id,
                )
                .order_by(
                    DocumentCategorySuggestion.rank.asc(),
                    DocumentCategorySuggestion.confidence.desc(),
                )
                .all()
            )
            categories = [
                self._classification_category_from_suggestion(source_run, item)
                for item in suggestions
            ]
        if agent_run_id and categories:
            existing_target_run = (
                self.db.query(DocumentClassificationRun.id)
                .filter(
                    DocumentClassificationRun.agent_run_id == agent_run_id,
                    DocumentClassificationRun.document_id == target_document.id,
                )
                .first()
            )
            if existing_target_run is None:
                persist_document_results_classifications(
                    db=self.db,
                    agent_run_id=agent_run_id,
                    document_results=[
                        {
                            "document_id": target_document.id,
                            "document_version_id": target_version.id,
                            "filename": target_filename,
                            "extraction_status": "COMPLETED",
                            "summary_status": "REUSED",
                            "categories": categories,
                            "source": "managed-source-classification-reuse",
                        }
                    ],
                )
        return InitialOrganizationDecision(
            filename=target_filename,
            extraction_result={"status": "COMPLETED"},
            categories=categories,
            primary_category=categories[0] if categories else None,
            document_summary_id=None,
            classification_summary_id=None,
            summary_status="REUSED",
            rename_status="DISABLED",
            rename_metadata={},
            summary_metadata={},
        )

    @staticmethod
    def _classification_category_from_suggestion(
        classification_run: Any,
        suggestion: Any,
    ) -> dict[str, Any]:
        """从持久化建议恢复统一门槛所需的完整候选特征。"""

        scores = dict(suggestion.candidate_scores_json or {})
        return {
            "name": suggestion.category_name,
            "category_id": suggestion.category_id,
            "category_path": list(suggestion.category_path_json or []),
            "confidence": float(suggestion.confidence or 0),
            "status": suggestion.status,
            "source": suggestion.source,
            "evidence_items": list(suggestion.evidence_json or []),
            "candidate_scores": scores,
            "matched_title_signals": list(scores.get("matched_title_signals") or []),
            "matched_content_signals": list(scores.get("matched_content_signals") or []),
            "negative_signals": list(scores.get("negative_signals") or []),
            "summary_fulltext_agreement": scores.get("summary_fulltext_agreement"),
            "semantic_evidence": dict(suggestion.semantic_evidence_json or {}),
            "taxonomy_key": suggestion.taxonomy_key,
            "taxonomy_version": suggestion.taxonomy_version,
            "classifier_version": classification_run.classifier_version,
            "reused_from_suggestion_id": suggestion.id,
        }

    def _initial_import_changeset(
        self,
        *,
        working_copy_id: str,
        changeset_id: str | None,
    ) -> ChangeSet | None:
        """读取首次隐藏导入的 ChangeSet，支持物化任务幂等重试。"""

        if changeset_id:
            changeset = self.db.get(ChangeSet, changeset_id)
            if changeset is not None:
                return changeset
        return (
            self.db.query(ChangeSet)
            .join(ChangeItem, ChangeItem.changeset_id == ChangeSet.id)
            .filter(
                ChangeItem.target_type == "working_copy",
                ChangeItem.target_id == working_copy_id,
                ChangeItem.change_type == "WORKING_COPY_ORGANIZING",
            )
            .order_by(ChangeSet.created_at.desc())
            .first()
        )

    def _analyze_document_version(self, job: FilesystemJob) -> None:
        """在 ANALYSIS 队列补齐正文解析、摘要、分类和检索索引。"""

        payload = dict(job.payload_json or {})
        managed_file = self.db.get(ManagedFile, str(payload.get("managed_file_id") or ""))
        working_copy = self.db.get(WorkingCopy, str(payload.get("working_copy_id") or ""))
        document = self.db.get(Document, str(payload.get("document_id") or ""))
        version = self.db.get(DocumentVersion, str(payload.get("document_version_id") or ""))
        if (
            managed_file is None
            or working_copy is None
            or document is None
            or version is None
            or working_copy.status not in {"ACTIVE", "ORGANIZING"}
            or working_copy.current_version_id != version.id
            or working_copy.document_id != document.id
        ):
            raise RuntimeError("ANALYZE_DOCUMENT_VERSION 缺少有效工作副本或版本")
        result = self._ensure_existing_working_copy_search_artifacts(
            working_copy=working_copy,
            managed_file=managed_file,
        )
        organization_decision = result.pop("_organization_decision", None)
        if result.get("status") != "READY":
            if working_copy.status == "ORGANIZING":
                changeset, _ = create_lifecycle_audit(
                    db=self.db,
                    user_id=document.user_id,
                    workspace_id=document.workspace_id,
                    conversation_id=self._conversation_for_upload(managed_file),
                    tool_name="document-background-analysis",
                    message_content=(
                        f"文件“{working_copy.filename}”已安全发布；正文暂未成功解析，"
                        "主分类需要确认。"
                    ),
                    change_type="AUTO_ORGANIZATION_REVIEW_REQUIRED",
                    target_type="working_copy",
                    target_id=working_copy.id,
                    target_document_id=document.id,
                    after_value={
                        "working_copy_id": working_copy.id,
                        "analysis_status": result.get("status"),
                        "organization_decision": "NEEDS_REVIEW",
                    },
                    graph_document_results=[],
                    visible_in_conversation=True,
                )
                self._finalize_initial_organization(
                    working_copy=working_copy,
                    managed_file=managed_file,
                    document=document,
                    version=version,
                    organization_decision=None,
                    changeset=changeset,
                    extraction_status="FAILED",
                )
            # 可判定的“不支持/需人工处理”属于业务终态，不应把同一文件无意义重跑三次。
            # 只有实际异常才交给 worker 的最多三次失败重试机制。
            FilesystemJobQueue(self.db).mark_completed(
                job=job,
                result={
                    "working_copy_id": working_copy.id,
                    "document_id": document.id,
                    "document_version_id": version.id,
                    **result,
                },
            )
            return
        pending_decision = None
        category_name = "暂无可靠分类"
        graph_document_results: list[dict[str, Any]] = []
        if isinstance(organization_decision, InitialOrganizationDecision):
            pending_decision = self._initial_organization_pending_decision(
                decision=organization_decision,
                working_copy=working_copy,
            )
            if organization_decision.primary_category is not None:
                category_name = "/".join(
                    str(item)
                    for item in organization_decision.primary_category.get("category_path", [])
                ) or "暂无可靠分类"
            file_receipt = {
                **organization_decision.document_result(
                    document_id=document.id,
                    document_version_id=version.id,
                ),
                "working_copy_id": working_copy.id,
                "filename": working_copy.filename,
                "pending_decision": pending_decision,
            }
            graph_document_results = [file_receipt]

        message_content = (
            self._initial_organization_message(
                filename=working_copy.filename,
                category_name=category_name,
                pending_decision=pending_decision,
            )
            if pending_decision
            else f"文件“{working_copy.filename}”的后台解析与索引已完成。"
        )
        changeset, _ = create_lifecycle_audit(
            db=self.db,
            user_id=document.user_id,
            workspace_id=document.workspace_id,
            conversation_id=self._conversation_for_upload(managed_file),
            tool_name="document-background-analysis",
            message_content=message_content,
            change_type="DOCUMENT_ANALYSIS_COMPLETED",
            target_type="document_version",
            target_id=version.id,
            target_document_id=document.id,
            after_value={
                "working_copy_id": working_copy.id,
                "document_version_id": version.id,
                "index_run_id": result.get("index_run_id"),
                "analysis_status": "READY",
                "pending_decision": pending_decision,
            },
            graph_document_results=graph_document_results,
            # 初次后台整理会保存命名候选和待复核事实，但用户没有明确要求
            # 改名时不应进入普通对话。真正阻断归档的文件名冲突仍可单独展示。
            visible_in_conversation=self._initial_organization_decision_is_user_visible(
                pending_decision
            ),
        )
        if graph_document_results:
            persist_document_results_classifications(
                db=self.db,
                agent_run_id=changeset.agent_run_id,
                document_results=graph_document_results,
            )
        self._finalize_initial_organization(
            working_copy=working_copy,
            managed_file=managed_file,
            document=document,
            version=version,
            organization_decision=(
                organization_decision
                if isinstance(organization_decision, InitialOrganizationDecision)
                else None
            ),
            changeset=changeset,
            extraction_status=(
                str((organization_decision.extraction_result or {}).get("status") or "FAILED")
                if isinstance(organization_decision, InitialOrganizationDecision)
                else "FAILED"
            ),
        )
        if pending_decision and pending_decision.get("reason") == "LOW_CONFIDENCE_RENAME":
            self.db.add(
                FileRenameReviewItem(
                    conversation_id=changeset.conversation_id,
                    agent_run_id=changeset.agent_run_id,
                    user_id=document.user_id,
                    managed_file_id=managed_file.id,
                    document_id=document.id,
                    root_key=self.db.get(ManagedRoot, managed_file.root_id).root_key,
                    original_relative_path=managed_file.relative_path,
                    original_filename=managed_file.filename,
                    source_sha256=managed_file.content_sha256 or version.sha256,
                    status="NEEDS_REVIEW",
                    review_context_json=pending_decision,
                    decision_json={},
                )
            )
        self.db.add(
            ChangeItem(
                changeset_id=changeset.id,
                target_type="document_index_run",
                target_id=str(result.get("index_run_id") or version.id),
                target_document_id=document.id,
                change_type="DOCUMENT_INDEX_CREATED",
                before_value_json={},
                after_value_json={
                    "document_version_id": version.id,
                    "index_run_id": result.get("index_run_id"),
                },
                source="document-background-analysis",
                confidence=1.0,
                evidence_json={},
                execution_status="COMPLETED",
            )
        )
        FilesystemJobQueue(self.db).mark_completed(
            job=job,
            result={
                "working_copy_id": working_copy.id,
                "document_id": document.id,
                "document_version_id": version.id,
                **result,
            },
        )

    def _finalize_initial_organization(
        self,
        *,
        working_copy: WorkingCopy,
        managed_file: ManagedFile,
        document: Document,
        version: DocumentVersion,
        organization_decision: InitialOrganizationDecision | None,
        changeset: ChangeSet,
        extraction_status: str,
    ) -> DocumentOrganizationDecision | None:
        """记录 Shadow 决策，或把隐藏副本首次原子发布到分类/中性路径。

        已经是 ``ACTIVE`` 的历史文件只允许 Shadow 回放，绝不能借此方法后台移动；
        只有本次上传创建且仍为 ``ORGANIZING`` 的副本可以绕过 OperationPlan 完成
        一次首次发布。发布后任何路径变化仍由既有高风险操作链路负责。
        """

        actual_placement = bool(
            working_copy.status == "ORGANIZING"
            and self.settings.auto_primary_classification_enabled
            and self.settings.auto_initial_placement_enabled
            and not self.settings.auto_classification_shadow_mode
        )
        shadow_only = not actual_placement
        if not actual_placement and not self.settings.auto_classification_shadow_mode:
            return None

        categories = organization_decision.categories if organization_decision is not None else []
        risk_status = self._initial_organization_risk_status(
            managed_file,
            version=version,
        )
        image_date_label = self._uploaded_image_date_label(
            managed_file=managed_file,
        )
        managed_source_image_date_label = self._managed_source_image_date_label(
            managed_file=managed_file,
            version=version,
        )
        image_date_rule_applied = bool(
            image_date_label and risk_status in {"PASS", "WARNING"}
        )
        image_date_source = IMAGE_DATE_RELATION_SOURCE
        image_date_classifier_version = IMAGE_DATE_CLASSIFIER_VERSION
        image_date_metadata_type = "upload_metadata"
        image_date_metadata_key = "upload_date"
        image_date_evidence_quote = "按图片上传日期自动归档"
        policy_result = None
        if not image_date_rule_applied:
            policy_result = AutoPlacementPolicy(self.settings).evaluate(
                categories=categories,
                extraction_status=extraction_status,
                risk_passed=risk_status in {"PASS", "WARNING"},
            )
            if (
                managed_source_image_date_label
                and risk_status in {"PASS", "WARNING"}
                and self._managed_source_image_date_fallback_needed(
                    categories=categories,
                    policy_result=policy_result,
                )
            ):
                image_date_label = managed_source_image_date_label
                image_date_rule_applied = True
                image_date_source = MANAGED_SOURCE_MODIFIED_DATE_RELATION_SOURCE
                image_date_classifier_version = (
                    MANAGED_SOURCE_MODIFIED_DATE_CLASSIFIER_VERSION
                )
                image_date_metadata_type = "managed_source_metadata"
                image_date_metadata_key = "modified_date"
                image_date_evidence_quote = "按受管源文件修改日期自动归档"
        if image_date_rule_applied:
            category_path = image_date_category_path(str(image_date_label))
            policy_result = AutoPlacementPolicyResult(
                accepted=True,
                primary_category={
                    "category_id": IMAGE_DATE_CATEGORY_ROOT_ID,
                    "category_path": category_path,
                    "name": str(image_date_label),
                    "source": image_date_source,
                },
                reason_codes=(),
                calibrated_confidence=1.0,
                required_threshold=0.0,
                top_margin=1.0,
                required_margin=0.0,
                feature_snapshot={
                    "placement_rule": image_date_source,
                    image_date_metadata_key: image_date_label,
                    "content_rule": "verified_image_container",
                    "semantic_college_detection_skipped": True,
                },
            )
        else:
            assert policy_result is not None
        repository = OrganizationDecisionRepository(self.db)
        classification_run, primary_suggestion = repository.latest_classification(
            agent_run_id=changeset.agent_run_id,
            document_id=document.id,
        )
        reasons = list(policy_result.reason_codes)
        target_relative_path: str | None = None
        working_root = self.db.get(WorkingCopyRoot, working_copy.working_copy_root_id)
        if working_root is None:
            raise RuntimeError("首次分类整理缺少工作副本根")

        if image_date_rule_applied:
            target_filename = self._initial_organization_filename(
                decision=organization_decision,
                fallback=working_copy.filename,
            )
            target_relative_path = self._available_initial_image_relative_path(
                working_copy=working_copy,
                working_root=working_root,
                managed_file=managed_file,
                version=version,
                date_label=str(image_date_label),
                target_filename=target_filename,
            )
        elif policy_result.accepted and classification_run is not None and primary_suggestion is not None:
            try:
                target = CategoryOrganizationPathResolver(self.storage).resolve_category(
                    category_id=primary_suggestion.category_id,
                    taxonomy_key=classification_run.taxonomy_key,
                    taxonomy_version=classification_run.taxonomy_version,
                    working_copy=working_copy,
                    working_root=working_root,
                )
                target_filename = self._initial_organization_filename(
                    decision=organization_decision,
                    fallback=working_copy.filename,
                )
                target_parent = (
                    Path(target.target_relative_path).parent
                    / self._managed_source_container_path(managed_file)
                )
                if self._is_personal_resume_rename(organization_decision):
                    target_relative_path = self._available_initial_resume_relative_path(
                        working_copy=working_copy,
                        working_root=working_root,
                        version=version,
                        target_parent=target_parent,
                        target_filename=target_filename,
                    )
                    target_filename = Path(target_relative_path).name
                    organization_decision.rename_metadata["proposed_filename"] = target_filename
                else:
                    target_relative_path = (target_parent / target_filename).as_posix()
                target_storage_path = f"{working_root.relative_storage_path}/{target_relative_path}"
                target_path = self.storage.working_copy_path(target_storage_path)
                staged_path = self.storage.working_copy_path(version.storage_path)
                retry_after_publish = (
                    not staged_path.exists()
                    and target_path.is_file()
                    and self.storage.sha256_file(target_path) == version.sha256
                )
                if (
                    not self._is_personal_resume_rename(organization_decision)
                    and target_path.exists()
                    and not retry_after_publish
                ):
                    dated_filename = self._full_date_collision_filename(
                        decision=organization_decision,
                        filename=target_filename,
                    )
                    if dated_filename:
                        dated_relative_path = (
                            target_parent / dated_filename
                        ).as_posix()
                        dated_storage_path = (
                            f"{working_root.relative_storage_path}/{dated_relative_path}"
                        )
                        dated_path = self.storage.working_copy_path(dated_storage_path)
                        retry_after_dated_publish = (
                            not staged_path.exists()
                            and dated_path.is_file()
                            and self.storage.sha256_file(dated_path) == version.sha256
                        )
                        if not dated_path.exists() or retry_after_dated_publish:
                            target_filename = dated_filename
                            target_relative_path = dated_relative_path
                            if organization_decision is not None:
                                organization_decision.rename_metadata["proposed_filename"] = dated_filename
                        else:
                            reasons.append("TARGET_NAME_CONFLICT")
                    else:
                        reasons.append("TARGET_NAME_CONFLICT")
            except CategoryOrganizationPathError:
                reasons.append("TARGET_PATH_UNAVAILABLE")
        elif policy_result.accepted:
            reasons.append("NO_TAXONOMY_CANDIDATE")

        reasons = list(dict.fromkeys(reasons))
        evaluated_decision = "AUTO_ORGANIZED" if not reasons else "NEEDS_REVIEW"
        if shadow_only:
            taxonomy = load_default_taxonomy() if image_date_rule_applied else None
            return repository.create_or_update_decision(
                working_copy=working_copy,
                classification_run=classification_run,
                primary_suggestion=(
                    None if image_date_rule_applied else primary_suggestion
                ),
                policy_result=policy_result,
                policy_version=self.settings.auto_classification_policy_version,
                calibration_version=self.settings.auto_classification_calibration_version,
                decision=evaluated_decision,
                reason_codes=reasons,
                target_relative_path=target_relative_path if not reasons else None,
                shadow_only=True,
                decision_scope=(
                    "initial-image-date-organization"
                    if image_date_rule_applied
                    else "initial-organization"
                ),
                category_id_override=(
                    IMAGE_DATE_CATEGORY_ROOT_ID if image_date_rule_applied else None
                ),
                taxonomy_key_override=taxonomy.key if taxonomy is not None else None,
                taxonomy_version_override=(
                    taxonomy.version if taxonomy is not None else None
                ),
                classifier_version_override=(
                    image_date_classifier_version if image_date_rule_applied else None
                ),
            )

        if not reasons and target_relative_path:
            final_relative_path = target_relative_path
            operation_type = (
                "INITIAL_IMAGE_DATE_PLACEMENT"
                if image_date_rule_applied
                else "INITIAL_AUTO_PLACEMENT"
            )
        else:
            neutral = self._neutral_initial_path_resolution(
                working_root=working_root,
                managed_file=managed_file,
                version=version,
            )
            target_filename = self._initial_organization_filename(
                decision=organization_decision,
                fallback=neutral.filename,
            )
            final_relative_path = (
                Path(neutral.relative_path).parent / target_filename
            ).as_posix()
            operation_type = "INITIAL_NEUTRAL_PLACEMENT"

        final_storage_path = f"{working_root.relative_storage_path}/{final_relative_path}"
        self.storage.publish_working_copy(
            staged_relative_path=version.storage_path,
            target_relative_path=final_storage_path,
            expected_sha256=version.sha256,
            staged_hash_verified=False,
        )
        before_relative_path = managed_file.relative_path
        working_copy.relative_path = final_relative_path
        working_copy.relative_path_hash = hashlib.sha256(
            final_relative_path.encode("utf-8")
        ).hexdigest()
        working_copy.filename = Path(final_relative_path).name
        working_copy.extension = Path(final_relative_path).suffix.lower()
        working_copy.status = "ACTIVE"
        version.storage_path = final_storage_path
        version.filename = working_copy.filename
        document.ingest_status = "INDEXED" if extraction_status == "COMPLETED" else "INGESTED"
        file_object = (
            self.db.query(FileObject)
            .filter(
                FileObject.document_id == document.id,
                FileObject.storage_backend == "working_copy_local",
            )
            .order_by(FileObject.created_at.desc())
            .first()
        )
        if file_object is not None:
            file_object.storage_path = final_storage_path

        path_record = WorkingCopyPathRecord(
            working_copy_id=working_copy.id,
            sequence_number=1,
            operation_type=operation_type,
            before_relative_path=before_relative_path,
            after_relative_path=final_relative_path,
            before_filename=managed_file.filename,
            after_filename=working_copy.filename,
            document_version_id=version.id,
            content_sha256=version.sha256,
            agent_run_id=changeset.agent_run_id,
            changeset_id=changeset.id,
            status="COMPLETED",
            executed_by=document.user_id,
        )
        self.db.add(path_record)
        self.db.flush()

        taxonomy = load_default_taxonomy() if image_date_rule_applied else None
        decision_row = repository.create_or_update_decision(
            working_copy=working_copy,
            classification_run=classification_run,
            primary_suggestion=(None if image_date_rule_applied else primary_suggestion),
            policy_result=policy_result,
            policy_version=self.settings.auto_classification_policy_version,
            calibration_version=self.settings.auto_classification_calibration_version,
            decision=evaluated_decision,
            reason_codes=reasons,
            target_relative_path=final_relative_path,
            shadow_only=False,
            decision_scope=(
                "initial-image-date-organization"
                if image_date_rule_applied
                else "initial-organization"
            ),
            category_id_override=(
                IMAGE_DATE_CATEGORY_ROOT_ID if image_date_rule_applied else None
            ),
            taxonomy_key_override=taxonomy.key if taxonomy is not None else None,
            taxonomy_version_override=(taxonomy.version if taxonomy is not None else None),
            classifier_version_override=(
                image_date_classifier_version if image_date_rule_applied else None
            ),
        )
        decision_row.path_record_id = path_record.id

        applied_relation = None
        if evaluated_decision == "AUTO_ORGANIZED" and image_date_rule_applied and taxonomy:
            applied_relation = repository.create_system_primary(
                working_copy=working_copy,
                category_id=IMAGE_DATE_CATEGORY_ROOT_ID,
                category_path=image_date_category_path(str(image_date_label)),
                taxonomy_key=taxonomy.key,
                taxonomy_version=taxonomy.version,
                classifier_version=image_date_classifier_version,
                source=image_date_source,
                evidence=[
                    {
                        "type": image_date_metadata_type,
                        "source": image_date_source,
                        image_date_metadata_key: image_date_label,
                        "quote": image_date_evidence_quote,
                    }
                ],
            )
        elif (
            evaluated_decision == "AUTO_ORGANIZED"
            and classification_run
            and primary_suggestion
        ):
            applied_relation = repository.create_auto_applied_primary(
                working_copy=working_copy,
                classification_run=classification_run,
                suggestion=primary_suggestion,
            )
        if applied_relation is not None:
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="document_category",
                    target_id=applied_relation.category_id,
                    target_document_id=document.id,
                    change_type="CATEGORY_AUTO_APPLIED",
                    before_value_json={},
                    after_value_json={
                        "working_copy_id": working_copy.id,
                        "category_id": applied_relation.category_id,
                        "category_path": list(
                            applied_relation.category_path_json or []
                        ),
                        "status": "AUTO_APPLIED",
                    },
                    source=(
                        image_date_source
                        if image_date_rule_applied
                        else "auto_placement_policy"
                    ),
                    confidence=policy_result.calibrated_confidence,
                    evidence_json={
                        "items": list(applied_relation.evidence_json or [])
                    },
                    execution_status="COMPLETED",
                )
            )
        if working_copy.filename != managed_file.filename:
            self.db.add(
                ChangeItem(
                    changeset_id=changeset.id,
                    target_type="working_copy",
                    target_id=working_copy.id,
                    target_document_id=document.id,
                    change_type="FILENAME_CHANGED",
                    before_value_json={"filename": managed_file.filename},
                    after_value_json={
                        "filename": working_copy.filename,
                        "managed_original_unchanged": True,
                    },
                    source="initial_upload_organization",
                    confidence=1.0,
                    evidence_json={
                        "rename_status": organization_decision.rename_status
                        if organization_decision is not None
                        else "NO_CHANGE"
                    },
                    execution_status="COMPLETED",
                )
            )
        self.db.add(
            ChangeItem(
                changeset_id=changeset.id,
                target_type="working_copy",
                target_id=working_copy.id,
                target_document_id=document.id,
                change_type=(
                    "WORKING_COPY_AUTO_ORGANIZED"
                    if evaluated_decision == "AUTO_ORGANIZED"
                    else "AUTO_ORGANIZATION_REVIEW_REQUIRED"
                ),
                before_value_json={"relative_path": before_relative_path, "status": "ORGANIZING"},
                after_value_json={
                    "relative_path": final_relative_path,
                    "status": "ACTIVE",
                    "organization_decision": evaluated_decision,
                    "managed_original_unchanged": True,
                },
                source=(
                    image_date_source
                    if image_date_rule_applied
                    else "auto_placement_policy"
                ),
                confidence=policy_result.calibrated_confidence,
                evidence_json={"reason_codes": reasons},
                execution_status="COMPLETED",
            )
        )
        DocumentSearchProfileService(db=self.db).upsert_current_profile(working_copy.id)
        return decision_row

    def _uploaded_image_date_label(
        self,
        *,
        managed_file: ManagedFile,
    ) -> str | None:
        """验证不可变归档中的真实图片容器，并返回原上传版本的本地日期。

        不能只信任浏览器 MIME 或扩展名；伪装成图片的文件仍走普通分类与风险复核。
        外部受管目录图片也不属于“上传图片”规则，避免后台扫描静默移动历史文件。
        """

        upload_version_id = str(managed_file.source_upload_version_id or "")
        if not upload_version_id:
            return None
        upload_version = self.db.get(DocumentVersion, upload_version_id)
        if upload_version is None:
            return None
        archived_path = self.storage.archive_path(managed_file.relative_path)
        if detect_image_content_type(archived_path) is None:
            return None
        return image_upload_date_label(upload_version.created_at)

    def _managed_source_image_date_label(
        self,
        *,
        managed_file: ManagedFile,
        version: DocumentVersion,
    ) -> str | None:
        """返回受管源图片的源文件修改日期，不把上传或复制时间当作分类依据。"""

        if managed_file.source_upload_version_id or managed_file.modified_at is None:
            return None
        staged_path = self.storage.working_copy_path(version.storage_path)
        if detect_image_content_type(staged_path) is None:
            return None
        return image_upload_date_label(managed_file.modified_at)

    @staticmethod
    def _managed_source_image_date_fallback_needed(
        *,
        categories: list[dict[str, Any]],
        policy_result: AutoPlacementPolicyResult,
    ) -> bool:
        """分类未命中具体业务节点时，让受管图片按源修改日期落位。"""

        return not policy_result.accepted or bool(categories) and all(
            str(category.get("source") or "") == "rule_fallback"
            for category in categories
        )

    def _available_initial_image_relative_path(
        self,
        *,
        working_copy: WorkingCopy,
        working_root: WorkingCopyRoot,
        managed_file: ManagedFile,
        version: DocumentVersion,
        date_label: str,
        target_filename: str,
    ) -> str:
        """在同一日期目录内分配不覆盖既有副本的首次发布路径。

        文件夹上传可能包含多个同名图片；确定性版本后缀保证单个冲突不会把图片
        放回中性目录。该分配只用于首次发布，活动副本后续改名仍须走 OperationPlan。
        """

        parent = (
            Path(*image_date_category_path(date_label))
            / self._managed_source_container_path(managed_file)
        )
        sanitized = self.storage.sanitize_filename(target_filename)
        suffix = Path(sanitized).suffix
        stem = sanitized[: -len(suffix)] if suffix else sanitized
        staged_path = self.storage.working_copy_path(version.storage_path)
        for ordinal in range(1, 1000):
            version_marker = f"_第{ordinal}版"
            filename = (
                sanitized
                if ordinal == 1
                else f"{stem[: max(1, 240 - len(version_marker) - len(suffix))]}"
                f"{version_marker}{suffix}"
            )
            relative_path = (parent / filename).as_posix()
            indexed_conflict = (
                self.db.query(WorkingCopy.id)
                .filter(
                    WorkingCopy.working_copy_root_id == working_root.id,
                    WorkingCopy.relative_path == relative_path,
                    WorkingCopy.id != working_copy.id,
                    WorkingCopy.status.in_(["ACTIVE", "ORGANIZING", "IMPORTING"]),
                )
                .first()
                is not None
            )
            target_path = self.storage.working_copy_path(
                f"{working_root.relative_storage_path}/{relative_path}"
            )
            if not indexed_conflict and not target_path.exists():
                return relative_path
            if (
                not indexed_conflict
                and not staged_path.exists()
                and target_path.is_file()
                and self.storage.sha256_file(target_path) == version.sha256
            ):
                # 文件系统发布成功、数据库事务尚未提交时，重试必须复用原目标。
                return relative_path
        raise RuntimeError("图片日期目录无法分配可用文件名")

    @staticmethod
    def _managed_source_container_path(managed_file: ManagedFile) -> Path:
        """仅为外部受管源保留源文件在受管根下的父目录。"""

        if managed_file.source_upload_version_id:
            return Path()
        relative_path = Path(str(managed_file.relative_path or "").replace("\\", "/"))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return Path()
        parent = relative_path.parent
        return Path() if parent == Path(".") else parent

    def _initial_organization_filename(
        self,
        *,
        decision: InitialOrganizationDecision | None,
        fallback: str,
    ) -> str:
        """仅把已通过命名校验的候选用于首次工作副本发布。"""

        if decision is None or decision.rename_status != "READY":
            return self.storage.sanitize_filename(fallback)
        proposed = str(decision.rename_metadata.get("proposed_filename") or "").strip()
        if not proposed:
            return self.storage.sanitize_filename(fallback)
        sanitized = self.storage.sanitize_filename(proposed)
        if Path(sanitized).suffix.lower() != Path(fallback).suffix.lower():
            return self.storage.sanitize_filename(fallback)
        return sanitized

    @staticmethod
    def _is_personal_resume_rename(
        decision: InitialOrganizationDecision | None,
    ) -> bool:
        """仅识别共享命名服务生成的个人简历专用建议。"""

        return bool(
            decision is not None
            and decision.rename_status == "READY"
            and decision.rename_metadata.get("template_key") == "personal_resume"
        )

    def _available_initial_resume_relative_path(
        self,
        *,
        working_copy: WorkingCopy,
        working_root: WorkingCopyRoot,
        version: DocumentVersion,
        target_parent: Path,
        target_filename: str,
    ) -> str:
        """同一最终目录中的个人简历重名时分配稳定版本后缀，绝不覆盖。"""

        suffix = Path(target_filename).suffix
        stem = target_filename[: -len(suffix)] if suffix else target_filename
        labels = {
            2: "二",
            3: "三",
            4: "四",
            5: "五",
            6: "六",
            7: "七",
            8: "八",
            9: "九",
            10: "十",
        }
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtext(:target_parent), hashtext(:target_filename)"
                    ")"
                ),
                {
                    "target_parent": (
                        f"{working_root.id}:{target_parent.as_posix()}"
                    ),
                    "target_filename": target_filename.casefold(),
                },
            )
        staged_path = self.storage.working_copy_path(version.storage_path)
        for ordinal in range(1, 1000):
            label = labels.get(ordinal, str(ordinal))
            filename = target_filename if ordinal == 1 else f"{stem}_第{label}版{suffix}"
            relative_path = (target_parent / filename).as_posix()
            indexed = (
                self.db.query(WorkingCopy.id)
                .filter(
                    WorkingCopy.working_copy_root_id == working_root.id,
                    WorkingCopy.relative_path == relative_path,
                    WorkingCopy.id != working_copy.id,
                    WorkingCopy.status.in_(["ACTIVE", "ORGANIZING", "IMPORTING"]),
                )
                .first()
                is not None
            )
            target_path = self.storage.working_copy_path(
                f"{working_root.relative_storage_path}/{relative_path}"
            )
            if not indexed and not target_path.exists():
                return relative_path
            if (
                not indexed
                and not staged_path.exists()
                and target_path.is_file()
                and self.storage.sha256_file(target_path) == version.sha256
            ):
                return relative_path
        raise RuntimeError("个人简历目录无法分配可用文件名")

    @staticmethod
    def _full_date_collision_filename(
        *,
        decision: InitialOrganizationDecision | None,
        filename: str,
    ) -> str | None:
        """年份标准名冲突且完整日期可靠时，升级为年月日标准名。"""

        if decision is None or decision.rename_status != "READY":
            return None
        document_date = str(decision.rename_metadata.get("document_date") or "")
        year = str(decision.rename_metadata.get("year") or "")
        if len(document_date) != 8 or not document_date.isdigit():
            return None
        if not year or not filename.startswith(f"{year}_"):
            return None
        return replace_year_prefix_with_date(
            filename=filename,
            year=year,
            document_date=document_date,
            separator="_",
        )

    def _initial_organization_risk_status(
        self,
        managed_file: ManagedFile,
        *,
        version: DocumentVersion | None = None,
    ) -> str:
        """读取上传风险结论，或验证外部源修订已完成受控只读分析。"""

        if not managed_file.source_upload_version_id:
            revision_id = str(
                version.source_managed_file_revision_id if version is not None else ""
            )
            revision = self.db.get(ManagedFileRevision, revision_id) if revision_id else None
            if (
                revision is not None
                and revision.is_current
                and revision.status == "READY"
                and revision.analysis_status == "READY"
                and revision.content_sha256 == managed_file.content_sha256
            ):
                return "PASS"
            return "UNKNOWN"
        archive = self.repository.get_archive_by_version(managed_file.source_upload_version_id)
        if archive is None:
            return "UNKNOWN"
        return str((archive.risk_assessment_json or {}).get("status") or "UNKNOWN")

    def _ensure_existing_working_copy_search_artifacts(
        self,
        *,
        working_copy: WorkingCopy,
        managed_file: ManagedFile,
    ) -> dict[str, Any]:
        """为历史幂等工作副本补齐解析索引和瘦检索投影。

        该修复只写可重建派生数据，不移动、覆盖或改名工作副本。已有成功索引会
        直接复用；缺少解析结果时才运行本地确定性解析与分类链路。
        """

        artifact_status = working_copy_search_artifact_status(self.db, working_copy)
        if artifact_status["ready"]:
            return {"status": "READY", "reused": True}
        document = self.db.get(Document, working_copy.document_id)
        version = self.db.get(DocumentVersion, working_copy.current_version_id)
        if document is None or version is None:
            log_event(
                "working_copy.search_repair.failed",
                level="ERROR",
                document_id=working_copy.document_id,
                status="FAILED",
                error_code="WORKING_COPY_LINEAGE_MISSING",
                working_copy_id=working_copy.id,
                document_version_id=working_copy.current_version_id,
                message="历史工作副本缺少当前文档或版本，无法修复检索索引",
            )
            raise RuntimeError("历史工作副本缺少当前 Document 或 DocumentVersion，无法修复检索索引")
        log_event(
            "working_copy.search_repair.started",
            document_id=document.id,
            status="RUNNING",
            working_copy_id=working_copy.id,
            document_version_id=version.id,
            profile_ready=artifact_status["profile_ready"],
            index_ready=artifact_status["index_ready"],
            message="历史工作副本检索派生数据补建开始",
        )

        organization_decision = None
        completed_index = (
            self.db.query(DocumentIndexRun)
            .filter(
                DocumentIndexRun.document_version_id == version.id,
                DocumentIndexRun.status == "COMPLETED",
                DocumentIndexRun.index_version == INDEX_VERSION,
            )
            .order_by(DocumentIndexRun.updated_at.desc())
            .first()
        )
        index_result: dict[str, Any] = {
            "ok": True,
            "status": "COMPLETED",
            "reused": True,
            "index_run_id": completed_index.id if completed_index is not None else None,
        }
        if completed_index is None:
            reusable = FileExtractionRepository(
                self.db,
                user_id=document.user_id,
            ).get_latest_successful_extraction(document_id=document.id)
            extraction_run_id = str(reusable["run"].id) if reusable is not None else ""
            if extraction_run_id:
                log_event(
                    "working_copy.search_repair.extraction_reused",
                    document_id=document.id,
                    status="COMPLETED",
                    working_copy_id=working_copy.id,
                    document_version_id=version.id,
                    extraction_run_id=extraction_run_id,
                    message="复用已有成功解析结果补建正文索引",
                )
            extraction_error: dict[str, Any] = {}
            if not extraction_run_id and not artifact_status.get("repair_blocked"):
                log_event(
                    "working_copy.search_repair.extraction_started",
                    document_id=document.id,
                    status="RUNNING",
                    working_copy_id=working_copy.id,
                    document_version_id=version.id,
                    message="未找到可复用解析结果，开始本地确定性解析",
                )
                try:
                    decision = InitialWorkingCopyOrganizer(
                        db=self.db,
                        user_id=document.user_id,
                        settings=self.settings,
                    ).decide(
                        document=document,
                        version=version,
                        managed_file=managed_file,
                    )
                    organization_decision = decision
                except Exception as exc:
                    log_event(
                        "working_copy.search_repair.extraction_failed",
                        level="ERROR",
                        document_id=document.id,
                        status="FAILED",
                        error_code=exc.__class__.__name__,
                        working_copy_id=working_copy.id,
                        document_version_id=version.id,
                        message="历史工作副本本地解析失败",
                    )
                    raise
                extraction_result = (
                    decision.extraction_result
                    if isinstance(decision.extraction_result, dict)
                    else {}
                )
                if extraction_result.get("status") == "COMPLETED":
                    extraction_run_id = str(
                        extraction_result.get("extraction_run_id") or ""
                    )
                else:
                    extraction_error = (
                        dict(extraction_result.get("error") or {})
                        if isinstance(extraction_result.get("error"), dict)
                        else {}
                    )
            elif artifact_status.get("repair_blocked"):
                extraction_error = {
                    "code": "EXTRACTION_REQUIRES_REPROCESS",
                    "message": "当前文档版本已有解析失败记录，等待显式重处理。",
                }
            if extraction_run_id:
                log_event(
                    "working_copy.search_repair.index_started",
                    document_id=document.id,
                    status="RUNNING",
                    working_copy_id=working_copy.id,
                    document_version_id=version.id,
                    extraction_run_id=extraction_run_id,
                    message="历史工作副本正文 Chunk 索引补建开始",
                )
                index_result = DocumentIndexService(
                    db=self.db,
                    settings=self.settings,
                ).build(
                    document_id=document.id,
                    document_version_id=version.id,
                    extraction_run_id=extraction_run_id,
                )
            else:
                index_result = {
                    "ok": False,
                    "status": "FAILED",
                    "error": {
                        "code": str(
                            extraction_error.get("code")
                            or "EXTRACTION_NOT_READY"
                        ),
                        "message": str(
                            extraction_error.get("message")
                            or "历史工作副本未生成可用于正文检索的解析结果。"
                        ),
                    },
                }

        # 即使正文解析失败，也必须补齐文件名/摘要投影，使用户至少能够按文件名查找。
        log_event(
            "working_copy.search_repair.profile_started",
            document_id=document.id,
            status="RUNNING",
            working_copy_id=working_copy.id,
            document_version_id=version.id,
            message="历史工作副本文件级检索投影补建开始",
        )
        try:
            DocumentSearchProfileService(db=self.db).upsert_current_profile(working_copy.id)
        except Exception as exc:
            log_event(
                "working_copy.search_repair.profile_failed",
                level="ERROR",
                document_id=document.id,
                status="FAILED",
                error_code=exc.__class__.__name__,
                working_copy_id=working_copy.id,
                document_version_id=version.id,
                message="历史工作副本文件级检索投影补建失败",
            )
            raise
        result = {
            "status": "READY" if index_result.get("ok") else "NEEDS_REVIEW",
            "reused": bool(index_result.get("reused")),
            "index_run_id": index_result.get("index_run_id"),
            "error": index_result.get("error"),
            # 仅供同一事务内的 ANALYSIS handler 生成用户待确认回执，写入任务结果前会移除。
            "_organization_decision": organization_decision,
        }
        error = result.get("error") if isinstance(result.get("error"), dict) else {}
        log_event(
            "working_copy.search_repair.completed",
            level="INFO" if result["status"] == "READY" else "WARNING",
            document_id=document.id,
            status=result["status"],
            error_code=error.get("code"),
            working_copy_id=working_copy.id,
            document_version_id=version.id,
            index_run_id=result.get("index_run_id"),
            message="历史工作副本检索派生数据补建完成",
        )
        return result

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
        scored: list[
            tuple[float, WorkingCopy | None, ManagedFile, Document | None, ManagedRoot | None]
        ] = []
        for working_copy, working_root, managed_file, document in rows:
            candidate_path = self.storage.working_copy_path(
                f"{working_root.relative_storage_path}/{working_copy.relative_path}"
            )
            candidate_tokens = _text_tokens(candidate_path, working_copy.filename)
            if not candidate_tokens:
                continue
            score = len(source_tokens & candidate_tokens) / max(1, len(source_tokens | candidate_tokens))
            if score >= self.settings.upload_duplicate_similarity_threshold:
                scored.append((score, working_copy, managed_file, document, None))
        source_rows = (
            self.db.query(ManagedFile, ManagedRoot)
            .join(ManagedRoot, ManagedFile.root_id == ManagedRoot.id)
            .outerjoin(WorkingCopy, WorkingCopy.managed_file_id == ManagedFile.id)
            .filter(
                ManagedFile.status == "ACTIVE",
                WorkingCopy.id.is_(None),
            )
            .filter(~ManagedFile.id.in_(exact_managed_ids) if exact_managed_ids else ManagedFile.id != "")
            .order_by(ManagedFile.updated_at.desc())
            .limit(100)
            .all()
        )
        for managed_file, managed_root in source_rows:
            try:
                candidate_path = resolve_managed_relative_path(
                    root_path=Path(managed_root.container_path),
                    relative_path=managed_file.relative_path,
                )
            except (OSError, ValueError):
                continue
            candidate_tokens = _text_tokens(candidate_path, managed_file.filename)
            if not candidate_tokens:
                continue
            score = len(source_tokens & candidate_tokens) / max(1, len(source_tokens | candidate_tokens))
            if score >= self.settings.upload_duplicate_similarity_threshold:
                scored.append((score, None, managed_file, None, managed_root))
        scored.sort(key=lambda item: item[0], reverse=True)
        candidates: list[UploadDuplicateCandidate] = []
        remaining = self.settings.upload_duplicate_max_candidates - len(exact)
        for offset, (
            score,
            working_copy,
            managed_file,
            candidate_document,
            managed_root,
        ) in enumerate(scored[:remaining], start=1):
            scope = self.repository._candidate_scope(
                review=review,
                working_copy=working_copy,
                candidate_document=candidate_document,
            )
            # 近重复候选与精确候选使用同一共享授权边界，不再比较上传用户。
            accessible = bool(
                working_copy is not None
                and working_copy.workspace_id == get_shared_workspace_id(self.db)
                and working_copy.status == "ACTIVE"
            )
            if working_copy is None:
                summary = {
                    "message": "检测到尚未同步到工作副本的高度相似文件",
                    "filename": managed_file.filename,
                    "managed_root_key": managed_root.root_key if managed_root else None,
                    "managed_relative_path": managed_file.relative_path,
                    "similarity_bucket": _similarity_bucket(score),
                    "file_status": "ACTIVE",
                }
            elif scope == "CROSS_USER":
                summary = {
                    "message": "系统检测到高度相似内容",
                    "similarity_bucket": _similarity_bucket(score),
                }
            else:
                summary = {
                    "message": "检测到共享工作目录中可直接使用的高度相似文件",
                    "filename": working_copy.filename,
                    "relative_path": working_copy.relative_path if accessible else None,
                    "similarity_bucket": _similarity_bucket(score),
                }
            candidate = UploadDuplicateCandidate(
                duplicate_review_id=review.id,
                candidate_managed_file_id=managed_file.id,
                candidate_working_copy_id=working_copy.id if working_copy else None,
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
        same_filename_only = all(item.match_type == "SAME_FILENAME" for item in candidates)
        match_description = "同名文件" if same_filename_only else "相同或高度相似内容"
        content = (
            f"系统检测到“{version.filename}”存在{match_description}。请选择继续上传或取消上传。"
            if cross_user_only
            else f"检测到“{version.filename}”已有{match_description}。请选择继续上传、使用已有文件或取消上传。"
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
        """解析首次目标；后台同步不得因同名事实进入用户可见待确认目录。"""

        candidate = preferred_relative_path or managed_file.relative_path
        storage_candidate = self.storage.working_copy_path(
            f"{working_root.relative_storage_path}/{candidate}"
        )
        # 后台同步不按全局文件名制造冲突；不同目录中的同名文件必须直接导入。
        if not storage_candidate.exists() or (
            storage_candidate.is_file()
            and managed_file.content_sha256
            and self.storage.sha256_file(storage_candidate) == managed_file.content_sha256
        ):
            return InitialWorkingPathResolution(
                relative_path=candidate,
                filename=Path(candidate).name,
            )
        # 只有数据库外文件占用完整目标路径时才进入隐藏隔离位置，不能生成用户可见
        # 的“待确认”目录或同步期确认卡；相关歧义在查询、上传或使用时再提示。
        safe_filename = self.storage.sanitize_filename(managed_file.filename)
        return InitialWorkingPathResolution(
            relative_path=f".internal/import-collisions/{managed_file.id}/{safe_filename}",
            filename=safe_filename,
            storage_collision=True,
        )

    def _neutral_initial_path_resolution(
        self,
        *,
        working_root: WorkingCopyRoot,
        managed_file: ManagedFile,
        version: DocumentVersion,
    ) -> InitialWorkingPathResolution:
        """生成不泄露源分类目录的中性首次路径。

        上传归档继续沿用自身中性上传相对路径；外部受管文件则进入内部中性命名空间，
        并仅通过逻辑 ``NEEDS_REVIEW`` 虚拟节点呈现，不创建用户可见“待确认”目录。
        """

        if version.source_managed_file_revision_id and not managed_file.source_upload_version_id:
            safe_filename = self.storage.sanitize_filename(managed_file.filename)
            return self._working_path_resolution(
                working_root=working_root,
                managed_file=managed_file,
                preferred_relative_path=(
                    f".internal/neutral/{managed_file.id}/{safe_filename}"
                ),
            )
        return self._working_path_resolution(
            working_root=working_root,
            managed_file=managed_file,
            preferred_relative_path=managed_file.relative_path,
        )

    def _gated_initial_placement_enabled(
        self,
        *,
        payload: dict[str, Any],
        managed_file: ManagedFile,
    ) -> bool:
        """判断新上传或已完成源侧分析的外部物化是否进入首次落位链路。"""

        source_materialization = bool(
            payload.get("skip_document_analysis")
            and payload.get("source_managed_file_revision_id")
        )
        return bool(
            (
                (
                    managed_file.source_type == "UPLOAD_ARCHIVE"
                    and not payload.get("skip_document_analysis")
                )
                or source_materialization
            )
            and self.settings.auto_primary_classification_enabled
            and self.settings.auto_initial_placement_enabled
            and not self.settings.auto_classification_shadow_mode
        )

    @staticmethod
    def _initial_organization_pending_decision(
        *,
        decision: InitialOrganizationDecision,
        working_copy: WorkingCopy,
    ) -> dict[str, Any] | None:
        """把低置信度命名转换为普通用户可理解的待复核项。"""

        # 上传后的高可信标准名称会在首次发布时直接应用，不再生成二次改名请求。
        if decision.rename_status in {"READY", "NO_CHANGE"}:
            return None
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
    def _initial_organization_decision_is_user_visible(
        pending_decision: dict[str, Any] | None,
    ) -> bool:
        """只展示会阻断当前归档的决策，隐藏未请求的命名分析。"""

        return bool(
            pending_decision
            and pending_decision.get("type") == "filename_conflict"
        )

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
        if pending_decision and pending_decision.get("type") == "rename_suggestion":
            return str(pending_decision["message"])
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
    """共享工作副本只读查询服务。

    用户身份仍用于认证与审计，但物理副本范围固定为系统共享工作区，不能再按
    ``default_workspace_id`` 过滤而造成用户看到不同的文件集合。
    """

    def __init__(self, db: Session) -> None:
        """注入数据库会话。"""

        self.db = db
        self.repository = FileLifecycleRepository(db)

    def list(self, current_user: User) -> list[WorkingCopyResponse]:
        """列出系统共享工作目录中的工作副本。"""

        shared_workspace_id = get_shared_workspace_id(self.db)
        return [
            self._to_response(copy, root)
            for copy, root in self.repository.list_user_working_copies(
                workspace_id=shared_workspace_id,
                user_id=current_user.id,
            )
        ]

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
        """列出共享工作目录的回收站条目，不返回物理回收站路径。"""

        shared_workspace_id = get_shared_workspace_id(self.db)
        entries = (
            self.db.query(TrashEntry)
            .join(WorkingCopy, WorkingCopy.id == TrashEntry.working_copy_id)
            .filter(TrashEntry.workspace_id == shared_workspace_id)
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
        """解析活动工作副本下载路径；回收站文件必须先恢复。"""

        copy = self._owned(working_copy_id=working_copy_id, current_user=current_user)
        if copy.status != "ACTIVE":
            raise HTTPException(status_code=410, detail="文件已删除，请先恢复。")
        version = self.db.get(DocumentVersion, copy.current_version_id) if copy.current_version_id else None
        document = self.db.get(Document, copy.document_id)
        if version is None or document is None:
            raise HTTPException(status_code=404, detail="Working copy content not found")
        path = FileLifecycleStorageService().working_copy_path(version.storage_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Working copy content not found")
        return path, copy.filename, document.content_type

    def _owned(self, *, working_copy_id: str, current_user: User) -> WorkingCopy:
        """校验工作副本属于唯一共享工作目录。"""

        copy = self.repository.get_user_working_copy(
            working_copy_id=working_copy_id,
            workspace_id=get_shared_workspace_id(self.db),
            user_id=current_user.id,
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
    visible_in_conversation: bool = True,
) -> tuple[ChangeSet, Message]:
    """创建系统生命周期调用的 AgentRun、ToolInvocation、ChangeSet 和逐文件 ChangeItem。

    ``visible_in_conversation=false`` 只隐藏普通聊天投影，不删除或弱化任何审计事实。
    """

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
        role="assistant" if visible_in_conversation else "SYSTEM_AUDIT",
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
