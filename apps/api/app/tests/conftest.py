"""测试全局配置。"""

from __future__ import annotations

import pytest

from app.core import config


@pytest.fixture(autouse=True)
def disable_real_llm_by_default(monkeypatch):
    """单元测试默认禁止访问真实 LLM，避免测试结果依赖外部模型。"""

    monkeypatch.setenv("LLM_ENABLED", "false")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
