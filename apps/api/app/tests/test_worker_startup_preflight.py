"""Worker 启动预检的跨平台回归测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.db.models import FilesystemJob, ManagedRoot
from app.modules.file_lifecycle.startup_preflight import (
    WorkerStartupPreflightError,
    prepare_worker_startup,
)
from app.tests.helpers import clear_overrides, client_with_database


def test_preflight_updates_stale_cross_platform_root_before_worker_starts(
    monkeypatch,
    tmp_path: Path,
):
    """共享开发库残留其他系统路径时，当前机器 `.env` 必须在扫描前覆盖它。"""

    current_root = tmp_path / "windows-managed-root"
    current_root.mkdir()
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(current_root))
    # 扫描批次预算不是目录定义，不能把数值 100、5 当成文件系统路径。
    monkeypatch.setenv("MANAGED_ROOT_SCAN_BATCH_SIZE", "100")
    monkeypatch.setenv("MANAGED_ROOT_SCAN_BATCH_MAX_SECONDS", "5")
    get_settings.cache_clear()
    _client, session_factory = client_with_database()
    try:
        with session_factory() as db:
            stale_root = ManagedRoot(
                root_key="school_files",
                display_name="school_files",
                container_path="/Users/old-machine/managed-files",
            )
            pseudo_root = ManagedRoot(
                root_key="scan_batch_size",
                display_name="scan_batch_size",
                container_path="100",
            )
            db.add_all([stale_root, pseudo_root])
            db.flush()
            pseudo_job = FilesystemJob(
                job_type="SCAN_MANAGED_ROOT",
                queue_name="SCAN",
                root_id=pseudo_root.id,
                status="PENDING",
                payload_json={},
                result_json={},
            )
            db.add(pseudo_job)
            db.commit()
            pseudo_root_id = pseudo_root.id
            pseudo_job_id = pseudo_job.id

        result = prepare_worker_startup(session_factory=session_factory)

        assert result.managed_root_keys == ("school_files",)
        with session_factory() as db:
            root = db.query(ManagedRoot).filter(ManagedRoot.root_key == "school_files").one()
            assert root.container_path == str(current_root)
            disabled_pseudo_root = db.get(ManagedRoot, pseudo_root_id)
            cancelled_pseudo_job = db.get(FilesystemJob, pseudo_job_id)
            assert disabled_pseudo_root is not None
            assert disabled_pseudo_root.enabled is False
            assert cancelled_pseudo_job is not None
            assert cancelled_pseudo_job.status == "FAILED"
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_preflight_rejects_missing_directory_without_committing_new_path(
    monkeypatch,
    tmp_path: Path,
):
    """目录不存在时必须停止整个启动流程，不能让 worker 继续领取扫描任务。"""

    missing_root = tmp_path / "missing-root"
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(missing_root))
    get_settings.cache_clear()
    _client, session_factory = client_with_database()
    try:
        with pytest.raises(WorkerStartupPreflightError) as captured:
            prepare_worker_startup(session_factory=session_factory)

        assert captured.value.code == "MANAGED_ROOT_NOT_FOUND"
        assert captured.value.root_key == "school_files"
        with session_factory() as db:
            assert db.query(ManagedRoot).count() == 0
    finally:
        get_settings.cache_clear()
        clear_overrides()


def test_preflight_distinguishes_directory_enumeration_permission_error(
    monkeypatch,
    tmp_path: Path,
):
    """Windows 目录句柄无法打开时必须报告真实权限错误，而不是路径不存在。"""

    managed_root = tmp_path / "protected-root"
    managed_root.mkdir()
    monkeypatch.setenv("MANAGED_ROOT_SCHOOL_FILES", str(managed_root))
    get_settings.cache_clear()
    _client, session_factory = client_with_database()
    real_scandir = os.scandir

    def denied_scandir(path):
        """只拒绝目标受管根，避免干扰测试框架清理临时目录。"""

        if Path(path) == managed_root:
            raise PermissionError("denied for test")
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", denied_scandir)
    try:
        with pytest.raises(WorkerStartupPreflightError) as captured:
            prepare_worker_startup(session_factory=session_factory)

        assert captured.value.code == "MANAGED_ROOT_PERMISSION_DENIED"
        assert captured.value.root_key == "school_files"
    finally:
        get_settings.cache_clear()
        clear_overrides()
