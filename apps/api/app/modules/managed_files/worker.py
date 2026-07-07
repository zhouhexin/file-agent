"""受管目录异步 worker。

该模块负责消费 filesystem_jobs 中的只读扫描任务，
避免聊天请求线程直接遍历服务器目录。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.logging import log_context, log_event, new_request_id
from app.db.models import FilesystemJob
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.repository import ManagedFileRepository
from app.modules.managed_files.scanner import ManagedFileScanner


def process_next_filesystem_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    worker_id: str = "filesystem-worker",
) -> str | None:
    """处理一个待执行文件系统任务，没有任务时返回 None。"""

    db = session_factory()
    try:
        queue = FilesystemJobQueue(db)
        job = queue.claim_next(worker_id=worker_id)
        if job is None:
            db.commit()
            return None

        job_id = str(job.id)
        with log_context(request_id=new_request_id()):
            log_event(
                "filesystem.worker.started",
                agent_run_id=job_id,
                job_id=job_id,
                status=job.status,
                message="文件系统任务开始执行",
            )
            try:
                _process_job(db=db, job=job)
                db.commit()
                log_event(
                    "filesystem.worker.completed",
                    agent_run_id=job_id,
                    job_id=job_id,
                    status=job.status,
                    message="文件系统任务执行完成",
                )
            except Exception as exc:
                db.rollback()
                failed_db = session_factory()
                try:
                    failed_job = failed_db.get(FilesystemJob, job_id)
                    if failed_job is not None:
                        FilesystemJobQueue(failed_db).mark_failed(job=failed_job, error_message=str(exc))
                        failed_db.commit()
                    log_event(
                        "filesystem.worker.failed",
                        level="ERROR",
                        agent_run_id=job_id,
                        job_id=job_id,
                        status="FAILED",
                        error_code=exc.__class__.__name__,
                        message=str(exc),
                    )
                finally:
                    failed_db.close()
                raise
        return job_id
    finally:
        db.close()


def run_filesystem_worker(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    worker_id: str = "filesystem-worker",
    poll_seconds: float = 3.0,
) -> None:
    """持续轮询数据库任务队列。"""

    while True:
        processed = process_next_filesystem_job(session_factory=session_factory, worker_id=worker_id)
        if processed is None:
            time.sleep(poll_seconds)


def _process_job(*, db: Session, job: FilesystemJob) -> None:
    """按任务类型执行具体处理逻辑。"""

    if job.job_type != "SCAN_MANAGED_ROOT":
        raise ValueError(f"Unsupported filesystem job type: {job.job_type}")
    if not job.root_id:
        raise ValueError("SCAN_MANAGED_ROOT 缺少 root_id")
    root = ManagedFileRepository(db).get_root(job.root_id)
    if root is None or not root.enabled:
        raise ValueError("Managed root not found")
    scan_run = ManagedFileScanner(db).scan_root(root, job_id=job.id)
    FilesystemJobQueue(db).mark_completed(
        job=job,
        result={
            "scan_run_id": scan_run.id,
            "files_discovered": scan_run.files_discovered,
            "files_updated": scan_run.files_updated,
            "files_missing": scan_run.files_missing,
            "errors": scan_run.errors,
        },
    )


def main() -> None:
    """worker 命令行入口。"""

    worker_id = os.getenv("FILESYSTEM_WORKER_ID", "filesystem-worker")
    poll_seconds = float(os.getenv("FILESYSTEM_WORKER_POLL_SECONDS", "3"))
    run_filesystem_worker(worker_id=worker_id, poll_seconds=poll_seconds)


if __name__ == "__main__":
    main()
