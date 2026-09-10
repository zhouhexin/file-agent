"""D4/T22-T24 工作副本无覆盖发布和文件系统恢复测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.core import config
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.file_lifecycle.working_copy_executor import (
    WorkingCopyExecutionError,
    WorkingCopyExecutor,
)


def _executor(monkeypatch, tmp_path: Path) -> tuple[WorkingCopyExecutor, Path]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://test:test@localhost/test")
    monkeypatch.setenv("WORKING_COPY_STORAGE_ROOT", str(tmp_path / "working"))
    config.get_settings.cache_clear()
    storage = FileLifecycleStorageService(config.get_settings())
    root = storage.working_copy_path("shared")
    root.mkdir(parents=True)
    return WorkingCopyExecutor(storage), root


def _identity(executor: WorkingCopyExecutor, source: str):
    return executor.capture_source_identity(
        root_relative_path="shared",
        source_relative_path=source,
    )


def test_t22_existing_target_is_never_overwritten(monkeypatch, tmp_path):
    """已有目标或进程外抢占必须保留两边文件，不提前宣告移动成功。"""

    executor, root = _executor(monkeypatch, tmp_path)
    source = root / "待整理" / "报告.txt"
    target = root / "其他" / "报告.txt"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    target.write_bytes(b"target")

    with pytest.raises(WorkingCopyExecutionError) as error:
        executor.apply_move(
            operation_id="operation-1",
            working_copy_id="copy-1",
            root_relative_path="shared",
            source_relative_path="待整理/报告.txt",
            target_relative_path="其他/报告.txt",
            expected_identity=_identity(executor, "待整理/报告.txt"),
        )

    assert error.value.code == "PLACEMENT_RECONCILIATION_REQUIRED"
    assert source.read_bytes() == b"source"
    assert target.read_bytes() == b"target"


def test_t22_safe_move_preserves_original_bytes_and_writes_identity_journal(monkeypatch, tmp_path):
    """同文件系统发布以 link+unlink 完成，目标不存在时才允许移动。"""

    executor, root = _executor(monkeypatch, tmp_path)
    source = root / "待整理" / "材料.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"immutable-working-copy-bytes")
    identity = _identity(executor, "待整理/材料.txt")

    result = executor.apply_move(
        operation_id="operation-2",
        working_copy_id="copy-2",
        root_relative_path="shared",
        source_relative_path="待整理/材料.txt",
        target_relative_path="其他/材料.txt",
        expected_identity=identity,
    )

    target = root / "其他" / "材料.txt"
    assert result.performed_move is True
    assert source.exists() is False
    assert target.read_bytes() == b"immutable-working-copy-bytes"
    assert result.target_identity.sha256 == hashlib.sha256(target.read_bytes()).hexdigest()
    journal = root / ".file-agent-operations" / "operation-2.json"
    assert '"phase":"FS_APPLIED"' in journal.read_text(encoding="utf-8")


def test_t23_recovery_after_target_link_before_database_commit(monkeypatch, tmp_path):
    """源和目标同 inode 时可证明属于本操作，恢复器可完成同一次物理移动。"""

    executor, root = _executor(monkeypatch, tmp_path)
    source = root / "待整理" / "恢复.txt"
    target = root / "其他" / "恢复.txt"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"same-file-identity")
    identity = _identity(executor, "待整理/恢复.txt")
    # 模拟 link 已成功但 worker 在删除源文件与事务 B 之前崩溃。
    target.hardlink_to(source)

    result = executor.apply_move(
        operation_id="operation-3",
        working_copy_id="copy-3",
        root_relative_path="shared",
        source_relative_path="待整理/恢复.txt",
        target_relative_path="其他/恢复.txt",
        expected_identity=identity,
    )

    assert result.recovered_after_filesystem_apply is True
    assert source.exists() is False
    assert target.read_bytes() == b"same-file-identity"


def test_t23_initial_publish_recovers_identity_from_its_own_journal(monkeypatch, tmp_path):
    """首次发布在源暂存件已删除时，只能复用匹配操作日志中的完整身份。"""

    executor, root = _executor(monkeypatch, tmp_path)
    source = root / ".internal" / "staged.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"initial-publish-recovery")
    identity = _identity(executor, ".internal/staged.txt")
    executor.apply_move(
        operation_id="initial-operation-1",
        working_copy_id="copy-initial-1",
        root_relative_path="shared",
        source_relative_path=".internal/staged.txt",
        target_relative_path="其他/staged.txt",
        expected_identity=identity,
    )

    recovered = executor.capture_or_recover_source_identity(
        operation_id="initial-operation-1",
        root_relative_path="shared",
        source_relative_path=".internal/staged.txt",
        target_relative_path="其他/staged.txt",
    )

    assert recovered == identity


def test_t24_same_hash_without_stable_identity_is_not_auto_claimed(monkeypatch, tmp_path):
    """两处独立文件即使 hash 相同，也必须进入人工恢复而不能删除任一文件。"""

    executor, root = _executor(monkeypatch, tmp_path)
    source = root / "待整理" / "冲突.txt"
    target = root / "其他" / "冲突.txt"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    source.write_bytes(b"same-hash-different-identity")
    identity = _identity(executor, "待整理/冲突.txt")
    target.write_bytes(b"same-hash-different-identity")

    with pytest.raises(WorkingCopyExecutionError) as error:
        executor.apply_move(
            operation_id="operation-4",
            working_copy_id="copy-4",
            root_relative_path="shared",
            source_relative_path="待整理/冲突.txt",
            target_relative_path="其他/冲突.txt",
            expected_identity=identity,
        )

    assert error.value.code == "PLACEMENT_RECONCILIATION_REQUIRED"
    assert source.exists() and target.exists()
