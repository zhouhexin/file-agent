"""导入批次状态投影与聚合。

既有文件生命周期仍是查重、归档和工作副本创建的执行事实；本模块把这些事实投影到 ``ingest_items``，
并统一计算批次状态。任何 API 或 MCP 都不能用某个子任务完成替代批次聚合。
"""

from __future__ import annotations

import hashlib
import re
from datetime import timezone

from sqlalchemy.orm import Session

from app.db.models import (
    Document,
    DocumentVersion,
    FilesystemJob,
    IngestBatch,
    IngestItem,
    IngestRequestExecution,
    UploadArchiveRecord,
    UploadDuplicateCandidate,
    UploadDuplicateReview,
    WorkingCopy,
    utcnow,
)
from app.modules.file_lifecycle.service import UploadLifecycleService
from app.modules.managed_files.jobs import FilesystemJobQueue


ACTIVE_PROCESSING_STATUSES = {
    "PENDING",
    "RUNNING",
    "WAITING_EXTERNAL_EXTRACTION",
    "WAITING_EXISTING_RESULT",
}
TERMINAL_OR_USER_WAIT_STATUSES = {
    "WAITING_DUPLICATE_CONFIRMATION",
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "SKIPPED",
}


class IngestionWorkflow:
    """同步逐项生命周期状态并计算符合混合批次规则的聚合状态。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def synchronize_batch(self, batch: IngestBatch) -> bool:
        """同步一个批次并在业务结果变化时递增 ``result_revision``。"""

        changed = False
        items = self.db.query(IngestItem).filter(IngestItem.batch_id == batch.id).all()
        for item in items:
            changed = self._synchronize_item(item) or changed
        aggregate = self._aggregate_status(items)
        if batch.status != aggregate:
            batch.status = aggregate
            changed = True
        changed = self._schedule_ready_extra_request(
            batch=batch,
            items=items,
            aggregate_status=aggregate,
        ) or changed
        if changed:
            batch.result_revision += 1
            self.db.flush()
        return changed

    def _schedule_ready_extra_request(
        self,
        *,
        batch: IngestBatch,
        items: list[IngestItem],
        aggregate_status: str,
    ) -> bool:
        """无自动处理项后，对本轮新完成文件安排一次固定范围的附带请求。

        等待重复确认是用户等待而不是运行态：已经成功整理的文件可以先执行总结；后来
        解决的条目会形成新的增量集合。分类/整理类文字与默认策略等价，不重复启动 AgentRun。
        """

        request = str(batch.user_request or "").strip()
        if not request or self._is_default_organization_request(request) or aggregate_status == "RUNNING":
            return False
        executions = (
            self.db.query(IngestRequestExecution)
            .filter(IngestRequestExecution.batch_id == batch.id)
            .all()
        )
        covered_item_ids = {
            str(item_id)
            for execution in executions
            for item_id in list(execution.item_ids_json or [])
        }
        eligible = [
            item
            for item in items
            if item.status in {"SUCCEEDED", "PARTIAL"}
            and item.final_document_id
            and item.id not in covered_item_ids
        ]
        if not eligible:
            return False
        groups = [[item] for item in eligible] if self._is_per_file_request(request) else [eligible]
        for group in groups:
            self._create_request_execution(batch=batch, items=group)
        return bool(groups)

    def _create_request_execution(
        self,
        *,
        batch: IngestBatch,
        items: list[IngestItem],
    ) -> None:
        """为一个固定文件集合创建一次附带请求及唯一队列任务。"""

        item_ids = sorted(item.id for item in items)
        document_ids = list(dict.fromkeys(str(item.final_document_id) for item in items))
        digest = hashlib.sha256("\n".join(item_ids).encode("utf-8")).hexdigest()
        execution = IngestRequestExecution(
            batch_id=batch.id,
            item_ids_json=item_ids,
            document_ids_json=document_ids,
            item_set_digest=digest,
            status="PENDING",
            conversation_id=batch.conversation_id,
            result_json={},
            error_json={},
        )
        self.db.add(execution)
        self.db.flush()
        job = FilesystemJobQueue(self.db).create_job(
            job_type="RUN_INGEST_EXTRA_REQUEST",
            queue_name="AGENT",
            root_id=None,
            created_by=batch.user_id,
            deduplication_key=f"ingest-extra-request:{batch.id}:{digest}",
            payload={
                "ingest_request_execution_id": execution.id,
                "ingest_batch_id": batch.id,
                "user_id": batch.user_id,
            },
        )
        execution.filesystem_job_id = job.id

    @staticmethod
    def _is_per_file_request(request: str) -> bool:
        """识别用户明确要求逐文件执行的只读请求，不让模型自行改变批次粒度。"""

        normalized = re.sub(r"\s+", "", request)
        return any(
            token in normalized
            for token in ("逐份", "逐个", "逐一", "分别", "每份", "每个文件", "每一份")
        )

    @staticmethod
    def _is_default_organization_request(request: str) -> bool:
        """识别仅重复默认分类/整理目标的附带文字，避免同一文件再次分类。"""

        normalized = re.sub(r"[\s，。！？、,.!?]", "", request).lower()
        if any(token in normalized for token in ("总结", "摘要", "读取", "查看内容", "讲解", "问", "提取")):
            return False
        organization_tokens = ("分类", "归类", "整理", "归档", "标准化命名", "自动命名", "建立索引", "索引")
        stripped = normalized
        for token in organization_tokens + (
            "帮我",
            "请",
            "这些文件",
            "这批文件",
            "文件",
            "按照系统默认",
            "按系统默认",
            "系统默认",
            "默认规则",
            "默认",
            "并",
            "和",
            "以及",
        ):
            stripped = stripped.replace(token, "")
        return bool(normalized) and not stripped and any(token in normalized for token in organization_tokens)

    def _synchronize_item(self, item: IngestItem) -> bool:
        """把上传确认、归档和活动工作副本事实映射为单项状态。"""

        if not item.upload_document_version_id:
            return False
        if item.status in {"SUCCEEDED", "PARTIAL"} and item.final_document_id:
            # 终态已经绑定稳定最终对象后不能被较早的子任务状态回退为 RUNNING。
            return False
        review = (
            self.db.query(UploadDuplicateReview)
            .filter(UploadDuplicateReview.upload_document_version_id == item.upload_document_version_id)
            .one_or_none()
        )
        archive = self.db.get(UploadArchiveRecord, item.archive_record_id) if item.archive_record_id else None
        before = self._snapshot(item)
        if review is not None and review.ingest_item_id is None:
            review.ingest_item_id = item.id
        if review is not None and review.status == "WAITING_CONFIRMATION":
            expires_at = review.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at < utcnow():
                review.status = "EXPIRED"
                review.revision += 1
                if archive is not None and archive.status not in {"ARCHIVED", "EXISTING_FILE_SELECTED"}:
                    cleanup = UploadLifecycleService(self.db).enqueue_reused_upload_cleanup(review=review)
                    archive.status = "CANCELLED"
                    archive.filesystem_job_id = cleanup.id
                    item.current_job_id = cleanup.id
                    version = self.db.get(DocumentVersion, review.upload_document_version_id)
                    document = self.db.get(Document, version.document_id) if version is not None else None
                    if document is not None:
                        # 确认过期后暂存文件会被删除，容量事实也必须立即停止计费。
                        document.status = "UPLOAD_CANCELLED"
        if review is not None and review.status == "WAITING_CONFIRMATION":
            item.status = "WAITING_DUPLICATE_CONFIRMATION"
            item.stage = "DUPLICATE_DECISION"
            item.current_job_id = review.duplicate_check_job_id
        elif review is not None and review.status == "EXPIRED":
            item.status = "EXPIRED"
            item.stage = "COMPLETED"
        elif review is not None and review.status == "RESOLVED" and review.decision == "CANCEL_UPLOAD":
            item.status = "CANCELLED"
            item.stage = "COMPLETED"
            item.decision = review.decision
            item.current_job_id = archive.filesystem_job_id if archive else None
        elif review is not None and review.status == "RESOLVED" and review.decision == "USE_EXISTING_FILE":
            working_copy = (
                self.db.get(WorkingCopy, review.selected_existing_working_copy_id)
                if review.selected_existing_working_copy_id
                else None
            )
            if working_copy is not None and working_copy.status == "ACTIVE":
                # 选择已有文件后，本次上传的 OCR 覆盖率不再代表最终文件质量。
                item.status = "SUCCEEDED"
                item.stage = "COMPLETED"
                item.decision = review.decision
                item.final_document_id = working_copy.document_id
                item.final_version_id = working_copy.current_version_id
                item.final_working_copy_id = working_copy.id
        elif review is not None and review.status == "RESOLVED" and review.decision == "WAIT_AND_REUSE":
            self._synchronize_wait_and_reuse(item=item, review=review, archive=archive)
        elif archive is not None and archive.status == "FAILED":
            item.status = "FAILED"
            item.stage = "COMPLETED"
            item.error_json = {
                "code": archive.last_error_code or "UPLOAD_LIFECYCLE_FAILED",
                "message": archive.last_error_message or "文件处理失败。",
            }
        elif archive is not None and archive.status == "NEEDS_REVIEW":
            item.status = "PARTIAL"
            item.stage = "COMPLETED"
            item.error_json = {
                "code": archive.last_error_code or "FILE_NEEDS_REVIEW",
                "message": archive.last_error_message or "文件需要人工处理。",
            }
        elif archive is not None and archive.status == "WAITING_EXTERNAL_EXTRACTION":
            item.status = "WAITING_EXTERNAL_EXTRACTION"
            item.stage = "PARSE_STAGING"
        elif archive is not None and archive.managed_file_id:
            working_copy = (
                self.db.query(WorkingCopy)
                .filter(
                    WorkingCopy.managed_file_id == archive.managed_file_id,
                )
                .order_by(WorkingCopy.created_at.asc())
                .first()
            )
            if (
                working_copy is not None
                and working_copy.status == "ACTIVE"
                and working_copy.current_version_id
            ):
                version = self.db.get(DocumentVersion, working_copy.current_version_id)
                extraction_partial = (
                    dict(item.result_json or {}).get("external_extraction_status") == "PARTIAL"
                )
                item.status = "PARTIAL" if extraction_partial else "SUCCEEDED"
                item.stage = "COMPLETED"
                item.decision = item.decision or "CONTINUE_UPLOAD"
                item.final_document_id = working_copy.document_id
                item.final_version_id = working_copy.current_version_id
                item.final_working_copy_id = working_copy.id
                item.result_json = {
                    **dict(item.result_json or {}),
                    "final_filename": working_copy.filename,
                    "original_filename": item.original_filename,
                    "rename_status": (
                        "NO_CHANGE" if working_copy.filename == item.original_filename else "COMPLETED"
                    ),
                    "content_sha256": version.sha256 if version is not None else item.actual_sha256,
                }
            elif working_copy is not None and working_copy.status == "ORGANIZING":
                # 归档记录只保存工作副本导入任务；真正的分析任务 ID 位于该任务结果中。
                # 必须继续投影分析任务终态，否则分析失败会让条目永久显示 RUNNING，用户也
                # 无法通过显式重试 API 恢复这一阶段。
                import_job = (
                    self.db.get(FilesystemJob, archive.filesystem_job_id)
                    if archive.filesystem_job_id
                    else None
                )
                analysis_job_id = str(
                    (import_job.result_json or {}).get("analysis_job_id")
                    if import_job is not None
                    else ""
                )
                analysis_job = self.db.get(FilesystemJob, analysis_job_id) if analysis_job_id else None
                item.current_job_id = analysis_job.id if analysis_job is not None else archive.filesystem_job_id
                item.stage = "ORGANIZE"
                if analysis_job is not None and analysis_job.status == "FAILED":
                    item.status = "FAILED"
                    item.error_json = {
                        "code": "FILESYSTEM_JOB_FAILED",
                        "message": analysis_job.error_message or "文件分析与整理失败。",
                    }
                else:
                    item.status = "RUNNING"
            else:
                lifecycle_job = (
                    self.db.get(FilesystemJob, archive.filesystem_job_id)
                    if archive.filesystem_job_id
                    else None
                )
                item.current_job_id = archive.filesystem_job_id
                item.stage = "ORGANIZE"
                if lifecycle_job is not None and lifecycle_job.status == "FAILED":
                    # 归档已发布但工作副本尚未产生时，当前任务就是导入任务；其失败
                    # 也必须进入条目终态，不能永久伪装成处理中。
                    item.status = "FAILED"
                    item.error_json = {
                        "code": "FILESYSTEM_JOB_FAILED",
                        "message": lifecycle_job.error_message or "工作副本导入失败。",
                    }
                else:
                    item.status = "RUNNING"
        elif archive is not None:
            item.status = "RUNNING"
            item.stage = self._stage_for_archive(archive.status)
            item.current_job_id = archive.filesystem_job_id
        if self._snapshot(item) != before:
            item.workflow_revision += 1
            return True
        return False

    def _synchronize_wait_and_reuse(
        self,
        *,
        item: IngestItem,
        review: UploadDuplicateReview,
        archive: UploadArchiveRecord | None,
    ) -> None:
        """等待固定同批主条目；主任务失败或取消时不自动另存新文件。"""

        candidate = (
            self.db.get(UploadDuplicateCandidate, review.selected_candidate_id)
            if review.selected_candidate_id
            else None
        )
        primary = (
            self.db.get(IngestItem, candidate.candidate_ingest_item_id)
            if candidate is not None and candidate.candidate_ingest_item_id
            else None
        )
        if primary is None:
            item.status = "FAILED"
            item.stage = "COMPLETED"
            item.error_json = {
                "code": "WAITED_ITEM_MISSING",
                "message": "等待复用的批内主文件不存在。",
            }
            return
        if primary.status == "SUCCEEDED" and primary.final_document_id:
            item.status = "SUCCEEDED"
            item.stage = "COMPLETED"
            item.final_document_id = primary.final_document_id
            item.final_version_id = primary.final_version_id
            item.final_working_copy_id = primary.final_working_copy_id
            item.result_json = {
                **dict(item.result_json or {}),
                "reused_from_ingest_item_id": primary.id,
                "new_logical_file_created": False,
            }
            self._schedule_waiting_copy_cleanup(item=item, review=review, archive=archive)
            return
        if primary.status in {"FAILED", "CANCELLED", "EXPIRED"}:
            item.status = "FAILED"
            item.stage = "COMPLETED"
            item.error_json = {
                "code": "WAITED_PRIMARY_NOT_AVAILABLE",
                "message": "等待复用的批内主文件未完成，本文件不会自动另存，也不会执行后续请求。",
                "primary_status": primary.status,
            }
            self._schedule_waiting_copy_cleanup(item=item, review=review, archive=archive)
            return
        item.status = "WAITING_EXISTING_RESULT"
        item.stage = "WAIT_EXISTING"
        item.current_job_id = primary.current_job_id

    def _schedule_waiting_copy_cleanup(
        self,
        *,
        item: IngestItem,
        review: UploadDuplicateReview,
        archive: UploadArchiveRecord | None,
    ) -> None:
        """终态只保留主文件映射，并异步清理后到成员的暂存字节。"""

        if archive is None or archive.status in {"EXISTING_FILE_SELECTED", "CANCELLED"}:
            return
        cleanup = UploadLifecycleService(self.db).enqueue_reused_upload_cleanup(review=review)
        archive.status = "EXISTING_FILE_SELECTED" if item.status == "SUCCEEDED" else "CANCELLED"
        archive.filesystem_job_id = cleanup.id
        item.current_job_id = cleanup.id
        version = self.db.get(DocumentVersion, review.upload_document_version_id)
        document = self.db.get(Document, version.document_id) if version else None
        if document is not None:
            document.status = (
                "UPLOAD_REPLACED_BY_EXISTING" if item.status == "SUCCEEDED" else "UPLOAD_CANCELLED"
            )

    @staticmethod
    def _snapshot(item: IngestItem) -> tuple:
        """返回会影响外部回执修订的稳定字段集合。"""

        return (
            item.status,
            item.stage,
            item.decision,
            item.current_job_id,
            item.final_document_id,
            item.final_version_id,
            item.final_working_copy_id,
            dict(item.error_json or {}),
            dict(item.result_json or {}),
        )

    @staticmethod
    def _stage_for_archive(status: str) -> str:
        """把既有归档状态映射为批次阶段。"""

        if status.startswith("DUPLICATE_CHECK"):
            return "EXACT_CHECK"
        if status in {"PENDING", "ARCHIVING", "ARCHIVED", "RETRY_WAIT"}:
            return "ARCHIVE"
        return "ORGANIZE"

    @staticmethod
    def _aggregate_status(items: list[IngestItem]) -> str:
        """按用户确认的混合批次语义计算后端状态。"""

        if not items:
            return "PENDING"
        statuses = [item.status for item in items]
        if any(status in ACTIVE_PROCESSING_STATUSES for status in statuses):
            return "RUNNING"
        if not all(status in TERMINAL_OR_USER_WAIT_STATUSES for status in statuses):
            return "RUNNING"
        succeeded = statuses.count("SUCCEEDED")
        partial = statuses.count("PARTIAL")
        failed = statuses.count("FAILED")
        waiting = statuses.count("WAITING_DUPLICATE_CONFIRMATION")
        expired = statuses.count("EXPIRED")
        cancelled = statuses.count("CANCELLED")
        if expired:
            # 确认期限是批次级交互终点：任一待确认条目过期后，批次必须明确显示
            # EXPIRED，同时逐项回执继续保留此前成功、失败、复用和取消事实。
            return "EXPIRED"
        if succeeded and not partial and not failed and not waiting and not expired:
            # 成功文件加用户主动取消仍表示其余处理目标已经完成。
            return "SUCCEEDED"
        if cancelled == len(statuses):
            return "CANCELLED"
        if failed == len(statuses):
            return "FAILED"
        return "PARTIAL"
