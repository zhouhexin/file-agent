"""受管目录只读扫描器。

扫描器只读取文件元数据并写入 managed_files，不打开正文、不修改原始文件。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import FilesystemScanRun, ManagedFile, ManagedRoot, WorkingCopy, utcnow
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
        existing_by_identity = {
            file.file_identity: file
            for file in existing_by_path.values()
            if file.file_identity
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
                relative_path_hash = _path_hash(relative_path)
                fingerprint = _fingerprint(relative_path=relative_path, size_bytes=stat.st_size, modified_at=stat.st_mtime)
                file_identity = f"{stat.st_dev}:{stat.st_ino}"
                existing = existing_by_path.get(relative_path)
                if existing is None:
                    # 同一设备和 inode 在本轮出现在新路径时视为原始文件重命名/移动，
                    # 继续沿用 ManagedFile 稳定 ID，工作副本路径保持不变。
                    identity_match = existing_by_identity.get(file_identity)
                    if identity_match is not None and identity_match.relative_path not in seen_paths:
                        existing = identity_match
                # 全量内容哈希只在异步扫描 worker 中计算；元数据未变化时复用既有哈希，
                # 避免查询请求承担大文件 I/O，同时保证查重不用轻量 fingerprint 冒充内容事实。
                content_sha256 = (
                    existing.content_sha256
                    if existing is not None
                    and existing.fingerprint == fingerprint
                    and existing.content_sha256
                    else _sha256_file(resolved)
                )
                category_path = _category_path_for(root=root, relative_path=relative_path)
                if existing is None:
                    existing = ManagedFile(
                        root_id=root.id,
                        relative_path=relative_path,
                        relative_path_hash=relative_path_hash,
                        category_path=category_path,
                        filename=resolved.name,
                        extension=resolved.suffix.lower(),
                        size_bytes=stat.st_size,
                        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                        fingerprint=fingerprint,
                        content_sha256=content_sha256,
                        file_identity=file_identity,
                        source_type="DEPLOYED_FILE",
                        status="ACTIVE",
                        last_seen_scan_run_id=scan_run.id,
                    )
                    self.db.add(existing)
                else:
                    existing.filename = resolved.name
                    existing.relative_path_hash = relative_path_hash
                    existing.category_path = category_path
                    existing.extension = resolved.suffix.lower()
                    existing.size_bytes = stat.st_size
                    existing.modified_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                    existing.fingerprint = fingerprint
                    existing.content_sha256 = content_sha256
                    existing.file_identity = file_identity
                    existing.status = "ACTIVE"
                    existing.last_seen_scan_run_id = scan_run.id
                    existing.updated_at = utcnow()
                    self._sync_working_copy_status(managed_file=existing, source_sha256=content_sha256)
                seen_paths.add(relative_path)
                files_updated += 1

        missing_files = (
            self.db.query(ManagedFile)
            .filter(ManagedFile.root_id == root.id, ManagedFile.status == "ACTIVE")
            .filter(~ManagedFile.relative_path.in_(seen_paths) if seen_paths else ManagedFile.relative_path != "")
            .all()
        )
        for missing in missing_files:
            missing.status = "MISSING"
            missing.updated_at = utcnow()
            self.db.query(WorkingCopy).filter(WorkingCopy.managed_file_id == missing.id).update(
                {"sync_status": "ORIGINAL_MISSING", "updated_at": utcnow()},
                synchronize_session=False,
            )
        missing_count = len(missing_files)
        scan_run.status = "COMPLETED"
        scan_run.files_discovered = len(seen_paths)
        scan_run.files_updated = files_updated
        scan_run.files_missing = int(missing_count or 0)
        scan_run.errors = errors
        scan_run.finished_at = utcnow()
        root.last_reconciled_at = scan_run.finished_at
        self.db.flush()
        return scan_run

    def _sync_working_copy_status(self, *, managed_file: ManagedFile, source_sha256: str) -> None:
        """根据原始文件内容变化更新工作副本同步状态，但绝不覆盖工作副本。"""

        working_copies = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.managed_file_id == managed_file.id)
            .all()
        )
        for working_copy in working_copies:
            working_copy.sync_status = (
                "SYNCED"
                if working_copy.imported_source_sha256 == source_sha256
                else "ORIGINAL_CHANGED"
            )
            working_copy.updated_at = utcnow()


def _fingerprint(*, relative_path: str, size_bytes: int, modified_at: float) -> str:
    """生成 P0 轻量 fingerprint，后续可升级为内容 hash。"""

    payload = f"{relative_path}\0{size_bytes}\0{int(modified_at)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _path_hash(relative_path: str) -> str:
    """生成相对路径唯一性哈希，避免把长路径放进唯一索引。"""

    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    """流式计算原始文件完整 SHA-256，供同步状态和重复检查使用。"""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_hidden_relative_path(relative_path: str) -> bool:
    """判断受管目录相对路径中是否包含隐藏文件或隐藏目录。"""

    return any(part.startswith(".") for part in Path(relative_path).parts)


def _category_path_for(*, root: ManagedRoot, relative_path: str) -> str | None:
    """按受管目录模式从父目录推导分类路径。"""

    if root.classification_mode not in {"PATH_AS_CATEGORY", "PATH_AS_WEAK_LABEL"}:
        return None
    parent = Path(relative_path).parent.as_posix()
    if parent in {"", "."}:
        return None
    return parent
