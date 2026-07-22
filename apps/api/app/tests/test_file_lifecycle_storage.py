"""文件生命周期 StorageService 的跨平台路径安全回归测试。"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.core import config
from app.modules.file_lifecycle import storage as storage_module
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


def _storage(monkeypatch, tmp_path: Path) -> FileLifecycleStorageService:
    """创建使用隔离目录的生命周期存储服务。"""

    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "uploads"))
    monkeypatch.setenv("MANAGED_ROOT_ARCHIVE_WRITE_PATH", str(tmp_path / "originals"))
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    monkeypatch.setenv("TRASH_STORAGE_ROOT", str(tmp_path / "trash"))
    config.get_settings.cache_clear()
    return FileLifecycleStorageService(config.get_settings())


def test_internal_staging_path_avoids_windows_max_path_regression(monkeypatch, tmp_path):
    """内部暂存路径不得重复完整 UUID 和文件名而再次突破 Windows MAX_PATH。"""

    service = _storage(monkeypatch, tmp_path)
    workspace_id = "dd925a6f-f4bf-44e0-a559-af7da74d5045"
    job_id = "e36657f6-bba5-40b5-aada-0e0fa9390301"
    managed_file_id = "d8de54be-0be6-4fc0-8560-9bdc450d58bb"
    filename = "2024年度通知.txt"
    relative_path = service.internal_staging_relative_path(
        working_root_relative_path=f"{workspace_id}/upload_archive",
        job_id=job_id,
        managed_file_id=managed_file_id,
        filename=filename,
    )

    # 使用故障报告中的 Windows pytest 根路径重建回归边界：旧临时名超过 260，
    # 新暂存路径即使再追加短原子临时名也保留足够余量。
    windows_root = PureWindowsPath(
        r"C:\Users\zhouhexin\AppData\Local\Temp\pytest-of-zhouhexin\pytest-4\test_upload_is_archived_then_i0\working"
    )
    old_target = windows_root.joinpath(
        workspace_id,
        "upload_archive",
        ".internal",
        job_id,
        managed_file_id,
        filename,
    )
    old_temporary = old_target.with_name(f".{old_target.name}.27288.part")
    new_target = windows_root.joinpath(*PurePosixPath(relative_path).parts)
    representative_temporary = new_target.parent / ".fa-12345678.part"

    assert len(str(old_temporary)) > 260
    assert len(str(representative_temporary)) < 240
    assert relative_path.endswith(".txt")
    assert job_id not in relative_path
    assert managed_file_id not in relative_path
    assert filename not in relative_path


def test_atomic_copy_uses_short_exclusive_temporary_name(monkeypatch, tmp_path):
    """原子复制临时名必须短且排他创建，不能再次拼接长目标文件名。"""

    service = _storage(monkeypatch, tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"windows-safe-copy")
    expected_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
    # 使用足以识别“临时名复制目标名称”的长度，同时让 Windows pytest 临时根仍有执行余量。
    target_filename = f"{'a' * 64}.txt"
    target_relative_path = f"workspace/upload_archive/{target_filename}"
    observed: dict[str, str] = {}
    real_mkstemp = tempfile.mkstemp

    def capture_mkstemp(*, prefix: str, suffix: str, dir: Path):
        """记录真实排他临时文件名，同时保留 mkstemp 的原始行为。"""

        descriptor, name = real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)
        observed["name"] = Path(name).name
        return descriptor, name

    monkeypatch.setattr(storage_module.tempfile, "mkstemp", capture_mkstemp)
    copied = service.import_working_copy(
        source=source,
        relative_path=target_relative_path,
        expected_sha256=expected_sha256,
    )

    assert copied.read_bytes() == b"windows-safe-copy"
    assert observed["name"].startswith(".fa-")
    assert observed["name"].endswith(".part")
    assert target_filename not in observed["name"]
    assert len(observed["name"]) <= 24
    assert list(copied.parent.glob("*.part")) == []
