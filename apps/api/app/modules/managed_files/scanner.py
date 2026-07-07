"""受管目录只读扫描器。

扫描器只读取文件元数据并写入 managed_files，不打开正文、不修改原始文件。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import FilesystemScanRun, ManagedFile, ManagedRoot, utcnow
from app.modules.managed_files.path_policy import PathPolicyError, resolve_managed_relative_path


class ManagedFileScanner:
    """只读扫描受管目录并同步文件元数据。"""

    def __init__(self, db: Session) -> None:
        """保存数据库会话。"""

        self.db = db

    def scan_root(self, root: ManagedRoot, job_id: str | None = None) -> FilesystemScanRun:
        """扫描一个受管目录并返回扫描汇总。"""

        scan_run = FilesystemScanRun(root_id=root.id, job_id=job_id, status="RUNNING")
        self.db.add(scan_run)
        self.db.flush()

        root_path = Path(root.container_path)
        seen_paths: set[str] = set()
        files_updated = 0
        errors = 0
        if root_path.exists():
            for path in sorted(item for item in root_path.rglob("*") if item.is_file() or item.is_symlink()):
                relative_path = path.relative_to(root_path).as_posix()
                try:
                    resolved = resolve_managed_relative_path(root_path=root_path, relative_path=relative_path)
                except PathPolicyError:
                    errors += 1
                    continue
                stat = resolved.stat()
                fingerprint = _fingerprint(relative_path=relative_path, size_bytes=stat.st_size, modified_at=stat.st_mtime)
                existing = (
                    self.db.query(ManagedFile)
                    .filter(ManagedFile.root_id == root.id, ManagedFile.relative_path == relative_path)
                    .one_or_none()
                )
                if existing is None:
                    existing = ManagedFile(
                        root_id=root.id,
                        relative_path=relative_path,
                        filename=resolved.name,
                        extension=resolved.suffix.lower(),
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                        fingerprint=fingerprint,
                        status="ACTIVE",
                        last_seen_scan_run_id=scan_run.id,
                    )
                    self.db.add(existing)
                else:
                    existing.filename = resolved.name
                    existing.extension = resolved.suffix.lower()
                    existing.size_bytes = stat.st_size
                    existing.modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    existing.fingerprint = fingerprint
                    existing.status = "ACTIVE"
                    existing.last_seen_scan_run_id = scan_run.id
                    existing.updated_at = utcnow()
                seen_paths.add(relative_path)
                files_updated += 1

        missing_count = (
            self.db.query(ManagedFile)
            .filter(ManagedFile.root_id == root.id, ManagedFile.status == "ACTIVE")
            .filter(~ManagedFile.relative_path.in_(seen_paths) if seen_paths else ManagedFile.relative_path != "")
            .update({"status": "MISSING", "updated_at": utcnow()}, synchronize_session=False)
        )
        scan_run.status = "COMPLETED"
        scan_run.files_discovered = len(seen_paths)
        scan_run.files_updated = files_updated
        scan_run.files_missing = int(missing_count or 0)
        scan_run.errors = errors
        scan_run.finished_at = utcnow()
        self.db.flush()
        return scan_run


def _fingerprint(*, relative_path: str, size_bytes: int, modified_at: float) -> str:
    """生成 P0 轻量 fingerprint，后续可升级为内容 hash。"""

    return f"{relative_path}:{size_bytes}:{int(modified_at)}"
