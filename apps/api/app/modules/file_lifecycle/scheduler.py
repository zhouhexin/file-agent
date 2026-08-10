"""三层文件生命周期的启动与定时同步入队器。

该模块只写持久化任务，绝不在 API 启动钩子中扫描目录或复制文件。
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import ManagedRoot, WorkingCopyRoot
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
    settings = get_settings()
    if settings.neo4j_sync_enabled and settings.graph_projection_worker_enabled:
        # 全量投影只作为一次性 bootstrap；后续正式分类变化通过 PostgreSQL outbox
        # 增量投影，API 重启不再同步执行 Neo4j sync_all。
        bootstrap_job = queue.create_job(
            job_type="GRAPH_BOOTSTRAP_PROJECTION",
            queue_name="GRAPH",
            root_id=None,
            created_by=created_by,
            deduplication_key="graph-bootstrap-projection:graph-v2",
            priority=20,
            payload={"projection_version": "graph-v2"},
        )
        incremental_job = queue.create_job(
            job_type="PROJECT_GRAPH_OUTBOX",
            queue_name="GRAPH",
            root_id=None,
            created_by=created_by,
            deduplication_key="project-graph-outbox",
            reuse_completed=True,
            priority=30,
            payload={"batch_size": settings.graph_projection_batch_size},
        )
        job_ids.extend([bootstrap_job.id, incremental_job.id])
    roots = sync_configured_managed_roots(db, scan=False, created_by=created_by)
    repair_roots = {root.id: root for root in roots}
    for root in (
        db.query(ManagedRoot)
        .join(WorkingCopyRoot, WorkingCopyRoot.managed_root_id == ManagedRoot.id)
        .all()
    ):
        repair_roots[root.id] = root
    # 上传归档根故意不参加普通目录扫描，但它已有的历史工作副本仍必须迁出
    # “待整理/待确认”并修复为 shared/upload_archive 前缀。
    for root in repair_roots.values():
        repair_job = queue.create_job(
            job_type="REPAIR_WORKING_COPY_LAYOUT",
            queue_name="RECONCILE",
            root_id=root.id,
            created_by=created_by,
            deduplication_key=f"repair-working-copy-layout-v2:{root.id}",
            priority=10,
            payload={"root_key": root.root_key, "reason": "startup-layout-repair-v2"},
        )
        job_ids.append(repair_job.id)
    for root in roots:
        job = queue.create_job(
            job_type="RECONCILE_MANAGED_ROOT",
            queue_name="RECONCILE",
            root_id=root.id,
            created_by=created_by,
            deduplication_key=f"reconcile-managed-root:{root.id}",
            reuse_completed=True,
            priority=50,
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
