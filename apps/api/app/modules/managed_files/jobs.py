"""文件系统异步任务队列。

P0 使用数据库表作为轻量队列；PostgreSQL 部署可扩展为 SKIP LOCKED，多 worker 并发领取。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import FilesystemJob, utcnow
from app.modules.managed_files.repository import FilesystemJobRepository


class FilesystemJobQueue:
    """文件系统任务队列服务。"""

    def __init__(self, db: Session) -> None:
        """保存数据库会话。"""

        self.db = db
        self.repository = FilesystemJobRepository(db)

    def create_job(
        self,
        *,
        job_type: str,
        root_id: str | None,
        created_by: str | None,
        payload: dict,
        queue_name: str = "RECONCILE",
        deduplication_key: str | None = None,
        priority: int = 100,
        max_attempts: int = 3,
        reuse_completed: bool = False,
        retry_failed: bool = False,
    ) -> FilesystemJob:
        """在当前事务中幂等创建 PENDING 任务并写入事件。

        自动扫描不得重新激活已达到终态的失败任务。只有管理员显式发起重处理时，
        调用方才能传入 ``retry_failed=True``；单个任务的尝试次数始终不超过三次。
        """

        bounded_max_attempts = max(1, min(3, int(max_attempts)))

        if deduplication_key:
            existing = (
                self.db.query(FilesystemJob)
                .filter(FilesystemJob.deduplication_key == deduplication_key)
                .one_or_none()
            )
            if existing is not None:
                if (retry_failed and existing.status == "FAILED") or (
                    reuse_completed and existing.status == "COMPLETED"
                ):
                    # 业务状态仍要求执行时允许补偿任务重置同一幂等键；不创建第二条任务，
                    # 从而保留完整尝试和事件历史。
                    existing.status = "PENDING"
                    existing.available_at = utcnow()
                    existing.error_message = None
                    existing.finished_at = None
                    existing.attempt_count = 0
                    existing.max_attempts = bounded_max_attempts
                    existing.lease_owner = None
                    existing.lease_expires_at = None
                    existing.execution_token = None
                    existing.updated_at = utcnow()
                    self.repository.create_event(
                        job_id=existing.id,
                        level="WARNING",
                        message="任务已由一致性补偿重新入队",
                    )
                    self.db.flush()
                return existing

        job = FilesystemJob(
            job_type=job_type,
            queue_name=queue_name,
            deduplication_key=deduplication_key,
            priority=priority,
            root_id=root_id,
            status="PENDING",
            progress_current=0,
            progress_total=0,
            attempt_count=0,
            max_attempts=bounded_max_attempts,
            available_at=utcnow(),
            payload_json=payload,
            result_json={},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已创建")
        return job

    def promote_pending_job(
        self,
        *,
        job: FilesystemJob,
        priority: int,
    ) -> FilesystemJob:
        """提升用户当前需要的待执行任务，但绝不重置失败次数。

        RUNNING 任务保持原租约；FAILED/COMPLETED 终态不会被该方法重新激活。
        因此用户重复检索不能绕过最多三次尝试的队列约束。
        """

        if job.status != "PENDING" or job.attempt_count >= job.max_attempts:
            return job
        target_priority = min(int(job.priority), int(priority))
        now = utcnow()
        available_at = job.available_at
        if available_at.tzinfo is None:
            # SQLite 测试和部分历史数据可能返回无时区 datetime；只用于比较，
            # 写回仍统一使用 utcnow() 的带时区值。
            available_at = available_at.replace(tzinfo=now.tzinfo)
        changed = target_priority != job.priority or available_at > now
        if not changed:
            return job
        job.priority = target_priority
        job.available_at = now
        job.updated_at = now
        self.repository.create_event(
            job_id=job.id,
            level="INFO",
            message="任务已按当前用户请求提升优先级",
        )
        self.db.flush()
        return job

    def claim_next(self, *, worker_id: str, queue_names: set[str] | None = None) -> FilesystemJob | None:
        """通过可恢复租约领取下一个可执行任务。

        PostgreSQL 使用 SKIP LOCKED 支持多 worker；租约过期的 RUNNING 任务可以安全重领。
        """

        now = utcnow()
        # 兼容升级前可能写入的大于三次配置，领取前统一收紧，避免旧积压任务继续超额重试。
        (
            self.db.query(FilesystemJob)
            .filter(FilesystemJob.max_attempts > 3)
            .update({FilesystemJob.max_attempts: 3}, synchronize_session=False)
        )
        pending_exhausted = (
            self.db.query(FilesystemJob)
            .filter(
                FilesystemJob.status == "PENDING",
                FilesystemJob.attempt_count >= FilesystemJob.max_attempts,
            )
            .all()
        )
        for stale_job in pending_exhausted:
            self.mark_failed(job=stale_job, error_message="任务已达到最大尝试次数")
        exhausted = (
            self.db.query(FilesystemJob)
            .filter(
                FilesystemJob.status == "RUNNING",
                FilesystemJob.lease_expires_at < now,
                FilesystemJob.attempt_count >= FilesystemJob.max_attempts,
            )
            .all()
        )
        for stale_job in exhausted:
            self.mark_failed(job=stale_job, error_message="任务租约多次过期，已达到最大尝试次数")
        query = self.db.query(FilesystemJob).filter(
            FilesystemJob.attempt_count < FilesystemJob.max_attempts,
            or_(
                and_(FilesystemJob.status == "PENDING", FilesystemJob.available_at <= now),
                and_(FilesystemJob.status == "RUNNING", FilesystemJob.lease_expires_at < now),
            )
        )
        if queue_names:
            query = query.filter(FilesystemJob.queue_name.in_(queue_names))
        query = query.order_by(FilesystemJob.priority.asc(), FilesystemJob.created_at.asc())
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job = query.first()
        if job is None:
            return None
        job.status = "RUNNING"
        job.attempt_count += 1
        job.lease_owner = worker_id
        # 每次重新领取都生成新令牌；超时后被终止的旧执行即使迟到，也不能提交结果。
        job.execution_token = str(uuid4())
        job.lease_expires_at = now + timedelta(seconds=get_settings().filesystem_job_lease_seconds)
        job.heartbeat_at = now
        job.locked_by = worker_id
        job.locked_at = now
        job.started_at = job.started_at or now
        job.updated_at = now
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已被 worker 领取", details={"worker_id": worker_id})
        self.db.flush()
        return job

    def mark_completed(self, *, job: FilesystemJob, result: dict) -> FilesystemJob:
        """标记任务完成。"""

        job.status = "COMPLETED"
        job.result_json = result
        job.finished_at = utcnow()
        job.lease_expires_at = None
        job.lease_owner = None
        job.execution_token = None
        job.updated_at = job.finished_at
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已完成", details=result)
        self.db.flush()
        return job

    def mark_failed(
        self,
        *,
        job: FilesystemJob,
        error_message: str,
        event_details: dict | None = None,
    ) -> FilesystemJob:
        """标记任务失败，并在事件中保留可关联但不含堆栈的诊断字段。"""

        job.status = "FAILED"
        job.error_message = error_message
        job.finished_at = utcnow()
        job.lease_expires_at = None
        job.lease_owner = None
        job.execution_token = None
        job.updated_at = job.finished_at
        self.repository.create_event(
            job_id=job.id,
            level="ERROR",
            message=error_message,
            details=event_details,
        )
        self.db.flush()
        return job

    def heartbeat(self, *, job: FilesystemJob, worker_id: str) -> None:
        """续租运行中的任务；其他 worker 不能替当前租约持有者续租。"""

        if job.status != "RUNNING" or job.lease_owner != worker_id:
            raise RuntimeError("任务租约不属于当前 worker")
        now = utcnow()
        job.heartbeat_at = now
        job.lease_expires_at = now + timedelta(seconds=get_settings().filesystem_job_lease_seconds)
        job.updated_at = now
        self.db.flush()

    def mark_retry(
        self,
        *,
        job: FilesystemJob,
        error_message: str,
        retry_after_seconds: int,
        event_details: dict | None = None,
    ) -> FilesystemJob:
        """在未超过最大尝试次数时释放租约并延后重试。"""

        if job.attempt_count >= job.max_attempts:
            return self.mark_failed(
                job=job,
                error_message=error_message,
                event_details=event_details,
            )
        now = utcnow()
        job.status = "PENDING"
        job.error_message = error_message
        job.available_at = now + timedelta(seconds=max(1, retry_after_seconds))
        job.lease_owner = None
        job.execution_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.updated_at = now
        self.repository.create_event(
            job_id=job.id,
            level="WARNING",
            message="任务将在稍后重试",
            details={
                "attempt_count": job.attempt_count,
                "max_attempts": job.max_attempts,
                **dict(event_details or {}),
            },
        )
        self.db.flush()
        return job
