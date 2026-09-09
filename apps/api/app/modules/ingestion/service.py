"""WorkBuddy/MCP 批量导入的清单、内容接收、动作和回执业务服务。

所有写入均绑定当前用户、默认工作区、固定清单与幂等摘要；内容接收只安排受控生命周期任务，
不会让 MCP 直接归档、分类、命名或修改工作副本，也不能在 seal 后扩大处理目录。
"""

from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    Conversation,
    Document,
    DocumentVersion,
    FilesystemJob,
    IngestBatch,
    IngestItem,
    IngestRequestExecution,
    IntegrationRequest,
    UploadArchiveRecord,
    UploadDuplicateCandidate,
    User,
    utcnow,
)
from app.modules.ingestion.repository import IngestionRepository
from app.modules.ingestion.workflow import IngestionWorkflow
from app.modules.ingestion.schemas import (
    IngestBatchCounts,
    IngestBatchCreateRequest,
    IngestBatchResponse,
    IngestBatchResumeResponse,
    IngestContentUploadResponse,
    IngestItemCreate,
    IngestItemResponse,
    IngestItemsAppendRequest,
    IngestItemsAppendResponse,
    IngestItemsPageResponse,
    IngestItemActionRequest,
    IngestItemActionResponse,
)
from app.modules.file_lifecycle.service import UploadLifecycleService
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.files.service import FileUploadService, StagedUpload


INGEST_POLICY_VERSION = "ingest-v1"
KNOWN_ITEM_STATUSES = (
    "PENDING",
    "RUNNING",
    "WAITING_DUPLICATE_CONFIRMATION",
    "WAITING_EXTERNAL_EXTRACTION",
    "WAITING_EXISTING_RESULT",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "SKIPPED",
)


