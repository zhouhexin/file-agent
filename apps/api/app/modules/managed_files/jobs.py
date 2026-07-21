"""文件系统异步任务队列。

P0 使用数据库表作为轻量队列；PostgreSQL 部署可扩展为 SKIP LOCKED，多 worker 并发领取。
"""

from __future__ import annotations

from datetime import timedelta

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
    ) -> FilesystemJob:
        """在当前事务中幂等创建 PENDING 任务并写入事件。"""

        if deduplication_key:
            existing = (
                self.db.query(FilesystemJob)
                .filter(FilesystemJob.deduplication_key == deduplication_key)
                .one_or_none()
            )
            if existing is not None:
                if existing.status == "FAILED" or (reuse_completed and existing.status == "COMPLETED"):
                    # 业务状态仍要求执行时允许补偿任务重置同一幂等键；不创建第二条任务，
                    # 从而保留完整尝试和事件历史。
                    existing.status = "PENDING"
                    existing.available_at = utcnow()
                    existing.error_message = None
                    existing.finished_at = None
                    existing.attempt_count = 0
                    existing.lease_owner = None
                    existing.lease_expires_at = None
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
            max_attempts=max_attempts,
            available_at=utcnow(),
            payload_json=payload,
            result_json={},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已创建")
        return job

    def claim_next(self, *, worker_id: str, queue_names: set[str] | None = None) -> FilesystemJob | None:
        """通过可恢复租约领取下一个可执行任务。

        PostgreSQL 使用 SKIP LOCKED 支持多 worker；租约过期的 RUNNING 任务可以安全重领。
        """

        now = utcnow()
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
        job.updated_at = job.finished_at
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已完成", details=result)
        self.db.flush()
        return job

    def mark_failed(self, *, job: FilesystemJob, error_message: str) -> FilesystemJob:
        """标记任务失败。"""

        job.status = "FAILED"
        job.error_message = error_message
        job.finished_at = utcnow()
        job.lease_expires_at = None
        job.lease_owner = None
        job.updated_at = job.finished_at
        self.repository.create_event(job_id=job.id, level="ERROR", message=error_message)
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

    def mark_retry(self, *, job: FilesystemJob, error_message: str, retry_after_seconds: int) -> FilesystemJob:
        """在未超过最大尝试次数时释放租约并延后重试。"""

        if job.attempt_count >= job.max_attempts:
            return self.mark_failed(job=job, error_message=error_message)
        now = utcnow()
        job.status = "PENDING"
        job.error_message = error_message
        job.available_at = now + timedelta(seconds=max(1, retry_after_seconds))
        job.lease_owner = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.updated_at = now
        self.repository.create_event(
            job_id=job.id,
            level="WARNING",
            message="任务将在稍后重试",
            details={"attempt_count": job.attempt_count, "max_attempts": job.max_attempts},
        )
        self.db.flush()
        return job
