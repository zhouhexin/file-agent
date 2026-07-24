"""共享工作目录与开发重置安全边界测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.modules.file_lifecycle.shared_workspace import (
    SHARED_WORKSPACE_STORAGE_KEY,
    SHARED_WORKSPACE_SYSTEM_KEY,
    get_or_create_shared_workspace,
)
from app.scripts.reset_development_shared_workspace import (
    ResetTarget,
    run_reset,
    validate_reset_targets,
)
from app.tests.helpers import client_with_database


def test_shared_workspace_is_singleton_and_has_stable_storage_contract():
    """同一数据库反复请求共享空间只能得到一个系统工作区，不能随用户数量增长。"""

    _client, SessionLocal = client_with_database()
    with SessionLocal() as db:
        first = get_or_create_shared_workspace(db)
        second = get_or_create_shared_workspace(db)
        db.commit()
        assert first.id == second.id
        assert first.system_key == SHARED_WORKSPACE_SYSTEM_KEY
        assert first.workspace_type == "SYSTEM_SHARED"
        assert SHARED_WORKSPACE_STORAGE_KEY == "shared"


def test_reset_rejects_archive_target_overlapping_external_managed_root(tmp_path: Path):
    """开发重置绝不能因归档配置错误删除学校外部受管原始资料目录。"""

    external_root = tmp_path / "school-files"
    archive_target = external_root / "uploads"
    with pytest.raises(ValueError, match="外部受管原始资料目录"):
        validate_reset_targets(
            [ResetTarget("上传归档原件", archive_target)],
            project_root=tmp_path / "project",
            protected_roots=[external_root],
        )


def test_reset_rejects_empty_archive_configuration_before_cleanup(tmp_path: Path):
    """没有明确上传归档路径时重置必须失败，不能把当前目录误当作删除目标。"""

    settings = Settings(database_url="sqlite+pysqlite:///:memory:", managed_root_archive_write_path="")
    # 在访问文件或数据库之前就拒绝缺失配置，不能把当前目录误当删除目标。
    with pytest.raises(ValueError, match="MANAGED_ROOT_ARCHIVE_WRITE_PATH"):
        run_reset(settings=settings, project_root=tmp_path, db=None, database_engine=None)  # type: ignore[arg-type]
