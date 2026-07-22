"""测试全局配置。"""

from __future__ import annotations

import os

import pytest

from app.core import config


@pytest.fixture(autouse=True)
def disable_real_llm_by_default(monkeypatch):
    """隔离外部智能能力和受管目录配置，保证单元测试只使用自身声明的依赖。"""

    # 受管目录服务会枚举进程内全部 MANAGED_ROOT_*；必须先移除 IDE 和 Shell 配置，
    # 否则“唯一受管根”测试会因开发者本机目录数量不同而产生不确定结果。
    for env_name in tuple(os.environ):
        if env_name.startswith(("MANAGED_ROOT_", "MANAGED_PATH_")):
            monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("FILE_RENAME_LLM_VALIDATION_ENABLED", "false")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
