"""两阶段检索 SQL 超时作用域测试。

这些用例保护请求级数据库 Session：检索可以使用较短的查询上限，但该设置不能泄漏到
完整性评估、AgentRun 审计或同一请求的后续 Tool 调用。
"""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.modules.retrieval.two_stage_search import TwoStageFileSearchService


def _postgresql_session(*, previous_timeout: str = "7s") -> MagicMock:
    """构造只记录配置 SQL 的 PostgreSQL Session fake，不连接真实数据库。"""

    db = MagicMock()
    db.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    db.begin_nested.side_effect = lambda: nullcontext()
    show_result = MagicMock()
    show_result.scalar_one.return_value = previous_timeout
    db.execute.side_effect = [show_result, MagicMock(), MagicMock()]
    return db


def test_search_restores_previous_statement_timeout_after_success():
    """正常返回后必须恢复进入检索前的超时值，而不是固定重置为零。"""

    db = _postgresql_session(previous_timeout="7s")
    service = TwoStageFileSearchService(
        db=db,
        user_id="user-1",
        workspace_id="workspace-1",
    )
    service._search = MagicMock(return_value={"ok": True, "results": []})

    result = service.search(
        query="工作总结",
        parsed_query=SimpleNamespace(cleaned="工作总结"),
    )

    assert result == {"ok": True, "results": []}
    calls = db.execute.call_args_list
    assert str(calls[0].args[0]) == "SHOW statement_timeout"
    assert calls[1].args[1] == {"timeout": "2000ms"}
    assert calls[2].args[1] == {"timeout": "7s"}


def test_search_restores_previous_statement_timeout_after_failure():
    """检索异常时 finally 仍需恢复超时，避免错误影响后续失败审计 SQL。"""

    db = _postgresql_session(previous_timeout="0")
    service = TwoStageFileSearchService(
        db=db,
        user_id="user-1",
        workspace_id="workspace-1",
    )
    service._search = MagicMock(side_effect=RuntimeError("search failed"))

    with pytest.raises(RuntimeError, match="search failed"):
        service.search(
            query="工作总结",
            parsed_query=SimpleNamespace(cleaned="工作总结"),
        )

    assert db.execute.call_args_list[2].args[1] == {"timeout": "0"}
