"""受管文件不可变用户快照服务。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document, FileObject, ManagedFile, ManagedFileSnapshot, ManagedRoot, User
from app.modules.managed_files.path_policy import resolve_managed_relative_path
from app.modules.managed_files.snapshot_repository import ManagedFileSnapshotRepository


@dataclass(frozen=True)
class ManagedSnapshotResolution:
    """一次受管文件快照创建或复用的结果。"""

    document: Document
    snapshot: ManagedFileSnapshot
    snapshot_status: str
    source_sha256: str


class ManagedFileSnapshotService:
    """负责受管文件内容哈希、只读复制和快照复用。"""

    def __init__(self, db: Session, user_id: str) -> None:
        """保存数据库会话和当前用户。"""

        self.db = db
        self.user_id = user_id
        self.repository = ManagedFileSnapshotRepository(db)
        self.storage_root = Path(get_settings().file_storage_root).resolve()

    def resolve(self, *, managed_file: ManagedFile, root: ManagedRoot) -> ManagedSnapshotResolution:
        """按内容哈希复用已有快照，或创建一个新的不可变快照。"""

        source_path = resolve_managed_relative_path(
            root_path=Path(root.container_path).resolve(),
            relative_path=managed_file.relative_path,
        )
        if not source_path.is_file():
            raise FileNotFoundError("受管文件已不存在。")

        source_stat_before = source_path.stat()
        source_sha256 = _sha256_file(source_path)
        source_stat_after = source_path.stat()
        if (
            source_stat_before.st_size != source_stat_after.st_size
            or source_stat_before.st_mtime_ns != source_stat_after.st_mtime_ns
        ):
            # 扫描或用户程序仍在写文件时不能生成“看似成功”的半成品快照。
            raise RuntimeError("受管文件在读取期间发生变化，请等待文件写入完成后重试。")
        existing = self.repository.get_by_source_version(
            user_id=self.user_id,
            managed_file_id=managed_file.id,
            source_sha256=source_sha256,
        )
        if existing is not None:
            document = self.db.get(Document, existing.document_id)
            if document is None:
                raise RuntimeError("快照关联的 Document 不存在。")
            self._ensure_snapshot_file(document=document, source_path=source_path, source_sha256=source_sha256)
            return ManagedSnapshotResolution(
                document=document,
                snapshot=existing,
                snapshot_status="REUSED",
                source_sha256=source_sha256,
            )

        document = self._create_document(
            filename=managed_file.filename,
            size_bytes=source_stat_after.st_size,
            source_sha256=source_sha256,
        )
        target_path: Path | None = None
        try:
            target_path = self._copy_snapshot(
                document=document,
                source_path=source_path,
                source_sha256=source_sha256,
            )
            snapshot = self.repository.create(
                user_id=self.user_id,
                managed_file_id=managed_file.id,
                document_id=document.id,
                source_fingerprint=managed_file.fingerprint,
                source_sha256=source_sha256,
                source_size_bytes=source_stat_after.st_size,
                source_modified_at=managed_file.modified_at,
            )
        except Exception:
            if target_path is not None:
                target_path.unlink(missing_ok=True)
                _remove_empty_parents(target_path.parent, stop_at=self.storage_root)
            raise
        return ManagedSnapshotResolution(
            document=document,
            snapshot=snapshot,
            snapshot_status="CREATED",
            source_sha256=source_sha256,
        )

    def _create_document(self, *, filename: str, size_bytes: int, source_sha256: str) -> Document:
        """创建当前用户拥有的快照 Document。"""

        user = self.db.get(User, self.user_id)
        document = Document(
            user_id=self.user_id,
            workspace_id=user.default_workspace_id if user is not None else None,
            original_filename=Path(filename).name,
            content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            size_bytes=size_bytes,
            sha256=source_sha256,
            status="USED_IN_MESSAGE",
            ingest_status="UPLOADED",
        )
        self.db.add(document)
        self.db.flush()
        return document

    def _copy_snapshot(self, *, document: Document, source_path: Path, source_sha256: str) -> Path:
        """原子复制并校验受管文件，然后创建本地 FileObject。"""

        storage_path = Path("managed-snapshots") / self.user_id / document.id / document.original_filename
        target_path = (self.storage_root / storage_path).resolve()
        _require_under_root(target_path, self.storage_root)
        try:
            _atomic_verified_copy(
                source_path=source_path,
                target_path=target_path,
                expected_sha256=source_sha256,
                expected_size=document.size_bytes,
            )
            self.db.add(
                FileObject(
                    document_id=document.id,
                    storage_backend="local",
                    storage_path=storage_path.as_posix(),
                    size_bytes=document.size_bytes,
                    sha256=source_sha256,
                )
            )
            self.db.flush()
        except Exception:
            target_path.unlink(missing_ok=True)
            _remove_empty_parents(target_path.parent, stop_at=self.storage_root)
            raise
        return target_path

    def cleanup_created_snapshot(self, *, document: Document) -> None:
        """清理当前事务回滚后可能残留的快照文件。"""

        target_path = (
            self.storage_root
            / "managed-snapshots"
            / self.user_id
            / document.id
            / document.original_filename
        ).resolve()
        _require_under_root(target_path, self.storage_root)
        target_path.unlink(missing_ok=True)
        _remove_empty_parents(target_path.parent, stop_at=self.storage_root)

    def _ensure_snapshot_file(self, *, document: Document, source_path: Path, source_sha256: str) -> None:
        """快照文件意外丢失时按原受管文件恢复，不重复创建 FileObject。"""

        file_object = (
            self.db.query(FileObject)
            .filter(FileObject.document_id == document.id, FileObject.storage_backend == "local")
            .order_by(FileObject.created_at.asc())
            .first()
        )
        if file_object is None:
            self._copy_snapshot(document=document, source_path=source_path, source_sha256=source_sha256)
            return
        target_path = (self.storage_root / file_object.storage_path).resolve()
        _require_under_root(target_path, self.storage_root)
        if _snapshot_matches(
            path=target_path,
            expected_sha256=source_sha256,
            expected_size=document.size_bytes,
        ):
            return
        # 兼容旧版本可能留下的截断快照：从仍受控的原文件原子修复，不新建业务版本。
        _atomic_verified_copy(
            source_path=source_path,
            target_path=target_path,
            expected_sha256=source_sha256,
            expected_size=document.size_bytes,
        )
        file_object.size_bytes = document.size_bytes
        file_object.sha256 = source_sha256


def _sha256_file(path: Path) -> str:
    """流式计算文件内容哈希，作为可靠版本标识。"""

    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_matches(*, path: Path, expected_sha256: str, expected_size: int) -> bool:
    """校验既有快照大小和内容哈希，截断文件不得继续被摘要链路复用。"""

    if not path.is_file() or path.stat().st_size != expected_size:
        return False
    return _sha256_file(path) == expected_sha256


def _atomic_verified_copy(
    *,
    source_path: Path,
    target_path: Path,
    expected_sha256: str,
    expected_size: int,
) -> None:
    """用同目录临时文件提交快照，并在提交前验证完整性。

    临时文件使用固定短前缀，避免 Windows 长路径下把原文件名再次拼入临时名称。
    """

    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".fa-snapshot-",
        suffix=".part",
        dir=target_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copyfile(source_path, temporary_path)
        if not _snapshot_matches(
            path=temporary_path,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
        ):
            raise RuntimeError("受管文件在快照复制期间发生变化，未保存不完整副本。")
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _require_under_root(path: Path, root: Path) -> None:
    """拒绝快照目标路径越过本地存储根目录。"""

    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("受管文件快照路径越界。") from exc


def _remove_empty_parents(start_dir: Path, *, stop_at: Path) -> None:
    """清理失败复制产生的空目录，不越过存储根目录。"""

    current = start_dir.resolve()
    stop = stop_at.resolve()
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent
