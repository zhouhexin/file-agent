"""三层文件生命周期的本地 StorageService。

所有路径都由后端根据稳定业务 ID 生成；调用方不能传入任意绝对路径。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from app.core.config import Settings, get_settings
from app.db.models import FileObject


class FileLifecycleStorageService:
    """在上传暂存、原始目录、工作副本和回收站之间执行受控文件操作。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """注入运行配置，测试可以提供隔离目录。"""

        self.settings = settings or get_settings()

    def upload_path(self, storage_path: str) -> Path:
        """解析上传暂存相对路径，拒绝路径穿越。"""

        return self._resolve_under(Path(self.settings.file_storage_root), storage_path)

    def archive_path(self, relative_path: str) -> Path:
        """解析归档 worker 独占的受管原始目录写入路径。"""

        if not self.settings.managed_root_archive_write_path:
            raise RuntimeError("MANAGED_ROOT_ARCHIVE_WRITE_PATH 未配置")
        return self._resolve_under(Path(self.settings.managed_root_archive_write_path), relative_path)

    def working_copy_path(self, relative_path: str) -> Path:
        """解析工作副本相对路径。"""

        return self._resolve_under(Path(self.settings.working_copy_storage_root), relative_path)

    def trash_path(self, relative_path: str) -> Path:
        """解析回收站相对路径。"""

        return self._resolve_under(Path(self.settings.trash_storage_root), relative_path)

    def file_object_path(self, file_object: FileObject) -> Path:
        """按受控 storage_backend 解析 FileObject，未知后端一律拒绝。"""

        if file_object.storage_backend == "local":
            return self.upload_path(file_object.storage_path)
        if file_object.storage_backend == "working_copy_local":
            return self.working_copy_path(file_object.storage_path)
        if file_object.storage_backend == "trash_local":
            return self.trash_path(file_object.storage_path)
        raise ValueError("不支持的文件存储后端")

    def archive_upload(self, *, source_storage_path: str, archive_relative_path: str, expected_sha256: str) -> Path:
        """把上传暂存原子复制为不可变原始文件并校验哈希。

        已存在目标只允许在内容哈希一致时幂等复用，任何冲突都禁止覆盖。
        """

        source = self.upload_path(source_storage_path)
        target = self.archive_path(archive_relative_path)
        return self._atomic_copy(source=source, target=target, expected_sha256=expected_sha256)

    def import_working_copy(self, *, source: Path, relative_path: str, expected_sha256: str) -> Path:
        """把原始文件原子复制到工作副本目录，禁止覆盖其他工作副本。"""

        target = self.working_copy_path(relative_path)
        return self._atomic_copy(source=source, target=target, expected_sha256=expected_sha256)

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """清理用户文件名中的路径和控制字符，同时保留中文与阿拉伯数字。"""

        basename = Path(filename).name.strip() or "uploaded-file"
        sanitized = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", basename).strip(" .")
        return sanitized[:240] or "uploaded-file"

    @staticmethod
    def sha256_file(path: Path) -> str:
        """流式计算完整 SHA-256，不能用元数据 fingerprint 代替内容校验。"""

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _atomic_copy(self, *, source: Path, target: Path, expected_sha256: str) -> Path:
        """使用同目录临时文件和原子提交复制内容。"""

        if not source.is_file():
            raise FileNotFoundError("源文件不存在")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if self.sha256_file(target) == expected_sha256:
                return target
            raise FileExistsError("目标文件已存在且内容不同，禁止覆盖")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.part")
        try:
            with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            if self.sha256_file(temporary) != expected_sha256:
                raise ValueError("复制后的文件哈希校验失败")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def _resolve_under(root: Path, relative_path: str) -> Path:
        """把相对路径限制在给定根目录内。"""

        if Path(relative_path).is_absolute():
            raise ValueError("文件路径必须是相对路径")
        resolved_root = root.resolve()
        candidate = (resolved_root / relative_path).resolve()
        if candidate == resolved_root or resolved_root not in candidate.parents:
            raise ValueError("文件路径越过受控存储根目录")
        return candidate
