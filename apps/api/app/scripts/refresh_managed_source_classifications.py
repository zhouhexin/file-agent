"""按当前动态分类身份检查并入队受管源分类刷新任务。"""

from __future__ import annotations

import argparse
from collections import Counter

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import (
    FilesystemJob,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
    User,
)
from app.modules.classification.freshness import (
    ClassificationFreshness,
    classification_refresh_deduplication_key,
    classification_refresh_priority,
    current_classification_identity,
    inspect_managed_source_classification,
)
from app.modules.managed_files.jobs import FilesystemJobQueue


def inspect_and_enqueue(*, root_key: str, enqueue: bool) -> dict[str, object]:
    """检查精确受管根；显式 enqueue 时只提交可重建分类任务。"""

    settings = get_settings()
    with SessionLocal() as db:
        root = db.query(ManagedRoot).filter(ManagedRoot.root_key == root_key).one()
        user_id = str(
            root.created_by
            or db.query(User.id).order_by(User.created_at.asc()).scalar()
            or ""
        )
        if not user_id:
            raise RuntimeError("受管源分类刷新缺少可审计用户")
        identity = current_classification_identity(
            db=db,
            settings=settings,
            user_id=user_id,
        )
        revisions = (
            db.query(ManagedFileRevision)
            .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
            .filter(
                ManagedFile.root_id == root.id,
                ManagedFile.status == "ACTIVE",
                ManagedFileRevision.is_current.is_(True),
                ManagedFileRevision.status == "READY",
            )
            .all()
        )
        counts: Counter[str] = Counter()
        job_ids: list[str] = []
        queue = FilesystemJobQueue(db)
        for revision in revisions:
            freshness = inspect_managed_source_classification(
                db=db,
                revision=revision,
                identity=identity,
            )
            counts[freshness.value] += 1
            if not enqueue or freshness is ClassificationFreshness.CURRENT:
                continue
            job = queue.create_job(
                job_type="REFRESH_MANAGED_SOURCE_CLASSIFICATION",
                queue_name="SOURCE_ANALYSIS",
                root_id=root.id,
                created_by=user_id,
                priority=classification_refresh_priority(settings),
                deduplication_key=classification_refresh_deduplication_key(
                    revision_id=revision.id,
                    identity=identity,
                ),
                reuse_completed=True,
                retry_failed=True,
                payload={
                    "managed_file_revision_id": revision.id,
                    "user_id": user_id,
                    "taxonomy_key": identity.taxonomy_key,
                    "taxonomy_version": identity.taxonomy_version,
                    "classifier_version": identity.classifier_version,
                },
            )
            job = queue.promote_pending_job(
                job=job,
                priority=classification_refresh_priority(settings),
            )
            job_ids.append(str(job.id))
        if enqueue:
            db.commit()
        refresh_jobs = (
            db.query(FilesystemJob)
            .filter(
                FilesystemJob.root_id == root.id,
                FilesystemJob.job_type
                == "REFRESH_MANAGED_SOURCE_CLASSIFICATION",
                FilesystemJob.deduplication_key.like(f"%:{identity.fingerprint}"),
            )
            .all()
        )
        job_status_counts = Counter(job.status for job in refresh_jobs)
        failed_jobs = [job for job in refresh_jobs if job.status == "FAILED"]
        active_source_jobs = (
            db.query(FilesystemJob)
            .filter(
                FilesystemJob.root_id == root.id,
                FilesystemJob.queue_name == "SOURCE_ANALYSIS",
                FilesystemJob.status.in_({"PENDING", "RUNNING"}),
            )
            .all()
        )
        source_queue_counts = Counter(
            f"{job.job_type}:{job.status}" for job in active_source_jobs
        )
        return {
            "root_key": root_key,
            "mode": "enqueue" if enqueue else "dry-run",
            "identity": {
                "taxonomy_key": identity.taxonomy_key,
                "taxonomy_version": identity.taxonomy_version,
                "classifier_version": identity.classifier_version,
                "fingerprint": identity.fingerprint,
            },
            "revision_count": len(revisions),
            "freshness_counts": dict(sorted(counts.items())),
            "job_ids": job_ids,
            "job_status_counts": dict(sorted(job_status_counts.items())),
            "failed_jobs": [
                {
                    "job_id": str(job.id),
                    "error_message": str(job.error_message or ""),
                }
                for job in failed_jobs[:10]
            ],
            "source_queue_counts": dict(sorted(source_queue_counts.items())),
            "running_source_jobs": [
                {
                    "job_id": str(job.id),
                    "job_type": job.job_type,
                    "started_at": job.started_at.isoformat()
                    if job.started_at is not None
                    else None,
                    "lease_owner": str(job.lease_owner or ""),
                }
                for job in active_source_jobs
                if job.status == "RUNNING"
            ][:10],
        }


def main() -> None:
    """解析精确 root key，默认 dry-run，显式开关后才写入任务。"""

    parser = argparse.ArgumentParser(
        description="按当前 taxonomy 与分类器身份刷新受管源分类"
    )
    parser.add_argument("--root-key", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计当前、过期和缺失数量（默认行为）",
    )
    mode.add_argument(
        "--enqueue",
        action="store_true",
        help="为过期或缺失分类提交刷新任务",
    )
    args = parser.parse_args()
    print(inspect_and_enqueue(root_key=args.root_key, enqueue=args.enqueue))


if __name__ == "__main__":
    main()
