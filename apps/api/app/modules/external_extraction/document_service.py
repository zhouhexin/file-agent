"""WorkBuddy 完整文档提取任务服务。

该服务仅发放固定原文件、验收宿主回传的正文并继续既有归档流程；它绝不调用服务器 OCR、
PDF get_text、Word 解析或 Office 转换来补足宿主结果。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentExtractionConfirmation,
    DocumentExtractionRun,
    DocumentPage,
    DocumentVersion,
    ExternalExtractionTask,
    IngestBatch,
    IngestItem,
    UploadArchiveRecord,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    User,
    utcnow,
)
from app.modules.file_lifecycle.service import UploadLifecycleService, create_lifecycle_audit
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.ingestion.workflow import IngestionWorkflow

from .document_schemas import (
    DOCUMENT_EXTRACTION_CONTRACT_VERSION,
    MAX_DOCUMENT_EXTRACTION_BLOCKS,
    MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES,
    MAX_DOCUMENT_EXTRACTION_TEXT_CHARS,
    DocumentExtractionPreviewRequest,
    DocumentExtractionSubmitRequest,
)
from .service import ExternalExtractionService, _IMAGE_SUFFIXES


DOCUMENT_EXTRACTOR = "workbuddy-external-document"
_SUPPORTED_SUFFIXES = _IMAGE_SUFFIXES | {".pdf", ".doc", ".docx", ".xls", ".xlsx"}


class WorkBuddyDocumentExtractionService:
    """以旧租约和上传生命周期为底座的文档提取 v1 服务。"""

    def __init__(self, db: Session) -> None:
        """每个请求构造独立数据库服务，避免租约和用户上下文跨请求泄漏。"""

        self.db = db
        self.legacy = ExternalExtractionService(db)
        self.settings = self.legacy.settings
        self.storage = self.legacy.storage

    def prepare_for_ingest_item(self, *, item: IngestItem) -> ExternalExtractionTask | None:
        """仅为冻结为 WORKBUDDY_DOCUMENT 的上传版本创建整文件任务。"""

        if not self.settings.workbuddy_document_extraction_enabled or not item.upload_document_version_id:
            return None
        version = self.db.get(DocumentVersion, item.upload_document_version_id)
        batch = self.db.get(IngestBatch, item.batch_id)
        if version is None or batch is None or str((batch.policy_json or {}).get("extraction_profile") or "") != "WORKBUDDY_DOCUMENT":
            return None
        suffix = Path(version.filename).suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            self.legacy._raise(422, "UNSUPPORTED_DOCUMENT_SOURCE", "该附件类型暂不支持 WorkBuddy 文档提取。")
        task = (
            self.db.query(ExternalExtractionTask)
            .filter(
                ExternalExtractionTask.ingest_item_id == item.id,
                ExternalExtractionTask.source_version_id == version.id,
                ExternalExtractionTask.provider_contract_version == DOCUMENT_EXTRACTION_CONTRACT_VERSION,
            )
            .one_or_none()
        )
        if task is not None:
            return task
        task = ExternalExtractionTask(
            ingest_item_id=item.id,
            source_version_id=version.id,
            source_sha256=version.sha256,
            phase="PARSE_STAGING",
            provider_contract_version=DOCUMENT_EXTRACTION_CONTRACT_VERSION,
            page_manifest_json=[],
            source_manifest_json=self._source_manifest(version),
            result_summary_json={},
            status="PENDING",
            error_json={},
        )
        self.db.add(task)
        self.db.flush()
        version.extraction_policy_json = {
            "mode": "WORKBUDDY_DOCUMENT",
            "contract_version": DOCUMENT_EXTRACTION_CONTRACT_VERSION,
            "origin_task_id": task.id,
            "origin_upload_version_id": version.id,
            "source_sha256": version.sha256,
        }
        item.status = "WAITING_EXTERNAL_EXTRACTION"
        item.stage = "PARSE_STAGING"
        item.result_json = {
            **dict(item.result_json or {}),
            "document_extraction_task_id": task.id,
            "content_extraction_contract": DOCUMENT_EXTRACTION_CONTRACT_VERSION,
        }
        item.workflow_revision += 1
        self.db.flush()
        return task

    def get(self, *, task_id: str, current_user: User) -> dict[str, Any]:
        """返回当前用户任务的安全状态投影，不泄漏原文件位置或正文。"""

        task, _, _ = self.legacy._owned_task(task_id=task_id, user_id=current_user.id)
        self._require_document_contract(task)
        return self.to_response(task)

    def claim(self, *, task_id: str, worker_id: str, current_user: User) -> dict[str, Any]:
        """发放与固定源版本绑定的租约，宿主随后必须下载并哈希核验源文件。"""

        task, item, batch = self.legacy._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        self._require_document_contract(task)
        # COMPLETE 是文档契约的终态；旧代码误用 COMPLETED 导致完整结果被再次领取。
        # 已持久化完整运行的旧 CLAIMED 任务也不能再次发放租约，应走受控恢复。
        if task.status in {"COMPLETE", "COMPLETED"} or self._completed_submission_run(task) is not None:
            self.legacy._raise(409, "EXTRACTION_ALREADY_COMPLETED", "文档提取结果已保存；如归档仍在等待，请调用恢复接口。")
        self._require_active_item(item)
        if task.status == "CLAIMED" and task.lease_expires_at and self.legacy._aware(task.lease_expires_at) > utcnow():
            if task.lease_owner != worker_id:
                self.legacy._raise(409, "EXTRACTION_ALREADY_CLAIMED", "任务正在由其他 Worker 处理。")
        token = self.legacy._new_lease_token()
        task.lease_owner = worker_id
        task.lease_token_hash = self.legacy._token_hash(token)
        task.lease_expires_at = utcnow() + self.legacy._lease_duration()
        task.status = "CLAIMED"
        task.attempt_count += 1
        self._record_audit(
            task=task, item=item, batch=batch, user=current_user,
            tool_name="document_extraction_claim", change_type="DOCUMENT_EXTRACTION_CLAIMED",
            status="COMPLETED", summary={"attempt_count": task.attempt_count, "lease_expires_at": task.lease_expires_at.isoformat()},
        )
        self.db.flush()
        return {
            "task_id": task.id,
            "provider_contract_version": DOCUMENT_EXTRACTION_CONTRACT_VERSION,
            "source_version_id": task.source_version_id,
            "source_sha256": task.source_sha256,
            "lease_token": token,
            "lease_expires_at": task.lease_expires_at,
            "source": {
                **dict(task.source_manifest_json or {}),
                "resource_url": f"/api/integrations/v1/document-extraction-tasks/{task.id}/source",
            },
            "limits": {
                "max_text_chars": MAX_DOCUMENT_EXTRACTION_TEXT_CHARS,
                "max_units": 500,
                "max_blocks": MAX_DOCUMENT_EXTRACTION_BLOCKS,
                "max_payload_bytes": MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES,
            },
            "extraction_save_policy": self._save_policy(batch),
            "confirmation_required": self._save_policy(batch) == "CONFIRM_BEFORE_SAVE",
            "confirmation": self._confirmation_projection(task),
        }

    def renew(self, *, task_id: str, worker_id: str, lease_token: str, current_user: User) -> ExternalExtractionTask:
        """续期当前租约；过期 token 不能恢复旧工作者。"""

        task, item, batch = self.legacy._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        self._require_document_contract(task)
        self._require_active_item(item)
        self.legacy._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        task.lease_expires_at = utcnow() + self.legacy._lease_duration()
        self._record_audit(
            task=task, item=item, batch=batch, user=current_user,
            tool_name="document_extraction_renew", change_type="DOCUMENT_EXTRACTION_RENEWED",
            status="COMPLETED", summary={"lease_expires_at": task.lease_expires_at.isoformat()},
        )
        self.db.flush()
        return task

    def source_response(self, *, task_id: str, worker_id: str, lease_token: str, current_user: User) -> FileResponse:
        """只在有效租约内返回固定源版本，文件路径由服务端存储层决定。"""

        task, item, _ = self.legacy._owned_task(task_id=task_id, user_id=current_user.id)
        self._require_document_contract(task)
        self._require_active_item(item)
        self.legacy._validate_lease(task=task, worker_id=worker_id, lease_token=lease_token)
        version = self.db.get(DocumentVersion, task.source_version_id)
        if version is None or version.sha256 != task.source_sha256:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "源文件版本或哈希已经变化。")
        path = self.storage.upload_path(version.storage_path)
        if not path.is_file() or path.stat().st_size != version.size_bytes or self.storage.sha256_file(path) != task.source_sha256:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "源文件不存在或哈希不一致。")
        return FileResponse(path=path, media_type=version.content_type, filename=version.filename)

    def submit(self, *, task_id: str, payload: DocumentExtractionSubmitRequest, current_user: User) -> tuple[ExternalExtractionTask, str, str | None, bool, bool]:
        """校验宿主全文并保存；只有完整结果才能继续现有查重与归档。"""

        if payload.payload_size_bytes() > MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES:
            self.legacy._raise(413, "EXTRACTION_PAYLOAD_TOO_LARGE", "文档提取结果超过请求体限制。")
        task, item, batch = self.legacy._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        self._require_document_contract(task)
        digest = self._submission_digest(payload)
        if task.last_submission_key == payload.submission_key:
            if task.last_submission_digest != digest:
                self.legacy._raise(409, "IDEMPOTENCY_CONFLICT", "同一提交键已经用于不同文档提取结果。")
            # 重放同一固定结果时可修复旧版状态错配，不重新保存正文或再次 OCR。
            if self._completed_submission_run(task) is not None:
                self._resume_completed_submission(task=task, item=item, batch=batch, user=current_user)
            ok = task.status in {"COMPLETE", "COMPLETED"}
            return task, item.stage, item.current_job_id, True, ok
        self._require_active_item(item)
        self.legacy._validate_lease(task=task, worker_id=payload.worker_id, lease_token=payload.lease_token)
        confirmation = self._require_confirmed_result(
            task=task,
            batch=batch,
            result_digest=digest,
        )
        if payload.source_sha256 != task.source_sha256 or str(payload.source_version_id) != task.source_version_id:
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "文档提取结果对应的源版本或哈希不一致。")
        version = self.db.get(DocumentVersion, task.source_version_id)
        document = self.db.get(Document, version.document_id) if version else None
        if version is None or document is None:
            self.legacy._raise(409, "SOURCE_VERSION_MISSING", "文档提取源版本不存在。")
        self._validate_unit_scope(payload=payload, manifest=dict(task.source_manifest_json or {}))
        run = self._persist_result(document=document, version=version, task=task, payload=payload, user_id=current_user.id)
        task.extraction_run_id = run.id
        task.last_submission_key = payload.submission_key
        task.last_submission_digest = digest
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.status = payload.coverage.status
        task.result_summary_json = {
            "coverage": payload.coverage.model_dump(mode="json"),
            "provider_name": payload.provider_name,
            "provider_version": payload.provider_version,
            "unit_count": len(payload.units),
            "read_unit_count": sum(unit.status == "READ" for unit in payload.units),
        }
        task.error_json = payload.error.model_dump() if payload.error else {}
        if confirmation is not None:
            confirmation.status = "SAVED"
            confirmation.saved_at = utcnow()
        item.extraction_run_id = run.id
        item.result_json = {
            **dict(item.result_json or {}),
            "document_extraction_status": task.status,
            "document_extraction_unit_count": len(payload.units),
            "content_extraction_contract": DOCUMENT_EXTRACTION_CONTRACT_VERSION,
        }
        item.workflow_revision += 1
        if payload.coverage.status != "COMPLETE":
            item.status = "WAITING_EXTERNAL_EXTRACTION"
            item.stage = "PARSE_STAGING"
            IngestionWorkflow(self.db).synchronize_batch(batch)
            batch.result_revision += 1
            self._record_audit(
                task=task, item=item, batch=batch, user=current_user,
                tool_name="document_extraction_submit", change_type="DOCUMENT_PROCESSING_FAILED",
                status="FAILED", summary=self._audit_summary(task=task, payload=payload, run=run),
            )
            self.db.flush()
            return task, item.stage, None, False, False
        result = self._continue_complete_result(task=task, item=item, batch=batch, version=version)
        changeset = self._record_audit(
            task=task, item=item, batch=batch, user=current_user,
            tool_name="document_extraction_submit", change_type="TEXT_EXTRACTED",
            status="COMPLETED", summary=self._audit_summary(task=task, payload=payload, run=run),
        )
        from app.db.models import ChangeItem

        self.db.add(ChangeItem(
            changeset_id=changeset.id, target_type="DOCUMENT", target_id=document.id,
            target_document_id=document.id, change_type="DOCUMENT_PAGES_CREATED",
            after_value_json={"extraction_run_id": run.id, "unit_count": len(payload.units)},
            source="document_extraction_submit", confidence=1.0, evidence_json={}, execution_status="COMPLETED",
        ))
        self.db.flush()
        return result

    def resume(self, *, task_id: str, current_user: User) -> tuple[ExternalExtractionTask, str, str | None, bool]:
        """只恢复已持久化且绑定固定源的完整提交；不重读文件、不重复写正文。"""

        task, item, batch = self.legacy._owned_task(task_id=task_id, user_id=current_user.id, for_update=True)
        self._require_document_contract(task)
        if self._completed_submission_run(task) is None:
            self.legacy._raise(409, "EXTRACTION_RECOVERY_NOT_AVAILABLE", "没有可验证的完整已提交结果，不能恢复。")
        resumed = self._resume_completed_submission(task=task, item=item, batch=batch, user=current_user)
        return task, item.stage, item.current_job_id, resumed

    def _completed_submission_run(self, task: ExternalExtractionTask) -> DocumentExtractionRun | None:
        """验证旧任务已有真实完整运行，不能仅凭 coverage 或提交键宣称完成。"""

        if not task.extraction_run_id or not task.last_submission_key or not task.last_submission_digest:
            return None
        coverage = (task.result_summary_json or {}).get("coverage") or {}
        if not isinstance(coverage, dict) or coverage.get("status") != "COMPLETE":
            return None
        version = self.db.get(DocumentVersion, task.source_version_id)
        if version is None or version.sha256 != task.source_sha256:
            return None
        run = self.db.get(DocumentExtractionRun, task.extraction_run_id)
        if (run is None or run.document_id != version.document_id or run.document_version_id != version.id
                or run.extractor != DOCUMENT_EXTRACTOR or run.status != "COMPLETED"):
            return None
        if not self.db.query(DocumentPage).filter_by(extraction_run_id=run.id).first():
            return None
        return run

    def completed_result_available(self, task: ExternalExtractionTask) -> bool:
        """仅在文档契约终态和固定来源的完整运行同时成立时允许后续归档。"""

        return (
            task.provider_contract_version == DOCUMENT_EXTRACTION_CONTRACT_VERSION
            and task.status == "COMPLETE"
            and self._completed_submission_run(task) is not None
        )

    def _resume_completed_submission(self, *, task, item, batch, user) -> bool:
        """在锁内仅推进仍待外部提取的条目；已推进的归档任务保持原 job 不重复创建。"""

        changed = task.status != "COMPLETE" or bool(task.lease_owner or task.lease_token_hash or task.lease_expires_at)
        task.status = "COMPLETE"
        task.lease_owner = None
        task.lease_token_hash = None
        task.lease_expires_at = None
        if item.status == "WAITING_EXTERNAL_EXTRACTION" and item.stage == "PARSE_STAGING":
            version = self.db.get(DocumentVersion, task.source_version_id)
            item.extraction_run_id = task.extraction_run_id
            item.result_json = {**dict(item.result_json or {}), "document_extraction_status": "COMPLETE"}
            item.workflow_revision += 1
            if not self._resume_confirmed_near_duplicate(task=task, item=item, batch=batch):
                self._continue_complete_result(task=task, item=item, batch=batch, version=version)
            changed = True
        if changed:
            self._record_audit(
                task=task, item=item, batch=batch, user=user,
                tool_name="document_extraction_resume", change_type="DOCUMENT_EXTRACTION_RECOVERED",
                status="COMPLETED", summary={"task_id": task.id, "extraction_run_id": task.extraction_run_id,
                                             "next_stage": item.stage, "filesystem_job_id": item.current_job_id},
            )
            self.db.flush()
        return changed

    def _resume_confirmed_near_duplicate(self, *, task: ExternalExtractionTask, item: IngestItem, batch: IngestBatch) -> bool:
        """复用用户已确认的近似查重决定；仅补建缺失的归档任务，不重新判重或改写正文。"""

        review = self.db.query(UploadDuplicateReview).filter_by(ingest_item_id=item.id).with_for_update().one_or_none()
        if not review or review.status != "RESOLVED" or review.comparison_phase != "NEAR":
            return False
        if review.decision != "CONTINUE_UPLOAD" or item.decision != "CONTINUE_UPLOAD":
            return False
        archive = self.db.query(UploadArchiveRecord).filter_by(id=item.archive_record_id).with_for_update().one_or_none()
        if (archive is None or review.upload_document_version_id != task.source_version_id
                or archive.upload_document_version_id != task.source_version_id
                or item.upload_document_version_id != task.source_version_id
                or archive.status != "WAITING_EXTERNAL_EXTRACTION" or archive.filesystem_job_id is not None):
            self.legacy._raise(409, "EXTRACTION_RECOVERY_CONFLICT", "已确认查重的归档状态已变化，不能自动恢复。")
        archive_job = UploadLifecycleService(self.db).enqueue_archive_after_external_extraction(
            review=review, archive=archive,
        )
        if archive_job.status not in {"PENDING", "RUNNING"}:
            self.legacy._raise(409, "EXTRACTION_RECOVERY_CONFLICT", "原归档任务已进入终态，不能自动重用。")
        archive.status = "PENDING"
        archive.filesystem_job_id = archive_job.id
        item.status = "RUNNING"
        item.stage = "ARCHIVE"
        item.current_job_id = archive_job.id
        IngestionWorkflow(self.db).synchronize_batch(batch)
        batch.result_revision += 1
        self.db.flush()
        return True

    def prepare_confirmation(
        self,
        *,
        task_id: str,
        payload: DocumentExtractionPreviewRequest,
        current_user: User,
    ) -> tuple[DocumentExtractionConfirmation, bool]:
        """登记本地结果指纹和摘要；该接口永远不接收或保存提取正文。"""

        task, item, batch = self.legacy._owned_task(
            task_id=task_id,
            user_id=current_user.id,
            for_update=True,
        )
        self._require_document_contract(task)
        self._require_active_item(item)
        if self._save_policy(batch) != "CONFIRM_BEFORE_SAVE":
            self.legacy._raise(
                409,
                "EXTRACTION_CONFIRMATION_NOT_REQUIRED",
                "纯归档任务不使用内容保存前确认。",
            )
        self.legacy._validate_lease(
            task=task,
            worker_id=payload.worker_id,
            lease_token=payload.lease_token,
        )
        if (
            payload.source_sha256 != task.source_sha256
            or str(payload.source_version_id) != task.source_version_id
        ):
            self.legacy._raise(409, "SOURCE_SNAPSHOT_CHANGED", "确认预览对应的源版本或哈希不一致。")
        existing = (
            self.db.query(DocumentExtractionConfirmation)
            .filter_by(task_id=task.id, preview_key=payload.preview_key)
            .one_or_none()
        )
        if existing is not None:
            if existing.result_digest != payload.result_digest:
                self.legacy._raise(409, "IDEMPOTENCY_CONFLICT", "同一预览键已经用于不同提取结果。")
            return existing, True
        latest = self._latest_confirmation(task.id, lock=True)
        if latest is not None and latest.result_digest == payload.result_digest:
            return latest, True
        if latest is not None and latest.status in {"WAITING_CONFIRMATION", "CONFIRMED"}:
            latest.status = "SUPERSEDED"
        confirmation = DocumentExtractionConfirmation(
            task_id=task.id,
            user_id=current_user.id,
            source_version_id=task.source_version_id,
            source_sha256=task.source_sha256,
            revision=(latest.revision + 1) if latest is not None else 1,
            preview_key=payload.preview_key,
            result_digest=payload.result_digest,
            status="WAITING_CONFIRMATION",
            summary_json={
                "provider_name": payload.provider_name,
                "coverage_status": payload.coverage_status,
                "unit_count": payload.unit_count,
                "block_count": payload.block_count,
            },
        )
        self.db.add(confirmation)
        self.db.flush()
        self._record_audit(
            task=task,
            item=item,
            batch=batch,
            user=current_user,
            tool_name="document_extraction_preview",
            change_type="DOCUMENT_EXTRACTION_CONFIRMATION_REQUESTED",
            status="COMPLETED",
            summary={
                "confirmation_id": confirmation.id,
                "confirmation_revision": confirmation.revision,
                "result_digest": confirmation.result_digest,
                **confirmation.summary_json,
            },
        )
        self.db.flush()
        return confirmation, False

    def confirm(
        self,
        *,
        task_id: str,
        confirmation_id: str,
        result_digest: str,
        current_user: User,
    ) -> tuple[DocumentExtractionConfirmation, bool]:
        """记录用户对当前结果指纹的确认；确认本身不写入正文。"""

        task, item, batch = self.legacy._owned_task(
            task_id=task_id,
            user_id=current_user.id,
            for_update=True,
        )
        self._require_document_contract(task)
        self._require_active_item(item)
        if self._save_policy(batch) != "CONFIRM_BEFORE_SAVE":
            self.legacy._raise(409, "EXTRACTION_CONFIRMATION_NOT_REQUIRED", "纯归档任务不需要内容确认。")
        confirmation = (
            self.db.query(DocumentExtractionConfirmation)
            .filter_by(id=confirmation_id, task_id=task.id, user_id=current_user.id)
            .with_for_update()
            .one_or_none()
        )
        latest = self._latest_confirmation(task.id, lock=True)
        if confirmation is None or latest is None or confirmation.id != latest.id:
            self.legacy._raise(409, "EXTRACTION_CONFIRMATION_STALE", "提取结果已经变化，请重新展示并确认。")
        if confirmation.result_digest != result_digest:
            self.legacy._raise(409, "EXTRACTION_CONFIRMATION_DIGEST_MISMATCH", "确认对象与展示结果指纹不一致。")
        if confirmation.status in {"CONFIRMED", "SAVED"}:
            return confirmation, True
        if confirmation.status != "WAITING_CONFIRMATION":
            self.legacy._raise(409, "EXTRACTION_CONFIRMATION_STALE", "当前确认请求已经失效。")
        confirmation.status = "CONFIRMED"
        confirmation.confirmed_at = utcnow()
        self._record_audit(
            task=task,
            item=item,
            batch=batch,
            user=current_user,
            tool_name="document_extraction_confirm",
            change_type="DOCUMENT_EXTRACTION_CONFIRMED",
            status="COMPLETED",
            summary={
                "confirmation_id": confirmation.id,
                "confirmation_revision": confirmation.revision,
                "result_digest": confirmation.result_digest,
            },
        )
        self.db.flush()
        return confirmation, False

    def _record_audit(self, *, task, item, batch, user, tool_name: str, change_type: str,
                      status: str, summary: dict[str, Any]):
        """保存不含 token 和正文的宿主工具调用、业务状态及变更集。"""

        version = self.db.get(DocumentVersion, task.source_version_id)
        document_id = version.document_id if version is not None else None
        changeset, _ = create_lifecycle_audit(
            db=self.db, user_id=user.id, workspace_id=batch.workspace_id,
            conversation_id=batch.conversation_id, tool_name=tool_name,
            message_content=f"WorkBuddy 文档提取：{task.status}", change_type=change_type,
            target_type="EXTERNAL_EXTRACTION_TASK", target_id=task.id,
            target_document_id=document_id, after_value=summary,
            execution_status=status, visible_in_conversation=False,
        )
        return changeset

    @staticmethod
    def _audit_summary(*, task, payload, run) -> dict[str, Any]:
        """审计仅保留来源标识、覆盖和计数，不复制宿主正文或租约。"""

        return {
            "task_id": task.id,
            "extraction_run_id": run.id,
            "coverage_status": payload.coverage.status,
            "unit_count": len(payload.units),
            "block_count": sum(len(unit.blocks) for unit in payload.units),
            "provider_name": payload.provider_name,
        }

    def to_response(self, task: ExternalExtractionTask) -> dict[str, Any]:
        """构造没有正文、token、存储路径的稳定状态投影。"""

        summary = dict(task.result_summary_json or {})
        error = dict(task.error_json or {})
        item = self.db.get(IngestItem, task.ingest_item_id)
        batch = self.db.get(IngestBatch, item.batch_id) if item is not None else None
        save_policy = self._save_policy(batch)
        return {
            "id": task.id,
            "ingest_item_id": task.ingest_item_id,
            "provider_contract_version": DOCUMENT_EXTRACTION_CONTRACT_VERSION,
            "source_version_id": task.source_version_id,
            "source_sha256": task.source_sha256,
            "status": task.status,
            "coverage": dict(summary.get("coverage") or {}),
            "retryable": bool(error.get("retryable")),
            "error": error,
            "extraction_run_id": task.extraction_run_id,
            "extraction_save_policy": save_policy,
            "confirmation_required": save_policy == "CONFIRM_BEFORE_SAVE",
            "confirmation": self._confirmation_projection(task),
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    def confirmation_response(
        self,
        confirmation: DocumentExtractionConfirmation,
        *,
        reused: bool,
    ) -> dict[str, Any]:
        """返回不含正文和租约的确认状态。"""

        return {
            "id": confirmation.id,
            "task_id": confirmation.task_id,
            "revision": confirmation.revision,
            "result_digest": confirmation.result_digest,
            "status": confirmation.status,
            "summary": dict(confirmation.summary_json or {}),
            "confirmed_at": confirmation.confirmed_at,
            "saved_at": confirmation.saved_at,
            "reused": reused,
        }

    def _persist_result(self, *, document: Document, version: DocumentVersion, task: ExternalExtractionTask, payload: DocumentExtractionSubmitRequest, user_id: str) -> DocumentExtractionRun:
        """把宿主真实区段写入正式页面和元素，绝不通过服务器解析器补正文。"""

        repository = FileExtractionRepository(self.db, user_id)
        run = repository.create_extraction_run(
            document_id=document.id,
            document_version_id=version.id,
            extractor=DOCUMENT_EXTRACTOR,
            parser_name=payload.provider_name,
            parser_version=payload.provider_version or DOCUMENT_EXTRACTION_CONTRACT_VERSION,
            parser_config_hash=hashlib.sha256(DOCUMENT_EXTRACTION_CONTRACT_VERSION.encode()).hexdigest(),
        )
        pages: list[dict[str, Any]] = []
        elements: list[dict[str, Any]] = []
        element_index = 0
        for unit in payload.units:
            metadata = {
                "content_origin": "WORKBUDDY",
                "contract_version": DOCUMENT_EXTRACTION_CONTRACT_VERSION,
                "origin_task_id": task.id,
                "source_sha256": task.source_sha256,
                "source_unit_id": unit.unit_id,
                "unit_order": unit.order,
                "unit_kind": unit.kind,
                "sheet_name": unit.sheet_name,
                "unit_status": unit.status,
                "coverage_status": payload.coverage.status,
                "coverage": payload.coverage.model_dump(mode="json"),
                "provider_name": payload.provider_name,
            }
            pages.append({"page_number": unit.page_number, "text": unit.text, "metadata": metadata})
            if unit.blocks:
                for block in unit.blocks:
                    elements.append({
                        "element_index": element_index,
                        "label": block.kind.lower(),
                        "text": block.text,
                        "page_number": unit.page_number,
                        "bbox": block.bbox.model_dump() if block.bbox else {},
                        "metadata": {**metadata, "source_block_id": block.block_id, "start": block.start, "end": block.end, "table_id": block.table_id, "row_index": block.row_index, "column_index": block.column_index},
                    })
                    element_index += 1
            elif unit.text:
                # 没有精确 block 时保留整段元素，并明确它只是区段定位，不能冒充表格单元格。
                elements.append({"element_index": element_index, "label": "text", "text": unit.text, "page_number": unit.page_number, "bbox": {}, "metadata": {**metadata, "source_block_id": None, "start": 0, "end": len(unit.text)}})
                element_index += 1
        repository.complete_extraction_run(run=run, pages=pages, elements=elements)
        if payload.coverage.status != "COMPLETE":
            run.status = payload.coverage.status
            run.error_message = payload.error.message if payload.error else "WorkBuddy 文档提取不完整"
            run.updated_at = utcnow()
            self.db.flush()
        return run

    def _continue_complete_result(self, *, task: ExternalExtractionTask, item: IngestItem, batch: Any, version: DocumentVersion) -> tuple[ExternalExtractionTask, str, str | None, bool, bool]:
        """完整正文复用既有近似查重和归档逻辑，不重新读取原文件。"""

        review = self.db.query(UploadDuplicateReview).filter(UploadDuplicateReview.ingest_item_id == item.id).one()
        pages = self.db.query(DocumentPage).filter_by(extraction_run_id=task.extraction_run_id).all()
        normalized_pages = [{"page_number": page.page_number, "text": page.text_content, "metadata": dict(page.metadata_json or {})} for page in pages]
        # 查重阶段可能已经写入精确哈希/同名候选；宿主正文只补充近似候选，不能覆盖
        # 或丢失前者。最终以数据库中的完整候选集决定是否要求用户选择。
        self.legacy._near_candidates(item=item, review=review, pages=normalized_pages)
        candidates = (
            self.db.query(UploadDuplicateCandidate)
            .filter(UploadDuplicateCandidate.duplicate_review_id == review.id)
            .order_by(UploadDuplicateCandidate.rank.asc())
            .all()
        )
        archive = self.db.get(UploadArchiveRecord, item.archive_record_id)
        if archive is None:
            self.legacy._raise(409, "ARCHIVE_STATE_MISSING", "上传归档状态不存在。")
        if candidates:
            review.status = "WAITING_CONFIRMATION"
            review.comparison_phase = "NEAR"
            review.revision += 1
            archive.status = "WAITING_DUPLICATE_CONFIRMATION"
            item.status = "WAITING_DUPLICATE_CONFIRMATION"
            item.stage = "DUPLICATE_DECISION"
            next_job_id = None
        else:
            review.status = "RESOLVED"
            review.decision = "CONTINUE_UPLOAD"
            review.decided_at = utcnow()
            archive.status = "PENDING"
            archive_job = UploadLifecycleService(self.db).enqueue_archive_after_external_extraction(review=review, archive=archive)
            archive.filesystem_job_id = archive_job.id
            item.current_job_id = archive_job.id
            item.stage = "ARCHIVE"
            item.status = "RUNNING"
            next_job_id = archive_job.id
        IngestionWorkflow(self.db).synchronize_batch(batch)
        batch.result_revision += 1
        self.db.flush()
        return task, item.stage, next_job_id, False, True

    def _validate_unit_scope(self, *, payload: DocumentExtractionSubmitRequest, manifest: dict[str, Any]) -> None:
        """PDF/图片按服务器仅元数据得到的固定页范围校验，Word 不生成假页码。"""

        kind = str(manifest.get("source_kind") or "")
        if kind == "PDF":
            expected = set(range(1, int(manifest.get("page_count") or 0) + 1))
            actual = {unit.page_number for unit in payload.units if unit.kind == "PDF_PAGE"}
            if actual != expected:
                self.legacy._raise(409, "EXTRACTION_PAGE_SCOPE_MISMATCH", "PDF 提交必须覆盖固定的完整页面集合。", details={"expected": sorted(expected), "submitted": sorted(number for number in actual if number is not None)})
        elif kind == "IMAGE":
            if len(payload.units) != 1 or payload.units[0].kind != "IMAGE_PAGE" or payload.units[0].page_number != 1:
                self.legacy._raise(409, "EXTRACTION_PAGE_SCOPE_MISMATCH", "图片提交必须包含唯一的第 1 页结果。")
        elif kind == "WORD" and any(unit.kind != "TEXT_SECTION" or unit.page_number is not None for unit in payload.units):
            self.legacy._raise(422, "WORD_LOCATION_INVALID", "Word 只能提交不带物理页码的 TEXT_SECTION。")
        elif kind == "SPREADSHEET":
            if any(
                unit.kind != "SHEET"
                or unit.page_number is not None
                or not unit.sheet_name
                for unit in payload.units
            ):
                self.legacy._raise(
                    422,
                    "SPREADSHEET_LOCATION_INVALID",
                    "电子表格只能提交带真实 sheet_name 且不带物理页码的 SHEET。",
                )
            sheet_names = [unit.sheet_name for unit in payload.units]
            if len(sheet_names) != len(set(sheet_names)):
                self.legacy._raise(422, "SPREADSHEET_SHEET_DUPLICATED", "同一电子表格不能重复提交工作表。")

    def _source_manifest(self, version: DocumentVersion) -> dict[str, Any]:
        """读取容器元信息而非正文；PDF 页数用于防止宿主漏提交页面。"""

        suffix = Path(version.filename).suffix.lower()
        source_kind = (
            "PDF"
            if suffix == ".pdf"
            else "WORD"
            if suffix in {".doc", ".docx"}
            else "SPREADSHEET"
            if suffix in {".xls", ".xlsx"}
            else "IMAGE"
        )
        page_count: int | None = 1 if source_kind == "IMAGE" else None
        if source_kind == "PDF":
            try:
                import fitz
                with fitz.open(self.storage.upload_path(version.storage_path)) as pdf:
                    if pdf.needs_pass:
                        self.legacy._raise(422, "PDF_ENCRYPTED", "加密 PDF 需要在 WorkBuddy 提供可读取副本。")
                    page_count = int(pdf.page_count)
            except ImportError:
                self.legacy._raise(503, "PDF_METADATA_UNAVAILABLE", "服务器缺少 PDF 容器元信息读取能力。")
            except HTTPException:
                raise
            except Exception:
                self.legacy._raise(422, "PDF_INVALID_OR_INCOMPLETE", "PDF 文件结构无效或不完整。")
        return {"filename": version.filename, "content_type": version.content_type, "size_bytes": version.size_bytes, "source_kind": source_kind, "page_count": page_count}

    def _require_document_contract(self, task: ExternalExtractionTask) -> None:
        """拒绝新旧提取契约交叉调用。"""

        if task.provider_contract_version != DOCUMENT_EXTRACTION_CONTRACT_VERSION:
            self.legacy._raise(409, "EXTRACTION_CONTRACT_MISMATCH", "任务不属于 WorkBuddy 文档提取契约。")

    def _require_active_item(self, item: IngestItem) -> None:
        """取消、过期或结束的导入条目不再接受领取、下载、续租或新结果。"""

        if item.status in {"CANCELLED", "EXPIRED", "SKIPPED", "FAILED", "SUCCEEDED"}:
            self.legacy._raise(409, "EXTRACTION_TASK_INACTIVE", "导入条目已结束，不能继续文档提取。")

    @staticmethod
    def _save_policy(batch: IngestBatch | None) -> str:
        """旧批次缺少新字段时保持纯归档直接提交语义。"""

        value = str(((batch.policy_json if batch is not None else {}) or {}).get("extraction_save_policy") or "DIRECT_ARCHIVE")
        return value if value in {"DIRECT_ARCHIVE", "CONFIRM_BEFORE_SAVE"} else "DIRECT_ARCHIVE"

    def _latest_confirmation(
        self,
        task_id: str,
        *,
        lock: bool = False,
    ) -> DocumentExtractionConfirmation | None:
        """读取任务最新确认修订，旧确认永远不能授权新结果。"""

        query = (
            self.db.query(DocumentExtractionConfirmation)
            .filter_by(task_id=task_id)
            .order_by(DocumentExtractionConfirmation.revision.desc())
        )
        return (query.with_for_update() if lock else query).first()

    def _confirmation_projection(self, task: ExternalExtractionTask) -> dict[str, Any]:
        """任务状态只暴露指纹和计数，不能返回正文。"""

        latest = self._latest_confirmation(task.id)
        if latest is None:
            return {}
        return self.confirmation_response(latest, reused=False)

    def _require_confirmed_result(
        self,
        *,
        task: ExternalExtractionTask,
        batch: IngestBatch,
        result_digest: str,
    ) -> DocumentExtractionConfirmation | None:
        """显式提取只能保存用户已经确认的完全相同结果。"""

        if self._save_policy(batch) != "CONFIRM_BEFORE_SAVE":
            return None
        latest = self._latest_confirmation(task.id, lock=True)
        if latest is None or latest.status != "CONFIRMED":
            self.legacy._raise(
                409,
                "EXTRACTION_CONFIRMATION_REQUIRED",
                "请先向用户展示 WorkBuddy 提取结果并取得明确确认。",
            )
        if latest.result_digest != result_digest:
            self.legacy._raise(
                409,
                "EXTRACTION_RESULT_CHANGED_AFTER_CONFIRMATION",
                "提取结果已变化，必须重新展示并确认后才能保存。",
            )
        return latest

    @staticmethod
    def _submission_digest(payload: DocumentExtractionSubmitRequest) -> str:
        value = payload.model_dump(mode="json", exclude={"worker_id", "lease_token", "submission_key"})
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
