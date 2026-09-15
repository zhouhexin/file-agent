"""显式重试指定受管根下的可恢复失败任务。

该脚本是运维边界：默认只预览，只有 ``--apply`` 才会重开任务。
它不修改受管原件，不重试已经确认不存在的源路径，并为每次操作
生成可审计批次清单。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import log_event
from app.db.models import (
    FilesystemJob,
    FilesystemJobEvent,
    ManagedFile,
    ManagedFileRevision,
    ManagedRoot,
    utcnow,
)
from app.modules.managed_files.jobs import FilesystemJobQueue


RETRYABLE_JOB_TYPES = {
    "ANALYZE_MANAGED_FILE_REVISION",
    "REFRESH_MANAGED_SOURCE_CLASSIFICATION",
    "MATERIALIZE_WORKING_COPY",
}
MISSING_SOURCE_ERROR = "relative_path 指向的文件不存在。"


def retry_failed_managed_jobs(
    *,
    db: Session,
    root_key: str,
    apply: bool,
    retry_priority: int = 10,
) -> dict[str, object]:
    """重开指定受管根的可恢复失败任务并返回逐文件清单。

    工作副本任务只在当前源修订已 READY 时重开，防止 MATERIALIZE
    worker 在源分析完成前再次失败。源文件不存在时必须由扫描协调，
    不得用无效重试掩盖。
    """

    root = db.query(ManagedRoot).filter(ManagedRoot.root_key == root_key).one()
    revision_rows = (
        db.query(ManagedFileRevision, ManagedFile)
        .join(ManagedFile, ManagedFile.id == ManagedFileRevision.managed_file_id)
        .filter(ManagedFile.root_id == root.id)
        .all()
    )
    revision_by_id = {
        str(revision.id): (revision, managed_file)
        for revision, managed_file in revision_rows
    }
    failed_jobs = (
        db.query(FilesystemJob)
        .filter(
            FilesystemJob.status == "FAILED",
            FilesystemJob.job_type.in_(RETRYABLE_JOB_TYPES),
        )
        .order_by(FilesystemJob.created_at.asc())
        .all()
    )

    batch_id = str(uuid4())
    batch_started_at = utcnow()
    candidates: list[
        tuple[FilesystemJob, ManagedFileRevision, ManagedFile, str]
    ] = []
    skipped: list[dict[str, str]] = []
    for job in failed_jobs:
        revision_id = str(
            (job.payload_json or {}).get("managed_file_revision_id") or ""
        )
        revision_pair = revision_by_id.get(revision_id)
        if revision_pair is None:
            continue
        revision, managed_file = revision_pair
        error_message = str(job.error_message or "")
        if not job.deduplication_key:
            skipped.append(
                _job_manifest_item(
                    job=job,
                    revision=revision,
                    managed_file=managed_file,
                    reason="MISSING_DEDUPLICATION_KEY",
                )
            )
            continue
        if MISSING_SOURCE_ERROR in error_message:
            skipped.append(
                _job_manifest_item(
                    job=job,
                    revision=revision,
                    managed_file=managed_file,
                    reason="SOURCE_MISSING",
                )
            )
            continue
        if job.job_type == "MATERIALIZE_WORKING_COPY" and (
            not revision.is_current or revision.status != "READY"
        ):
            skipped.append(
                _job_manifest_item(
                    job=job,
                    revision=revision,
                    managed_file=managed_file,
                    reason="SOURCE_ANALYSIS_NOT_READY",
                )
            )
            continue
        candidates.append((job, revision, managed_file, error_message))

    retried: list[dict[str, str]] = []
    if apply:
        queue = FilesystemJobQueue(db)
        for job, revision, managed_file, previous_error_message in candidates:
            payload = {
                **dict(job.payload_json or {}),
                "explicit_retry_batch_id": batch_id,
            }
            reopened = queue.create_job(
                job_type=job.job_type,
                root_id=job.root_id,
                created_by=job.created_by,
                payload=payload,
                queue_name=job.queue_name,
                deduplication_key=job.deduplication_key,
                priority=min(int(job.priority), int(retry_priority)),
                max_attempts=job.max_attempts,
                retry_failed=True,
            )
            reopened = queue.promote_pending_job(
                job=reopened,
                priority=retry_priority,
            )
            # create_job 重开既有幂等任务时保留原 payload；这里只增加
            # 本次显式重试批次，便于独立日志和审计追踪。
            reopened.payload_json = payload
            db.add(
                FilesystemJobEvent(
                    job_id=reopened.id,
                    level="WARNING",
                    message="管理员显式重试受管文件失败任务",
                    details_json={
                        "retry_batch_id": batch_id,
                        "root_key": root_key,
                        "previous_error_message": previous_error_message,
                    },
                )
            )
            item = _job_manifest_item(
                job=reopened,
                revision=revision,
                managed_file=managed_file,
                reason="EXPLICIT_RETRY",
            )
            retried.append(item)
            log_event(
                "managed_jobs.failed_retry.queued",
                status="PENDING",
                job_id=str(reopened.id),
                managed_file_revision_id=str(revision.id),
                root_id=str(root.id),
                retry_batch_id=batch_id,
                message="受管文件失败任务已显式重新入队",
            )
        db.commit()

    counts = Counter(item[0].job_type for item in candidates)
    skipped_counts = Counter(item["reason"] for item in skipped)
    return {
        "mode": "apply" if apply else "dry-run",
        "retry_batch_id": batch_id,
        "retry_started_at": batch_started_at.isoformat(),
        "root_key": root_key,
        "candidate_count": len(candidates),
        "candidate_counts": dict(sorted(counts.items())),
        "retried_count": len(retried),
        "job_ids": [item["job_id"] for item in retried],
        "files": retried if apply else [
            _job_manifest_item(
                job=job,
                revision=revision,
                managed_file=managed_file,
                reason="DRY_RUN_CANDIDATE",
            )
            for job, revision, managed_file, _previous_error_message in candidates
        ],
        "skipped_count": len(skipped),
        "skipped_counts": dict(sorted(skipped_counts.items())),
        "skipped": skipped,
    }


def _job_manifest_item(
    *,
    job: FilesystemJob,
    revision: ManagedFileRevision,
    managed_file: ManagedFile,
    reason: str,
) -> dict[str, str]:
    """生成不包含原文内容或宿主机绝对路径的审计清单项。"""

    return {
        "job_id": str(job.id),
        "job_type": str(job.job_type),
        "managed_file_revision_id": str(revision.id),
        "filename": str(managed_file.filename),
        "source_relative_path": str(managed_file.relative_path),
        "reason": reason,
    }


def _write_manifest(result: dict[str, object]) -> Path:
    """把重试对象和 job_id 写入服务器独立运维清单。"""

    log_dir = Path(get_settings().log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    batch_id = str(result["retry_batch_id"])
    manifest_path = log_dir / f"failed-managed-retry-{batch_id}.json"
    manifest_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path


def main() -> None:
    """解析受管根和显式写入开关，输出机器可读批次结果。"""

    parser = argparse.ArgumentParser(
        description="显式重试指定受管根的可恢复失败任务"
    )
    parser.add_argument("--root-key", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正重新入队；未指定时只输出预览",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=10,
        help="显式重试任务优先级，数值越小越优先",
    )
    args = parser.parse_args()
    if args.priority < 0:
        parser.error("--priority 不能为负数")

    with SessionLocal() as db:
        result = retry_failed_managed_jobs(
            db=db,
            root_key=args.root_key,
            apply=args.apply,
            retry_priority=args.priority,
        )
    if args.apply:
        result["manifest_path"] = str(_write_manifest(result))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
