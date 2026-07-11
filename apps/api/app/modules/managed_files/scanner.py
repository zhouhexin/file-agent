"""受管目录只读扫描器。

扫描器只读取文件元数据并写入 managed_files，不打开正文、不修改原始文件。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
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
        existing_by_path = {
            file.relative_path: file
            for file in self.db.query(ManagedFile).filter(ManagedFile.root_id == root.id).all()
        }
        seen_paths: set[str] = set()
        files_updated = 0
        errors = 0
        if root_path.exists():
            for path in sorted(item for item in root_path.rglob("*") if item.is_file() or item.is_symlink()):
                relative_path = path.relative_to(root_path).as_posix()
                if _is_hidden_relative_path(relative_path):
                    # 受管目录只展示业务文件，macOS .DS_Store、点号目录等隐藏项不进入索引。
                    continue
                try:
                    resolved = resolve_managed_relative_path(root_path=root_path, relative_path=relative_path)
                except PathPolicyError:
                    errors += 1
                    continue
                stat = resolved.stat()
                fingerprint = _fingerprint(relative_path=relative_path, size_bytes=stat.st_size, modified_at=stat.st_mtime)
                category_path = _category_path_for(root=root, relative_path=relative_path)
                existing = existing_by_path.get(relative_path)
                if existing is None:
                    existing = ManagedFile(
                        root_id=root.id,
                        relative_path=relative_path,
                        category_path=category_path,
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
                    existing.category_path = category_path
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

    payload = f"{relative_path}\0{size_bytes}\0{int(modified_at)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _is_hidden_relative_path(relative_path: str) -> bool:
    """判断受管目录相对路径中是否包含隐藏文件或隐藏目录。"""

    return any(part.startswith(".") for part in Path(relative_path).parts)


def _category_path_for(*, root: ManagedRoot, relative_path: str) -> str | None:
    """按受管目录模式从父目录推导分类路径。"""

    if root.classification_mode != "PATH_AS_CATEGORY":
        return None
    parent = Path(relative_path).parent.as_posix()
    if parent in {"", "."}:
        return None
    return parent