class IngestionService:
    """管理批次创建、清单登记、seal 和当前用户的只读恢复。"""

    def __init__(self, db: Session) -> None:
        """注入请求级 Session，避免跨请求用户或事务泄漏。"""

        self.db = db
        self.repository = IngestionRepository(db)
        self._execution_status_cache: dict[str, dict[str, str]] = {}

    def create_batch(
        self,
        *,
        request: IngestBatchCreateRequest,
        current_user: User,
    ) -> IngestBatchResponse:
        """幂等创建批次；同键不同策略必须返回冲突而不能复用旧批次。"""

        workspace_id = current_user.default_workspace_id
        if not workspace_id:
            self._raise_error(409, "DEFAULT_WORKSPACE_REQUIRED", "当前用户缺少默认工作区。")
        self._validate_conversation(
            conversation_id=request.conversation_id,
            user_id=current_user.id,
        )
        request_fingerprint = self._batch_fingerprint(request)
        existing = self.repository.find_batch_by_idempotency(
            user_id=current_user.id,
            client_id=request.client_id,
            idempotency_key=request.idempotency_key,
        )
        if existing is not None:
            self._validate_batch_replay(existing=existing, fingerprint=request_fingerprint)
            return self._to_batch_response(existing)

        policy = {
            "source_root_ref": request.source_root_ref,
            "relative_directory": request.relative_directory,
            "recursive": request.recursive,
            "ingest_policy": request.ingest_policy,
            "placement_mode": request.placement_mode,
            "rule_profile": request.rule_profile,
        }
        try:
            batch = self.repository.create_batch(
                user_id=current_user.id,
                workspace_id=str(workspace_id),
                client_id=request.client_id,
                request_id=request.request_id,
                idempotency_key=request.idempotency_key,
                request_fingerprint=request_fingerprint,
                manifest_status="OPEN",
                manifest_revision=1,
                result_revision=1,
                policy_json=policy,
                policy_version=INGEST_POLICY_VERSION,
                user_request=request.user_request,
                conversation_id=request.conversation_id,
                status="PENDING",
            )
            self.repository.create_integration_request(
                user_id=current_user.id,
                client_id=request.client_id,
                request_id=request.request_id,
                operation="INGEST_BATCH_CREATE",
                idempotency_key=request.idempotency_key,
                payload_digest=request_fingerprint,
                target_refs_json={"batch_id": batch.id},
                status="COMPLETED",
                result_json={"batch_id": batch.id, "manifest_status": batch.manifest_status},
            )
            self.db.commit()
            self.db.refresh(batch)
        except IntegrityError as exc:
            # 并发重试可能在首次查询后插入相同键；回滚后只允许返回完全相同的请求。
            self.db.rollback()
            existing = self.repository.find_batch_by_idempotency(
                user_id=current_user.id,
                client_id=request.client_id,
                idempotency_key=request.idempotency_key,
            )
            if existing is None:
                raise
            self._validate_batch_replay(existing=existing, fingerprint=request_fingerprint)
            return self._to_batch_response(existing)
        return self._to_batch_response(batch)

    def append_items(
        self,
        *,
        batch_id: str,
        request: IngestItemsAppendRequest,
        current_user: User,
    ) -> IngestItemsAppendResponse:
        """向 OPEN 批次追加一页清单，并逐项校验幂等载荷和目录边界。"""

        batch = self._get_owned_batch(
            batch_id=batch_id,
            user_id=current_user.id,
            for_update=True,
        )
        if batch.manifest_status != "OPEN":
            self._raise_error(409, "MANIFEST_SEALED", "批次清单已经固定，不能继续追加文件。")

        existing_by_id = self.repository.get_items_by_client_ids(
            batch_id=batch.id,
            client_item_ids=[item.client_item_id for item in request.items],
        )
        for item_request in request.items:
            self._validate_item_scope(batch=batch, item=item_request)
            existing_item = existing_by_id.get(item_request.client_item_id)
            if existing_item is not None and not self._same_item_snapshot(existing_item, item_request):
                self._raise_error(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "同一 client_item_id 已登记为不同的文件快照。",
                    details={"client_item_id": item_request.client_item_id},
                )

        new_requests = [
            item_request
            for item_request in request.items
            if item_request.client_item_id not in existing_by_id
        ]
        self._validate_ingest_limits(
            batch=batch,
            user_id=current_user.id,
            new_requests=new_requests,
        )

        ordered_items: list[IngestItem] = []
        created_count = 0
        for item_request in request.items:
            existing_item = existing_by_id.get(item_request.client_item_id)
            if existing_item is not None:
                ordered_items.append(existing_item)
                continue
            created = self.repository.create_item(
                batch_id=batch.id,
                client_item_id=item_request.client_item_id,
                source_root_ref=item_request.source_root_ref,
                source_relative_path=item_request.source_relative_path,
                original_filename=item_request.original_filename,
                expected_size=item_request.size_bytes,
                source_mtime_ns=item_request.mtime_ns,
                expected_sha256=item_request.expected_sha256,
                workflow_revision=1,
                stage="RECEIVE",
                status="PENDING",
                error_json={},
                result_json={},
            )
            ordered_items.append(created)
            created_count += 1
        if created_count:
            batch.manifest_revision += 1
        try:
            self.db.commit()
            self.db.refresh(batch)
            for item in ordered_items:
                self.db.refresh(item)
        except IntegrityError as exc:
            # 批次级唯一约束是并发写入的最终安全网；调用方可用同一页重试恢复。
            self.db.rollback()
            self._raise_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "清单正在被另一个请求修改，请使用相同内容重试。",
            )
        return IngestItemsAppendResponse(
            batch=self._to_batch_response(batch),
            items=[self._to_item_response(item) for item in ordered_items],
            created_count=created_count,
            reused_count=len(ordered_items) - created_count,
        )

    def seal_batch(self, *, batch_id: str, current_user: User) -> IngestBatchResponse:
        """固定批次成员范围；重复 seal 幂等返回，不启动后续文件处理。"""

        batch = self._get_owned_batch(
            batch_id=batch_id,
            user_id=current_user.id,
            for_update=True,
        )
        if batch.manifest_status == "SEALED":
            return self._to_batch_response(batch)
        if batch.manifest_status != "OPEN":
            self._raise_error(409, "MANIFEST_NOT_OPEN", "当前批次不能固定清单。")
        if self.repository.count_items(batch_id=batch.id) == 0:
            self._raise_error(409, "EMPTY_MANIFEST", "批次至少需要登记一个文件后才能固定。")
        batch.manifest_status = "SEALED"
        batch.manifest_revision += 1
        self.db.commit()
        self.db.refresh(batch)
        return self._to_batch_response(batch)

    def get_batch(self, *, batch_id: str, current_user: User) -> IngestBatchResponse:
        """返回当前用户的批次及真实逐状态统计。"""

        batch = self._get_owned_batch(batch_id=batch_id, user_id=current_user.id)
        self._synchronize_and_commit(batch)
        return self._to_batch_response(batch)

    def list_items(
        self,
        *,
        batch_id: str,
        cursor: str | None,
        limit: int,
        current_user: User,
    ) -> IngestItemsPageResponse:
        """分页返回当前用户批次条目，并拒绝跨批次游标。"""

        batch = self._get_owned_batch(batch_id=batch_id, user_id=current_user.id)
        self._synchronize_and_commit(batch)
        if cursor:
            cursor_item = self.repository.get_item(item_id=cursor)
            if cursor_item is None or cursor_item.batch_id != batch.id:
                self._raise_error(400, "INVALID_CURSOR", "分页游标不属于当前批次。")
        rows = self.repository.list_items(batch_id=batch.id, cursor=cursor, limit=limit)
        has_next = len(rows) > limit
        page_rows = rows[:limit]
        next_cursor = page_rows[-1].id if has_next and page_rows else None
        return IngestItemsPageResponse(
            items=[self._to_item_response(item) for item in page_rows],
            next_cursor=next_cursor,
        )

    def resume_batch(
        self,
        *,
        batch_id: str,
        current_user: User,
    ) -> IngestBatchResumeResponse:
        """返回可恢复传输项，但不重置失败、取消、等待确认或已完成结果。"""

        batch = self._get_owned_batch(batch_id=batch_id, user_id=current_user.id)
        if batch.manifest_status != "SEALED":
            self._raise_error(409, "MANIFEST_NOT_SEALED", "批次清单固定后才能恢复传输。")
        self._synchronize_and_commit(batch)
        return IngestBatchResumeResponse(
            batch=self._to_batch_response(batch),
            receivable_item_ids=self.repository.list_receivable_item_ids(batch_id=batch.id),
        )

    def retry_item(
        self,
        *,
        item_id: str,
        request: IngestItemActionRequest,
        current_user: User,
    ) -> IngestItemActionResponse:
        """显式重试失败条目，或只重试已发布文件失败的附带请求。"""

        item, batch = self._owned_item(item_id=item_id, user_id=current_user.id, for_update=True)
        self.db.refresh(batch, with_for_update=True)
        # 先吸收工作副本和附带请求的持久化事实，避免用陈旧 RUNNING 状态重开已发布文件。
        IngestionWorkflow(self.db).synchronize_batch(batch)
        replay = self._action_replay(
            current_user=current_user,
            request=request,
            operation="INGEST_ITEM_RETRY",
            item=item,
        )
        if replay is not None:
            return self._action_response(batch=batch, item=item, job_id=replay.result_json.get("filesystem_job_id"), reused=True)
        failed_execution = self._failed_request_execution_for_item(item=item)
        if item.status in {"SUCCEEDED", "PARTIAL"} and failed_execution is not None:
            job_id = self._retry_failed_request_execution(
                execution=failed_execution,
                batch=batch,
            )
            item.workflow_revision += 1
            batch.result_revision += 1
            self._record_action(
                current_user=current_user,
                request=request,
                operation="INGEST_ITEM_RETRY",
                item=item,
                result={
                    "filesystem_job_id": job_id,
                    "status": item.status,
                    "retry_scope": "ATTACHED_REQUEST",
                },
            )
            self.db.commit()
            return self._action_response(batch=batch, item=item, job_id=job_id)
        if item.status != "FAILED":
            self._raise_error(409, "INGEST_ITEM_NOT_RETRYABLE", "只有失败条目可以显式重试。")
        job_id: str | None = None
        if not item.upload_document_version_id:
            # 接收失败后重新开放同一个固定清单项；MCP 仍须重新校验本地快照并上传字节。
            item.status = "PENDING"
            item.stage = "RECEIVE"
            item.error_json = {}
        else:
            failed_job = self.db.get(FilesystemJob, item.current_job_id) if item.current_job_id else None
            if failed_job is None or failed_job.status != "FAILED":
                self._raise_error(409, "INGEST_RETRY_JOB_MISSING", "失败阶段缺少可恢复任务。")
            reset = FilesystemJobQueue(self.db).create_job(
                job_type=failed_job.job_type,
                queue_name=failed_job.queue_name,
                root_id=failed_job.root_id,
                created_by=failed_job.created_by,
                deduplication_key=failed_job.deduplication_key,
                priority=failed_job.priority,
                max_attempts=failed_job.max_attempts,
                payload=dict(failed_job.payload_json or {}),
                retry_failed=True,
            )
            archive = (
                self.db.get(UploadArchiveRecord, item.archive_record_id)
                if item.archive_record_id
                else None
            )
            if archive is not None and archive.status == "FAILED":
                # 生命周期失败会把归档投影同步标成 FAILED。显式重开原任务时必须同时恢复
                # 该业务状态，否则 worker 会因“FAILED 不允许归档”立即再次失败。已生成
                # managed_file 的后续阶段从 ARCHIVED 继续；尚未发布的归档阶段从 RETRY_WAIT 继续。
                archive.status = "ARCHIVED" if archive.managed_file_id else "RETRY_WAIT"
                archive.last_error_code = None
                archive.last_error_message = None
            job_id = reset.id
            item.current_job_id = reset.id
            item.status = "RUNNING"
            item.stage = self._stage_for_job_type(reset.job_type)
            item.error_json = {}
        item.workflow_revision += 1
        batch.status = "RUNNING"
        batch.result_revision += 1
        self._record_action(
            current_user=current_user,
            request=request,
            operation="INGEST_ITEM_RETRY",
            item=item,
            result={"filesystem_job_id": job_id, "status": item.status},
        )
        self.db.commit()
        return self._action_response(batch=batch, item=item, job_id=job_id)

    def cancel_item(
        self,
        *,
        item_id: str,
        request: IngestItemActionRequest,
        current_user: User,
    ) -> IngestItemActionResponse:
        """取消未发布条目；已发布时只取消尚未执行的附带请求。"""

        item, batch = self._owned_item(item_id=item_id, user_id=current_user.id, for_update=True)
        self.db.refresh(batch, with_for_update=True)
        # 发布边界以数据库中的活动工作副本为准，不能依赖客户端最后一次看到的条目状态。
        IngestionWorkflow(self.db).synchronize_batch(batch)
        replay = self._action_replay(
            current_user=current_user,
            request=request,
            operation="INGEST_ITEM_CANCEL",
            item=item,
        )
        if replay is not None:
            return self._action_response(batch=batch, item=item, job_id=replay.result_json.get("filesystem_job_id"), reused=True)
        if item.status in {"SUCCEEDED", "PARTIAL"} and item.final_working_copy_id:
            execution = self._pending_request_execution_for_item(item=item)
            if execution is None:
                self._raise_error(
                    409,
                    "INGEST_ITEM_ALREADY_PUBLISHED",
                    "文件已经完成发布且没有可取消的附带请求，不能撤销真实导入结果。",
                )
            cancelled_job_id = self._cancel_pending_request_execution(execution=execution)
            item.workflow_revision += 1
            batch.result_revision += 1
            self._record_action(
                current_user=current_user,
                request=request,
                operation="INGEST_ITEM_CANCEL",
                item=item,
                result={
                    "filesystem_job_id": cancelled_job_id,
                    "status": item.status,
                    "cancel_scope": "ATTACHED_REQUEST",
                },
            )
            self.db.commit()
            return self._action_response(
                batch=batch,
                item=item,
                job_id=cancelled_job_id,
            )
        if item.status in {"CANCELLED", "EXPIRED", "SKIPPED"}:
            self._raise_error(409, "INGEST_ITEM_NOT_CANCELLABLE", "当前条目已经进入终态，不能取消。")
        cleanup_job_id: str | None = None
        if item.upload_document_version_id:
            lifecycle = UploadLifecycleService(self.db)
            review = lifecycle.repository.get_review_by_version(item.upload_document_version_id)
            archive = lifecycle.repository.get_archive_by_version(item.upload_document_version_id)
            if review is not None:
                review.status = "RESOLVED"
                review.decision = "CANCEL_UPLOAD"
                review.decided_at = utcnow()
                review.revision += 1
                cleanup = lifecycle.enqueue_reused_upload_cleanup(review=review)
                cleanup_job_id = cleanup.id
            if archive is not None:
                archive.status = "CANCELLED"
                archive.filesystem_job_id = cleanup_job_id
            version = self.db.get(DocumentVersion, item.upload_document_version_id)
            document = self.db.get(Document, version.document_id) if version is not None else None
            if document is not None:
                # 暂存字节将由清理任务删除；同步业务状态可防止容量统计继续占用已取消文件。
                document.status = "UPLOAD_CANCELLED"
        item.status = "CANCELLED"
        item.stage = "DONE"
        item.decision = "CANCEL_UPLOAD"
        item.current_job_id = cleanup_job_id
        item.error_json = {}
        item.workflow_revision += 1
        IngestionWorkflow(self.db).synchronize_batch(batch)
        batch.result_revision += 1
        self._record_action(
            current_user=current_user,
            request=request,
            operation="INGEST_ITEM_CANCEL",
            item=item,
            result={"filesystem_job_id": cleanup_job_id, "status": item.status},
        )
        self.db.commit()
        return self._action_response(batch=batch, item=item, job_id=cleanup_job_id)

    def _request_executions_for_item(self, *, item: IngestItem) -> list[IngestRequestExecution]:
        """返回固定集合包含当前条目的附带请求，集合匹配不能退化为字符串包含。"""

        executions = (
            self.db.query(IngestRequestExecution)
            .filter(IngestRequestExecution.batch_id == item.batch_id)
            .order_by(IngestRequestExecution.created_at.desc())
            .all()
        )
        return [
            execution
            for execution in executions
            if item.id in {str(value) for value in list(execution.item_ids_json or [])}
        ]

    def _failed_request_execution_for_item(
        self,
        *,
        item: IngestItem,
    ) -> IngestRequestExecution | None:
        """定位最近一次失败的附带请求，文件导入成功事实不随请求失败降级。"""

        return next(
            (
                execution
                for execution in self._request_executions_for_item(item=item)
                if execution.status == "FAILED"
            ),
            None,
        )

    def _pending_request_execution_for_item(
        self,
        *,
        item: IngestItem,
    ) -> IngestRequestExecution | None:
        """仅允许取消尚未被 worker 领取的附带请求，避免伪造运行中任务已停止。"""

        for execution in self._request_executions_for_item(item=item):
            job = self.db.get(FilesystemJob, execution.filesystem_job_id) if execution.filesystem_job_id else None
            if execution.status == "PENDING" and job is not None and job.status == "PENDING":
                return execution
        return None

    def _retry_failed_request_execution(
        self,
        *,
        execution: IngestRequestExecution,
        batch: IngestBatch,
    ) -> str:
        """只重开附带请求的原任务，不重新分类、命名、归档或扩大附件集合。"""

        failed_job = self.db.get(FilesystemJob, execution.filesystem_job_id) if execution.filesystem_job_id else None
        if failed_job is None or failed_job.status != "FAILED":
            self._raise_error(409, "INGEST_RETRY_JOB_MISSING", "失败的附带请求缺少可恢复任务。")
        reset = FilesystemJobQueue(self.db).create_job(
            job_type=failed_job.job_type,
            queue_name=failed_job.queue_name,
            root_id=failed_job.root_id,
            created_by=failed_job.created_by,
            deduplication_key=failed_job.deduplication_key,
            priority=failed_job.priority,
            max_attempts=failed_job.max_attempts,
            payload=dict(failed_job.payload_json or {}),
            retry_failed=True,
        )
        execution.status = "PENDING"
        execution.error_json = {}
        execution.result_json = {}
        batch.updated_at = utcnow()
        return reset.id

    def _cancel_pending_request_execution(self, *, execution: IngestRequestExecution) -> str:
        """停止尚未领取的附带请求任务，同时保留任务与执行记录供回执审计。"""

        job = self.db.get(FilesystemJob, execution.filesystem_job_id)
        if job is None or job.status != "PENDING":
            self._raise_error(409, "INGEST_REQUEST_NOT_CANCELLABLE", "附带请求已经开始执行，当前不能安全取消。")
        job.status = "CANCELLED"
        job.finished_at = utcnow()
        job.result_json = {"cancelled": True, "scope": "ATTACHED_REQUEST"}
        execution.status = "CANCELLED"
        execution.error_json = {}
        execution.result_json = {"cancelled": True}
        return job.id

    async def receive_item_content(
        self,
        *,
        batch_id: str,
        item_id: str,
        file: UploadFile,
        current_user: User,
    ) -> IngestContentUploadResponse:
        """流式接收固定清单项，并在同一事务中绑定上传版本和后台任务。

        文件名、大小和可选 SHA-256 必须与 seal 清单一致。成功后自动进入现有查重链路，
        不要求 MCP 再伪造聊天消息；完全相同的重试只返回既有任务，不重复落盘。
        """

        batch = self._get_owned_batch(batch_id=batch_id, user_id=current_user.id)
        if batch.manifest_status != "SEALED":
            self._raise_error(409, "MANIFEST_NOT_SEALED", "批次清单固定后才能上传文件内容。")
        item = self.repository.get_item_for_update(item_id=item_id)
        if item is None or item.batch_id != batch.id:
            self._raise_error(404, "INGEST_ITEM_NOT_FOUND", "导入条目不存在或无权访问。")
        if item.upload_document_version_id:
            return self._existing_content_response(batch=batch, item=item)
        if item.status not in {"PENDING", "FAILED"} or item.stage != "RECEIVE":
            self._raise_error(409, "INGEST_ITEM_NOT_RECEIVABLE", "当前条目不能接收文件内容。")

        staged: StagedUpload | None = None
        try:
            staged = await FileUploadService(self.db).stage_upload(
                file=file,
                current_user=current_user,
                conversation_id=batch.conversation_id,
                expected_filename=item.original_filename,
            )
            self._validate_received_snapshot(item=item, staged=staged)
            staged.review.ingest_item_id = item.id
            staged.review.decision_scope_json = {
                "batch_id": batch.id,
                "ingest_item_id": item.id,
                "workflow_revision": item.workflow_revision,
            }
            primary_item = self._join_exact_duplicate_group(
                batch=batch,
                item=item,
                staged=staged,
            )
            if primary_item is None:
                processing = UploadLifecycleService(self.db).start_processing(
                    upload_version_id=staged.version.id,
                    current_user=current_user,
                    commit=False,
                )
                processing_job_id = processing.filesystem_job_id
                item.stage = "EXACT_CHECK"
                item.status = "RUNNING"
            else:
                # 同批后到成员先停在结构化确认，不重复启动相同内容的解析/归档主任务。
                staged.review.status = "WAITING_CONFIRMATION"
                staged.review.comparison_phase = "EXACT"
                staged.archive.status = "WAITING_DUPLICATE_CONFIRMATION"
                processing_job_id = primary_item.current_job_id
                item.stage = "DUPLICATE_DECISION"
                item.status = "WAITING_DUPLICATE_CONFIRMATION"
            item.actual_sha256 = staged.sha256
            item.upload_document_version_id = staged.version.id
            item.archive_record_id = staged.archive.id
            item.current_job_id = processing_job_id
            item.workflow_revision += 1
            item.error_json = {}
            item.result_json = {
                "document_id": staged.document.id,
                "size_bytes": staged.size_bytes,
                "sha256": staged.sha256,
            }
            # 文件传输期间不能持有批次锁，否则 P3 的逐文件并发会退化为串行；仅在发布
            # 状态修订前锁定并刷新批次，避免并发上传丢失 result_revision 增量。
            self.db.refresh(batch, with_for_update=True)
            batch.status = "RUNNING"
            batch.result_revision += 1
            self.db.commit()
            self.db.refresh(batch)
            self.db.refresh(item)
            return IngestContentUploadResponse(
                batch=self._to_batch_response(batch),
                item=self._to_item_response(item),
                filesystem_job_id=processing_job_id,
                accepted=True,
                reused=False,
            )
        except HTTPException as exc:
            self.db.rollback()
            if staged is not None:
                FileUploadService.remove_staged_file(staged.relative_path)
            # 文件名、类型、大小、容量或来源快照错误都是该条目的真实失败事实；记录后
            # 其他批次成员仍可继续，且显式重试仍复用同一固定清单项。
            self._record_receive_failure(
                batch_id=batch_id,
                item_id=item_id,
                user_id=current_user.id,
                detail=exc.detail,
            )
            raise
        except Exception:
            self.db.rollback()
            if staged is not None:
                FileUploadService.remove_staged_file(staged.relative_path)
            raise

    def _join_exact_duplicate_group(
        self,
        *,
        batch: IngestBatch,
        item: IngestItem,
        staged: StagedUpload,
    ) -> IngestItem | None:
        """登记同批完整哈希组，并为后到成员创建真实条目候选。"""

        group = self.repository.get_active_duplicate_group(
            batch_id=batch.id,
            user_id=batch.user_id,
            workspace_id=batch.workspace_id,
            content_sha256=staged.sha256,
        )
        created_group = False
        if group is None:
            try:
                # 两个不同条目可能同时观察到“尚无分组”；保存点只回滚竞争失败的
                # INSERT，随后读取唯一约束胜者并作为同批后到成员继续。
                with self.db.begin_nested():
                    group = self.repository.create_duplicate_group(
                        batch_id=batch.id,
                        user_id=batch.user_id,
                        workspace_id=batch.workspace_id,
                        content_sha256=staged.sha256,
                        revision=1,
                        primary_item_id=item.id,
                        status="ACTIVE",
                    )
                created_group = True
            except IntegrityError:
                group = self.repository.get_active_duplicate_group(
                    batch_id=batch.id,
                    user_id=batch.user_id,
                    workspace_id=batch.workspace_id,
                    content_sha256=staged.sha256,
                )
                if group is None:
                    raise
        if created_group:
            self.repository.create_duplicate_group_member(
                group_id=group.id,
                ingest_item_id=item.id,
                joined_revision=group.revision,
                decision=None,
                waits_for_item_id=None,
            )
            return None
        primary_item = self.repository.get_item(item_id=str(group.primary_item_id))
        if primary_item is None or primary_item.id == item.id:
            return None
        group.revision += 1
        self.repository.create_duplicate_group_member(
            group_id=group.id,
            ingest_item_id=item.id,
            joined_revision=group.revision,
            decision=None,
            waits_for_item_id=primary_item.id,
        )
        candidate = UploadDuplicateCandidate(
            duplicate_review_id=staged.review.id,
            candidate_managed_file_id=None,
            candidate_working_copy_id=None,
            candidate_ingest_item_id=primary_item.id,
            compared_version_id=primary_item.upload_document_version_id,
            compared_sha256=staged.sha256,
            compared_working_copy_revision=None,
            match_type="EXACT_HASH",
            match_scope="SAME_BATCH",
            similarity_score=1.0,
            match_evidence_json={"sha256": staged.sha256, "group_revision": group.revision},
            user_visible_summary_json={
                "filename": primary_item.original_filename,
                "source": "本批次较早提交的同内容文件",
            },
            rank=1,
        )
        self.db.add(candidate)
        self.db.flush()
        staged.review.decision_scope_json = {
            **dict(staged.review.decision_scope_json or {}),
            "duplicate_group_id": group.id,
            "group_revision": group.revision,
            "candidate_ids": [candidate.id],
        }
        return primary_item

    def _existing_content_response(
        self,
        *,
        batch: IngestBatch,
        item: IngestItem,
    ) -> IngestContentUploadResponse:
        """把成功接收过的条目作为幂等重传返回，不再次读取请求体或创建任务。"""

        if not item.current_job_id:
            self._raise_error(409, "INGEST_ITEM_INCOMPLETE", "条目已有上传版本但缺少处理任务，请恢复批次。")
        return IngestContentUploadResponse(
            batch=self._to_batch_response(batch),
            item=self._to_item_response(item),
            filesystem_job_id=item.current_job_id,
            accepted=True,
            reused=True,
        )

    def _validate_received_snapshot(self, *, item: IngestItem, staged: StagedUpload) -> None:
        """验证实际字节仍对应枚举时冻结的文件快照。"""

        if staged.size_bytes != item.expected_size:
            self._raise_error(
                409,
                "SOURCE_SNAPSHOT_CHANGED",
                "文件大小与清单快照不一致，请重新枚举目录并创建新批次。",
                details={"expected_size": item.expected_size, "actual_size": staged.size_bytes},
            )
        if item.expected_sha256 and staged.sha256 != item.expected_sha256:
            self._raise_error(
                409,
                "SOURCE_SNAPSHOT_CHANGED",
                "文件哈希与清单快照不一致，请重新枚举目录并创建新批次。",
                details={"expected_sha256": item.expected_sha256, "actual_sha256": staged.sha256},
            )

    def _record_receive_failure(
        self,
        *,
        batch_id: str,
        item_id: str,
        user_id: str,
        detail: object,
    ) -> None:
        """回滚暂存实体后，仅保留安全的逐文件失败事实和审计修订。"""

        batch = self._get_owned_batch(batch_id=batch_id, user_id=user_id)
        item = self.repository.get_item_for_update(item_id=item_id)
        if item is None or item.batch_id != batch.id:
            return
        self.db.refresh(batch, with_for_update=True)
        error = dict(detail) if isinstance(detail, dict) else {
            "code": "CONTENT_RECEIVE_FAILED",
            "message": str(detail),
        }
        item.status = "FAILED"
        item.stage = "RECEIVE"
        item.error_json = error
        item.workflow_revision += 1
        batch.result_revision += 1
        self.db.commit()

    def _get_owned_batch(
        self,
        *,
        batch_id: str,
        user_id: str,
        for_update: bool = False,
    ) -> IngestBatch:
        """统一隐藏不存在和无权访问的批次，避免通过 ID 枚举其他用户任务。"""

        batch = (
            self.repository.get_batch_for_update(batch_id=batch_id)
            if for_update
            else self.repository.get_batch(batch_id=batch_id)
        )
        if batch is None or batch.user_id != user_id:
            self._raise_error(404, "INGEST_BATCH_NOT_FOUND", "导入批次不存在或无权访问。")
        return batch

    def _owned_item(
        self,
        *,
        item_id: str,
        user_id: str,
        for_update: bool,
    ) -> tuple[IngestItem, IngestBatch]:
        """锁定当前用户条目，隐藏跨用户条目是否存在。"""

        query = self.db.query(IngestItem).filter(IngestItem.id == item_id)
        if for_update:
            query = query.with_for_update()
        item = query.one_or_none()
        batch = self.db.get(IngestBatch, item.batch_id) if item else None
        if item is None or batch is None or batch.user_id != user_id:
            self._raise_error(404, "INGEST_ITEM_NOT_FOUND", "导入条目不存在或无权访问。")
        return item, batch

    def _action_replay(
        self,
        *,
        current_user: User,
        request: IngestItemActionRequest,
        operation: str,
        item: IngestItem,
    ) -> IntegrationRequest | None:
        """校验条目动作幂等重放，同键不同动作或原因必须冲突。"""

        digest = self._action_digest(operation=operation, item_id=item.id, request=request)
        existing = self.repository.find_integration_request(
            user_id=current_user.id,
            client_id=request.client_id,
            idempotency_key=request.idempotency_key,
        )
        if existing is None:
            return None
        if (
            existing.operation != operation
            or existing.payload_digest != digest
            or str(existing.target_refs_json.get("ingest_item_id") or "") != item.id
        ):
            self._raise_error(409, "IDEMPOTENCY_CONFLICT", "同一幂等键已经用于不同条目动作。")
        return existing

    def _record_action(
        self,
        *,
        current_user: User,
        request: IngestItemActionRequest,
        operation: str,
        item: IngestItem,
        result: dict,
    ) -> None:
        """记录外部取消或重试写请求，审计中不保存自由文本正文。"""

        self.repository.create_integration_request(
            user_id=current_user.id,
            client_id=request.client_id,
            request_id=request.request_id,
            operation=operation,
            idempotency_key=request.idempotency_key,
            payload_digest=self._action_digest(operation=operation, item_id=item.id, request=request),
            target_refs_json={"ingest_item_id": item.id, "batch_id": item.batch_id},
            status="COMPLETED",
            result_json=result,
        )

    @staticmethod
    def _action_digest(
        *,
        operation: str,
        item_id: str,
        request: IngestItemActionRequest,
    ) -> str:
        """计算不包含 request_id 的稳定动作摘要。"""

        payload = {
            "operation": operation,
            "item_id": item_id,
            "reason": str(request.reason or "").strip(),
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _action_response(
        self,
        *,
        batch: IngestBatch,
        item: IngestItem,
        job_id: str | None,
        reused: bool = False,
    ) -> IngestItemActionResponse:
        """投影条目动作响应，不泄露任务载荷或归档内部 ID。"""

        return IngestItemActionResponse(
            item=self._to_item_response(item),
            batch=self._to_batch_response(batch),
            filesystem_job_id=str(job_id) if job_id else None,
            reused=reused,
        )

    @staticmethod
    def _stage_for_job_type(job_type: str) -> str:
        """把可重试任务类型映射回用户可见业务阶段。"""

        return {
            "CHECK_UPLOAD_DUPLICATES": "EXACT_CHECK",
            "ARCHIVE_UPLOAD_TO_MANAGED_ROOT": "ARCHIVE",
            "IMPORT_WORKING_COPIES": "MATERIALIZE",
            "ANALYZE_DOCUMENT_VERSION": "ORGANIZE",
            "RUN_INGEST_EXTRA_REQUEST": "EXTRA_REQUEST",
        }.get(job_type, "ORGANIZE")

    def _synchronize_and_commit(self, batch: IngestBatch) -> None:
        """按生命周期事实刷新批次投影；GET 产生的确定性修订也必须持久化。"""

        # 多个刷新/恢复请求可以同时投影异步任务；批次锁保证状态与修订号一起单调推进。
        self.db.refresh(batch, with_for_update=True)
        if IngestionWorkflow(self.db).synchronize_batch(batch):
            self.db.commit()
            self.db.refresh(batch)

    def _validate_conversation(self, *, conversation_id: str | None, user_id: str) -> None:
        """可选会话只能绑定当前用户，外部客户端不能借 ID 越权附着任务。"""

        if not conversation_id:
            return
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None or conversation.user_id != user_id:
            self._raise_error(404, "CONVERSATION_NOT_FOUND", "会话不存在或无权访问。")

    def _validate_batch_replay(self, *, existing: IngestBatch, fingerprint: str) -> None:
        """仅允许完全相同载荷复用幂等批次。"""

        if existing.request_fingerprint != fingerprint:
            self._raise_error(
                409,
                "IDEMPOTENCY_CONFLICT",
                "同一幂等键已经用于不同的批次请求。",
            )

    def _validate_ingest_limits(
        self,
        *,
        batch: IngestBatch,
        user_id: str,
        new_requests: list[IngestItemCreate],
    ) -> None:
        """在清单写入前执行单批文件数、总字节和用户容量限制。"""

        if not new_requests:
            return
        # 用户容量跨多个批次共享；锁定用户行后重新统计，防止并发批次都按旧余量通过。
        self.db.query(User).filter(User.id == user_id).with_for_update().one()
        settings = get_settings()
        existing_count = self.repository.count_items(batch_id=batch.id)
        resulting_count = existing_count + len(new_requests)
        if resulting_count > settings.integration_max_batch_files:
            self._raise_error(
                413,
                "INGEST_BATCH_FILE_LIMIT_EXCEEDED",
                "批次文件数量超过当前部署限制。",
                details={"limit": settings.integration_max_batch_files},
            )
        added_bytes = sum(item.size_bytes for item in new_requests)
        existing_bytes = self.repository.sum_batch_expected_bytes(batch_id=batch.id)
        if existing_bytes + added_bytes > settings.integration_max_batch_bytes:
            self._raise_error(
                413,
                "INGEST_BATCH_BYTE_LIMIT_EXCEEDED",
                "批次总大小超过当前部署限制。",
                details={"limit_bytes": settings.integration_max_batch_bytes},
            )
        used_and_reserved = self.repository.user_committed_and_reserved_bytes(user_id=user_id)
        if used_and_reserved + added_bytes > settings.integration_user_quota_bytes:
            self._raise_error(
                413,
                "INGEST_USER_QUOTA_EXCEEDED",
                "当前用户可用文件容量不足。",
                details={"quota_bytes": settings.integration_user_quota_bytes},
            )

    def _validate_item_scope(self, *, batch: IngestBatch, item: IngestItemCreate) -> None:
        """确认条目仍在创建批次时冻结的逻辑根和目录范围内。"""

        policy = dict(batch.policy_json or {})
        source_root_ref = str(policy.get("source_root_ref") or "")
        if item.source_root_ref != source_root_ref:
            self._raise_error(
                409,
                "MANIFEST_SCOPE_MISMATCH",
                "清单项的逻辑根与批次不一致。",
                details={"client_item_id": item.client_item_id},
            )
        relative_directory = str(policy.get("relative_directory") or ".")
        if relative_directory != "." and not item.source_relative_path.startswith(f"{relative_directory}/"):
            self._raise_error(
                409,
                "MANIFEST_SCOPE_MISMATCH",
                "清单项不在批次指定目录内。",
                details={"client_item_id": item.client_item_id},
            )

    @staticmethod
    def _same_item_snapshot(existing: IngestItem, requested: IngestItemCreate) -> bool:
        """比较会影响文件身份的完整清单快照。"""

        return (
            existing.source_root_ref == requested.source_root_ref
            and existing.source_relative_path == requested.source_relative_path
            and existing.original_filename == requested.original_filename
            and existing.expected_size == requested.size_bytes
            and existing.source_mtime_ns == requested.mtime_ns
            and existing.expected_sha256 == requested.expected_sha256
        )

    def _to_batch_response(self, batch: IngestBatch) -> IngestBatchResponse:
        """投影批次响应；策略中只包含逻辑引用，不包含宿主机目录。"""

        raw_counts = self.repository.count_items_by_status(batch_id=batch.id)
        count_values = {
            "total": sum(raw_counts.values()),
            **{
                status.lower(): raw_counts.get(status, 0)
                for status in KNOWN_ITEM_STATUSES
            },
        }
        final_receipt_ready = batch.status != "RUNNING" and count_values["total"] > 0
        request_executions = (
            self.db.query(IngestRequestExecution)
            .filter(IngestRequestExecution.batch_id == batch.id)
            .order_by(IngestRequestExecution.created_at.asc())
            .all()
        )
        batch_items = self.db.query(IngestItem).filter(IngestItem.batch_id == batch.id).all()
        retained_file_count = len({
            str(document_id)
            for (document_id,) in self.db.query(IngestItem.final_document_id).filter(
                IngestItem.batch_id == batch.id,
                IngestItem.final_document_id.isnot(None),
            ).all()
            if document_id
        })
        reused_count = sum(
            1
            for item in batch_items
            if item.status in {"SUCCEEDED", "PARTIAL"}
            and item.decision in {"USE_EXISTING_FILE", "WAIT_AND_REUSE"}
        )
        new_file_count = len({
            str(item.final_document_id)
            for item in batch_items
            if item.final_document_id
            and item.status in {"SUCCEEDED", "PARTIAL"}
            and item.decision not in {"USE_EXISTING_FILE", "WAIT_AND_REUSE"}
        })
        excluded_count = sum(
            1
            for item in batch_items
            if item.status in {"FAILED", "CANCELLED", "EXPIRED", "SKIPPED"}
        )
        receipt = {
            "title": "文件处理完成" if final_receipt_ready else "文件处理中",
            "counts": count_values,
            "retained_file_count": retained_file_count,
            "new_file_count": new_file_count,
            "reused_count": reused_count,
            "excluded_count": excluded_count,
            "has_pending_duplicate_confirmation": count_values["waiting_duplicate_confirmation"] > 0,
            "request_executions": [
                {
                    "id": execution.id,
                    "status": execution.status,
                    "item_ids": list(execution.item_ids_json or []),
                    "agent_run_id": execution.agent_run_id,
                    "result": dict(execution.result_json or {}),
                    "error": dict(execution.error_json or {}),
                }
                for execution in request_executions
            ],
        }
        return IngestBatchResponse(
            id=batch.id,
            client_id=batch.client_id,
            request_id=batch.request_id,
            manifest_status=batch.manifest_status,
            manifest_revision=batch.manifest_revision,
            result_revision=batch.result_revision,
            policy=dict(batch.policy_json or {}),
            policy_version=batch.policy_version,
            user_request=batch.user_request,
            conversation_id=batch.conversation_id,
            status=batch.status,
            counts=IngestBatchCounts(**count_values),
            display_status=("FILE_PROCESSING_COMPLETED" if final_receipt_ready else "PROCESSING"),
            final_receipt_ready=final_receipt_ready,
            receipt=receipt,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
        )

    def _to_item_response(self, item: IngestItem) -> IngestItemResponse:
        """投影单项五阶段结果，明确隐藏内部任务和归档记录 ID。"""

        result = dict(item.result_json or {})
        execution_status = self._request_execution_status(item=item)
        batch = self.db.get(IngestBatch, item.batch_id)
        attached_request = str(batch.user_request or "").strip() if batch is not None else ""
        if execution_status is not None:
            user_task_status = execution_status
        elif not attached_request:
            user_task_status = "NOT_REQUESTED"
        elif IngestionWorkflow._is_default_organization_request(attached_request):
            user_task_status = (
                "COMPLETED"
                if item.status in {"SUCCEEDED", "PARTIAL"}
                else "FAILED"
                if item.status == "FAILED"
                else "PENDING"
            )
        else:
            user_task_status = (
                "EXCLUDED"
                if item.status in {"FAILED", "CANCELLED", "EXPIRED", "SKIPPED"}
                else "PENDING"
            )
        extraction_status = str(result.get("external_extraction_status") or "")
        if not extraction_status:
            extraction_status = (
                "COMPLETED"
                if item.final_document_id
                else "FAILED"
                if item.status == "FAILED" and item.stage in {"PARSE_STAGING", "NEAR_CHECK"}
                else "RUNNING"
                if item.stage in {"PARSE_STAGING", "NEAR_CHECK"}
                else "PENDING"
            )
        published = bool(item.final_working_copy_id and item.status in {"SUCCEEDED", "PARTIAL"})

        return IngestItemResponse(
            id=item.id,
            client_item_id=item.client_item_id,
            source_root_ref=item.source_root_ref,
            source_relative_path=item.source_relative_path,
            original_filename=item.original_filename,
            expected_size=item.expected_size,
            source_mtime_ns=item.source_mtime_ns,
            expected_sha256=item.expected_sha256,
            actual_sha256=item.actual_sha256,
            workflow_revision=item.workflow_revision,
            stage=item.stage,
            status=item.status,
            ingest_status=item.status,
            extraction_status=extraction_status,
            organization_status=(
                "COMPLETED"
                if published
                else "FAILED"
                if item.status == "FAILED" and item.stage == "ORGANIZE"
                else "RUNNING"
                if item.stage == "ORGANIZE"
                else "PENDING"
            ),
            index_status=(
                "COMPLETED"
                if published
                else "FAILED"
                if item.status == "FAILED" and item.stage == "INDEX"
                else "RUNNING"
                if item.stage == "INDEX"
                else "PENDING"
            ),
            user_task_status=user_task_status,
            decision=item.decision,
            final_document_id=item.final_document_id,
            final_version_id=item.final_version_id,
            final_working_copy_id=item.final_working_copy_id,
            error=dict(item.error_json or {}),
            result=result,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )

    def _request_execution_status(self, *, item: IngestItem) -> str | None:
        """批量缓存同一批次的附带请求状态，避免逐文件回执产生 N+1 查询。"""

        by_item = self._execution_status_cache.get(item.batch_id)
        if by_item is None:
            by_item = {}
            executions = (
                self.db.query(IngestRequestExecution)
                .filter(IngestRequestExecution.batch_id == item.batch_id)
                .order_by(IngestRequestExecution.created_at.desc())
                .all()
            )
            for execution in executions:
                for item_id in list(execution.item_ids_json or []):
                    by_item.setdefault(str(item_id), execution.status)
            self._execution_status_cache[item.batch_id] = by_item
        return by_item.get(item.id)

    @staticmethod
    def _batch_fingerprint(request: IngestBatchCreateRequest) -> str:
        """计算稳定业务载荷摘要，排除每次重试可能变化的观测 request_id。"""

        payload = request.model_dump(
            mode="json",
            exclude={"request_id", "idempotency_key"},
        )
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _raise_error(
        status_code: int,
        code: str,
        message: str,
        *,
        details: dict | None = None,
    ) -> None:
        """抛出统一错误 Envelope 可识别的结构化业务异常。"""

        raise HTTPException(
            status_code=status_code,
            detail={"code": code, "message": message, "details": details},
        )
