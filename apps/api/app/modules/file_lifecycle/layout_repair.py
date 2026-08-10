"""共享工作副本物理布局修复服务。

本模块只由持久化 RECONCILE worker 调用，用于把升级前的工作副本根以及系统自动
生成的“待整理/待确认”路径迁移到 ``shared/<root_key>/<源相对路径>``。用户已经
确认执行过的改名或移动必须保留，修复过程不得触碰不可变受管原始目录。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import (
    Document,
    DocumentVersion,
    FileObject,
    ManagedFile,
    ManagedRoot,
    WorkingCopy,
    WorkingCopyPathRecord,
    WorkingCopyRoot,
    utcnow,
)
from app.modules.file_lifecycle.shared_workspace import (
    SHARED_WORKSPACE_STORAGE_KEY,
    get_shared_workspace_id,
)
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


LEGACY_SYSTEM_DIRECTORIES = frozenset({"待整理", "待确认"})


class WorkingCopyLayoutRepairService:
    """修复共享根前缀和历史系统暂存路径，并保存逐文件路径记录。"""

    def __init__(self, db: Session) -> None:
        """保存 worker 级数据库会话和受控 StorageService。"""

        self.db = db
        self.storage = FileLifecycleStorageService()

    def repair_managed_root(self, *, managed_root_id: str) -> dict[str, Any]:
        """修复一个受管根对应的共享工作副本布局。

        历史用户主动改名或移动通过 ``working_copy_path_records`` 判定并保留；只有
        最新记录仍为首次导入的系统路径才从“待整理/待确认”恢复源相对路径。
        """

        managed_root = self.db.get(ManagedRoot, managed_root_id)
        if managed_root is None:
            raise RuntimeError("布局修复缺少受管原始目录")
        working_root = (
            self.db.query(WorkingCopyRoot)
            .filter(
                WorkingCopyRoot.workspace_id == get_shared_workspace_id(self.db),
                WorkingCopyRoot.managed_root_id == managed_root.id,
            )
            .one_or_none()
        )
        expected_prefix = f"{SHARED_WORKSPACE_STORAGE_KEY}/{managed_root.root_key}"
        if working_root is None:
            return {
                "status": "SKIPPED",
                "reason": "WORKING_ROOT_NOT_CREATED",
                "expected_prefix": expected_prefix,
                "repaired_files": 0,
            }

        old_prefix = working_root.relative_storage_path.strip("/\\")
        copies = (
            self.db.query(WorkingCopy)
            .filter(WorkingCopy.working_copy_root_id == working_root.id)
            .order_by(WorkingCopy.created_at.asc())
            .all()
        )
        repaired_files = 0
        legacy_paths = 0
        collision_paths = 0
        for working_copy in copies:
            managed_file = self.db.get(ManagedFile, working_copy.managed_file_id)
            if managed_file is None:
                log_event(
                    "working_copy.layout_repair.skipped",
                    level="WARNING",
                    status="DEGRADED",
                    error_code="MANAGED_FILE_MISSING",
                    working_copy_id=working_copy.id,
                    root_id=managed_root.id,
                    message="工作副本缺少原件索引，布局修复已跳过",
                )
                continue
            current_relative = working_copy.relative_path.replace("\\", "/").strip("/")
            desired_relative = current_relative
            restoring_source_path = self._is_unmodified_legacy_import(
                working_copy=working_copy,
                relative_path=current_relative,
            )
            if restoring_source_path:
                desired_relative = managed_file.relative_path.replace("\\", "/").strip("/")
                legacy_paths += 1

            before_storage = _join(old_prefix, current_relative)
            after_storage = _join(expected_prefix, desired_relative)
            resolved_storage, used_collision_path = self._relocate_file(
                before_storage=before_storage,
                after_storage=after_storage,
                working_copy=working_copy,
            )
            if used_collision_path:
                collision_paths += 1
            resolved_relative = _relative_under_prefix(
                storage_path=resolved_storage,
                prefix=expected_prefix,
            )
            if before_storage == resolved_storage and old_prefix == expected_prefix:
                continue
            self._update_persistent_paths(
                working_copy=working_copy,
                managed_file=managed_file,
                before_storage=before_storage,
                after_storage=resolved_storage,
                relative_path=resolved_relative,
                restoring_source_path=restoring_source_path,
            )
            repaired_files += 1

        working_root.relative_storage_path = expected_prefix
        working_root.status = "READY"
        working_root.last_reconciled_at = utcnow()
        log_event(
            "working_copy.layout_repair.completed",
            status="COMPLETED",
            root_id=managed_root.id,
            repaired_files=repaired_files,
            legacy_paths=legacy_paths,
            collision_paths=collision_paths,
            message="共享工作副本布局修复完成",
        )
        return {
            "status": "COMPLETED",
            "old_prefix": old_prefix,
            "expected_prefix": expected_prefix,
            "working_copy_count": len(copies),
            "repaired_files": repaired_files,
            "legacy_paths": legacy_paths,
            "collision_paths": collision_paths,
        }

    def _is_unmodified_legacy_import(
        self,
        *,
        working_copy: WorkingCopy,
        relative_path: str,
    ) -> bool:
        """只修复系统首次导入路径，不能撤销用户确认后的移动或改名。"""

        first_segment = PurePosixPath(relative_path).parts[0] if relative_path else ""
        if first_segment not in LEGACY_SYSTEM_DIRECTORIES:
            return False
        latest = (
            self.db.query(WorkingCopyPathRecord)
            .filter(WorkingCopyPathRecord.working_copy_id == working_copy.id)
            .order_by(WorkingCopyPathRecord.sequence_number.desc())
            .first()
        )
        return latest is None or latest.operation_type == "INITIAL_IMPORT"

    def _relocate_file(
        self,
        *,
        before_storage: str,
        after_storage: str,
        working_copy: WorkingCopy,
    ) -> tuple[str, bool]:
        """原子移动工作副本；不同内容占位时使用隐藏稳定隔离路径。"""

        source = self.storage.working_copy_path(before_storage)
        target = self.storage.working_copy_path(after_storage)
        if source == target:
            return after_storage, False
        if target.exists():
            if target.is_file() and self.storage.sha256_file(target) == working_copy.content_sha256:
                if source.is_file():
                    source.unlink()
                return after_storage, False
            collision_storage = _join(
                str(PurePosixPath(after_storage).parent),
                ".internal-layout-collisions",
                working_copy.id,
                working_copy.filename,
            )
            target = self.storage.working_copy_path(collision_storage)
            after_storage = collision_storage
            if target.exists() and (
                not target.is_file()
                or self.storage.sha256_file(target) != working_copy.content_sha256
            ):
                raise RuntimeError("布局修复隔离目标仍被不同内容占用")
            used_collision_path = True
        else:
            used_collision_path = False
        if not source.is_file():
            if target.is_file() and self.storage.sha256_file(target) == working_copy.content_sha256:
                return after_storage, used_collision_path
            raise FileNotFoundError("历史工作副本物理文件不存在")
        if self.storage.sha256_file(source) != working_copy.content_sha256:
            raise RuntimeError("历史工作副本内容哈希与数据库不一致")
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, target)
        return after_storage, used_collision_path

    def _update_persistent_paths(
        self,
        *,
        working_copy: WorkingCopy,
        managed_file: ManagedFile,
        before_storage: str,
        after_storage: str,
        relative_path: str,
        restoring_source_path: bool,
    ) -> None:
        """同步当前版本、文件对象和路径审计，不能只移动物理文件。"""

        if not working_copy.current_version_id:
            # 路径审计必须引用确定的工作副本版本；异常旧数据不能写入半完整记录。
            raise RuntimeError("历史工作副本缺少当前版本，无法安全修复路径")
        filename = Path(relative_path).name
        working_copy.relative_path = relative_path
        working_copy.relative_path_hash = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()
        working_copy.filename = filename
        working_copy.extension = Path(filename).suffix.lower()
        working_copy.updated_at = utcnow()
        version = self.db.get(DocumentVersion, working_copy.current_version_id)
        if version is not None:
            version.storage_path = after_storage
            version.filename = filename
        self.db.query(FileObject).filter(
            FileObject.document_id == working_copy.document_id,
            FileObject.storage_backend == "working_copy_local",
        ).update({"storage_path": after_storage}, synchronize_session=False)
        document = self.db.get(Document, working_copy.document_id)
        if document is not None and restoring_source_path:
            document.original_filename = managed_file.filename
        sequence = (
            self.db.query(func.max(WorkingCopyPathRecord.sequence_number))
            .filter(WorkingCopyPathRecord.working_copy_id == working_copy.id)
            .scalar()
            or 0
        ) + 1
        self.db.add(
            WorkingCopyPathRecord(
                working_copy_id=working_copy.id,
                sequence_number=sequence,
                operation_type="SYSTEM_LAYOUT_REPAIR",
                before_relative_path=before_storage,
                after_relative_path=after_storage,
                before_filename=Path(before_storage).name,
                after_filename=filename,
                document_version_id=working_copy.current_version_id,
                content_sha256=working_copy.content_sha256,
                status="COMPLETED",
                executed_by=None,
            )
        )


def _join(*parts: str) -> str:
    """拼接受控 POSIX 相对路径，不接受空路径片段。"""

    cleaned = [part.replace("\\", "/").strip("/") for part in parts if part]
    return PurePosixPath(*cleaned).as_posix()


def _relative_under_prefix(*, storage_path: str, prefix: str) -> str:
    """从已校验存储路径中移除共享根前缀。"""

    storage = PurePosixPath(storage_path)
    root = PurePosixPath(prefix)
    try:
        return storage.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError("布局修复结果不在共享工作副本根内") from exc
