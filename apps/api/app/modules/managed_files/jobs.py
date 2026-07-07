"""文件系统异步任务队列。

P0 使用数据库表作为轻量队列；PostgreSQL 部署可扩展为 SKIP LOCKED，多 worker 并发领取。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

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
    ) -> FilesystemJob:
        """创建 PENDING 任务并写入事件。"""

        job = FilesystemJob(
            job_type=job_type,
            root_id=root_id,
            status="PENDING",
            progress_current=0,
            progress_total=0,
            payload_json=payload,
            result_json={},
            created_by=created_by,
        )
        self.db.add(job)
        self.db.flush()
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已创建")
        return job

    def claim_next(self, *, worker_id: str) -> FilesystemJob | None:
        """领取下一个 PENDING 任务并标记 RUNNING。"""

        query = self.db.query(FilesystemJob).filter(FilesystemJob.status == "PENDING").order_by(FilesystemJob.created_at.asc())
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        job = query.first()
        if job is None:
            return None
        job.status = "RUNNING"
        job.locked_by = worker_id
        job.locked_at = utcnow()
        job.updated_at = utcnow()
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已被 worker 领取", details={"worker_id": worker_id})
        self.db.flush()
        return job

    def mark_completed(self, *, job: FilesystemJob, result: dict) -> FilesystemJob:
        """标记任务完成。"""

        job.status = "COMPLETED"
        job.result_json = result
        job.updated_at = utcnow()
        self.repository.create_event(job_id=job.id, level="INFO", message="任务已完成", details=result)
        self.db.flush()
        return job

    def mark_failed(self, *, job: FilesystemJob, error_message: str) -> FilesystemJob:
        """标记任务失败。"""

        job.status = "FAILED"
        job.error_message = error_message
        job.updated_at = utcnow()
        self.repository.create_event(job_id=job.id, level="ERROR", message=error_message)
        self.db.flush()
        return job
