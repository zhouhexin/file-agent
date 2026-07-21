"""三层文件生命周期的启动与定时同步入队器。

该模块只写持久化任务，绝不在 API 启动钩子中扫描目录或复制文件。
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.service import sync_configured_managed_roots


def enqueue_reconciliation_jobs(*, db: Session, created_by: str | None = None) -> list[str]:
    """幂等提交上传归档补偿和全部受管原始目录同步任务。"""

    queue = FilesystemJobQueue(db)
    job_ids: list[str] = []
    upload_job = queue.create_job(
        job_type="RECONCILE_UPLOAD_ARCHIVES",
        queue_name="RECONCILE",
        root_id=None,
        created_by=created_by,
        deduplication_key="reconcile-upload-archives",
        reuse_completed=True,
        payload={"reason": "startup-or-scheduler"},
    )
    job_ids.append(upload_job.id)
    roots = sync_configured_managed_roots(db, scan=False, created_by=created_by)
    for root in roots:
        job = queue.create_job(
            job_type="RECONCILE_MANAGED_ROOT",
            queue_name="RECONCILE",
            root_id=root.id,
            created_by=created_by,
            deduplication_key=f"reconcile-managed-root:{root.id}",
            reuse_completed=True,
            payload={"root_key": root.root_key, "reason": "startup-or-scheduler"},
        )
        job_ids.append(job.id)
    db.flush()
    return job_ids


def run_reconciliation_scheduler(*, interval_seconds: int | None = None) -> None:
    """独立定时进程周期性入队；不扫描目录、不复制文件。"""

    settings = get_settings()
    interval = max(30, interval_seconds or settings.managed_root_reconcile_interval_seconds)
    while True:
        with SessionLocal() as db:
            enqueue_reconciliation_jobs(db=db)
            db.commit()
        time.sleep(interval)


def main() -> None:
    """scheduler 命令行入口。"""

    run_reconciliation_scheduler()


if __name__ == "__main__":
    main()
