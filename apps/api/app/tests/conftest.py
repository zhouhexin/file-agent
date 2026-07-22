"""测试全局配置。"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest

from app.core import config


def pytest_configure(config):
    """在 Windows 为 pytest 使用短临时根，避免测试函数名放大 MAX_PATH。

    业务代码的长路径安全仍由独立回归测试覆盖；这里仅移除 pytest 自动生成的
    ``pytest-of-user/pytest-N/test_function_name`` 非业务层级。
    """

    if os.name != "nt" or config.getoption("basetemp"):
        return
    temp_root = Path(tempfile.gettempdir()) / f"fa-pytest-{os.getpid()}"
    config.option.basetemp = str(temp_root)


@pytest.fixture(autouse=True)
def disable_real_integrations_by_default(monkeypatch, request):
    """隔离外部能力、项目 .env 和受管目录，保证测试只使用显式依赖。"""

    # 受管目录服务会枚举进程内全部 MANAGED_ROOT_*；必须先移除 IDE 和 Shell 配置，
    # 否则“唯一受管根”测试会因开发者本机目录数量不同而产生不确定结果。
    for env_name in tuple(os.environ):
        if env_name.upper().startswith(("MANAGED_ROOT_", "MANAGED_PATH_")):
            monkeypatch.delenv(env_name, raising=False)
    # 除配置模块自身的测试外，禁止 get_settings 在测试中重新读取项目 .env，
    # 避免 Windows 开发机上的真实 Downloads 或 Neo4j 配置污染隔离用例。
    if Path(str(request.node.path)).name != "test_config.py":
        monkeypatch.setattr(config, "find_dotenv_file", lambda: None)
    for env_name in (
        "LLM_ENABLED",
        "FILE_RENAME_LLM_VALIDATION_ENABLED",
        "EMBEDDING_ENABLED",
        "GRAPH_CLASSIFICATION_ENABLED",
        "GRAPH_EMBEDDING_ENABLED",
        "GRAPH_PROJECTION_WORKER_ENABLED",
        "NEO4J_SYNC_ENABLED",
        "MCP_FILESYSTEM_ENABLED",
        "OCR_ENABLED",
        "OCR_LLM_ENABLED",
        "DOCLING_ENABLED",
        "MANAGED_ROOT_RECONCILE_ON_STARTUP",
    ):
        monkeypatch.setenv(env_name, "false")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
