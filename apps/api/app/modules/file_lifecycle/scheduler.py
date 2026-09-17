"""三层文件生命周期的启动与定时同步入队器。

该模块只写持久化任务，绝不在 API 启动钩子中扫描目录、分析原始文件或复制工作副本。
"""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import FilesystemJob, ManagedRoot, WorkingCopyRoot, utcnow
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
    cleanup_job = queue.create_job(
        job_type="CLEANUP_EXTERNAL_EXTRACTION_RESOURCES",
        queue_name="FILE_OPERATION",
        root_id=None,
        created_by=created_by,
        deduplication_key="cleanup-external-extraction-resources",
        reuse_completed=True,
        priority=100,
        payload={"reason": "retention-policy"},
    )
    job_ids.append(cleanup_job.id)
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
        if not _periodic_managed_root_scan_due(db=db, root_id=root.id):
            continue
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


def _periodic_managed_root_scan_due(*, db: Session, root_id: str) -> bool:
    """判断常规全量扫描是否到期，避免大目录扫描结束后立刻再次启动。

    watcher 发现真实文件变化时走独立入队路径，不调用本方法，因此不会因周期冷却延迟增量同步。
    """

    active_job = (
        db.query(FilesystemJob.id)
        .filter(
            FilesystemJob.root_id == root_id,
            FilesystemJob.job_type.in_({"RECONCILE_MANAGED_ROOT", "SCAN_MANAGED_ROOT"}),
            FilesystemJob.status.in_({"PENDING", "RUNNING"}),
        )
        .first()
    )
    if active_job is not None:
        return False

    latest_scan = (
        db.query(FilesystemJob.finished_at)
        .filter(
            FilesystemJob.root_id == root_id,
            FilesystemJob.job_type == "SCAN_MANAGED_ROOT",
            FilesystemJob.status.in_({"COMPLETED", "FAILED"}),
            FilesystemJob.finished_at.is_not(None),
        )
        .order_by(FilesystemJob.finished_at.desc())
        .first()
    )
    if latest_scan is None or latest_scan[0] is None:
        return True
    settings = get_settings()
    finished_at = latest_scan[0]
    now = utcnow()
    if finished_at.tzinfo is None:
        finished_at = finished_at.replace(tzinfo=now.tzinfo)
    return finished_at + timedelta(
        seconds=settings.managed_root_full_scan_min_interval_seconds
    ) <= now


def run_reconciliation_scheduler(*, interval_seconds: int | None = None) -> None:
    """独立定时进程周期性入队；不扫描目录、不分析原件、不复制文件。"""

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
