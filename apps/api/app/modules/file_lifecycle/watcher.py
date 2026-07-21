"""受管原始目录近实时监听进程。

该进程只比较轻量元数据、记录 `managed_file_events` 并创建全量对账任务；
它不计算正文、不复制文件、不更新 ManagedFile，从而避免监听回调越过异步边界。
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.db.models import ManagedFileEvent, ManagedRoot
from app.modules.file_lifecycle.scheduler import enqueue_reconciliation_jobs
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.service import sync_configured_managed_roots


@dataclass(frozen=True)
class ObservedFile:
    """watcher 内存中的文件轻量快照，不承载文件内容。"""

    size_bytes: int
    modified_ns: int


def snapshot_root(root: ManagedRoot) -> dict[str, ObservedFile]:
    """遍历一个只读受管原始目录并生成相对路径元数据快照。"""

    base = Path(root.container_path).resolve()
    if not base.exists() or not base.is_dir():
        return {}
    snapshot: dict[str, ObservedFile] = {}
    for current, directories, filenames in os.walk(base, followlinks=False):
        # 符号链接目录不进入，防止 watcher 越过已授权根目录。
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for filename in filenames:
            path = Path(current) / filename
            if path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                relative_path = path.relative_to(base).as_posix()
            except (OSError, ValueError):
                continue
            snapshot[relative_path] = ObservedFile(stat.st_size, stat.st_mtime_ns)
    return snapshot


def record_snapshot_changes(
    *,
    root: ManagedRoot,
    previous: dict[str, ObservedFile],
    current: dict[str, ObservedFile],
) -> int:
    """把新增、修改和删除转换为去重事件，并只入队一次对账任务。"""

    changes: list[tuple[str, str, ObservedFile | None]] = []
    for relative_path in sorted(current.keys() - previous.keys()):
        changes.append(("CREATED", relative_path, current[relative_path]))
    for relative_path in sorted(previous.keys() - current.keys()):
        changes.append(("DELETED", relative_path, previous[relative_path]))
    for relative_path in sorted(current.keys() & previous.keys()):
        if current[relative_path] != previous[relative_path]:
            changes.append(("MODIFIED", relative_path, current[relative_path]))
    if not changes:
        return 0

    with SessionLocal() as db:
        for event_type, relative_path, observed in changes:
            raw_key = ":".join(
                [
                    root.id,
                    event_type,
                    relative_path,
                    str(observed.size_bytes if observed else 0),
                    str(observed.modified_ns if observed else 0),
                ]
            )
            event = ManagedFileEvent(
                root_id=root.id,
                event_type=event_type,
                source_relative_path=relative_path,
                observed_size=observed.size_bytes if observed else None,
                observed_mtime=(
                    datetime.fromtimestamp(observed.modified_ns / 1_000_000_000, tz=timezone.utc)
                    if observed
                    else None
                ),
                origin="SYSTEM_ARCHIVE" if root.archive_write_enabled else "EXTERNAL",
                deduplication_key=hashlib.sha256(raw_key.encode("utf-8")).hexdigest(),
                status="PENDING",
            )
            try:
                with db.begin_nested():
                    db.add(event)
                    db.flush()
            except IntegrityError:
                # 相同元数据事件已经记录时只保留第一条，避免大文件连续通知造成事件风暴。
                continue
        FilesystemJobQueue(db).create_job(
            job_type="RECONCILE_MANAGED_ROOT",
            queue_name="RECONCILE",
            root_id=root.id,
            created_by=None,
            deduplication_key=f"reconcile-managed-root:{root.id}",
            reuse_completed=True,
            payload={"root_key": root.root_key, "reason": "watcher"},
        )
        db.commit()
    return len(changes)


def run_managed_root_watcher(*, poll_seconds: float = 2.0) -> None:
    """持续监听已启用受管根；启动和异常恢复都额外提交全量对账。"""

    settings = get_settings()
    if not settings.managed_root_watch_enabled:
        return
    with SessionLocal() as db:
        roots = sync_configured_managed_roots(db, scan=False)
        enqueue_reconciliation_jobs(db=db)
        db.commit()
        root_ids = [root.id for root in roots]
    snapshots: dict[str, dict[str, ObservedFile]] = {}
    while True:
        with SessionLocal() as db:
            roots = (
                db.query(ManagedRoot)
                .filter(ManagedRoot.id.in_(root_ids), ManagedRoot.enabled.is_(True))
                .all()
            )
            detached = list(roots)
            for root in detached:
                db.expunge(root)
        for root in detached:
            current = snapshot_root(root)
            previous = snapshots.get(root.id)
            if previous is not None:
                record_snapshot_changes(root=root, previous=previous, current=current)
            snapshots[root.id] = current
        time.sleep(max(0.5, poll_seconds))


def main() -> None:
    """watcher 命令行入口。"""

    poll_seconds = float(os.getenv("MANAGED_ROOT_WATCH_POLL_SECONDS", "2"))
    run_managed_root_watcher(poll_seconds=poll_seconds)


if __name__ == "__main__":
    main()
