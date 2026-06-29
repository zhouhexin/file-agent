"""轻量结构化文件日志。

本模块不接外部日志平台，只负责把关键运行事件写入服务器本地 JSONL 文件。
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from app.core.config import get_settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
agent_run_id_var: ContextVar[str | None] = ContextVar("agent_run_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
conversation_id_var: ContextVar[str | None] = ContextVar("conversation_id", default=None)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def new_request_id() -> str:
    """生成短 request_id，便于在日志和响应头中追踪一次请求。"""

    return f"req_{uuid4().hex}"


@contextmanager
def log_context(
    *,
    request_id: str | None = None,
    agent_run_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
) -> Iterator[None]:
    """临时设置日志上下文字段，退出后恢复旧值。"""

    tokens = []
    if request_id is not None:
        tokens.append((request_id_var, request_id_var.set(request_id)))
    if agent_run_id is not None:
        tokens.append((agent_run_id_var, agent_run_id_var.set(agent_run_id)))
    if user_id is not None:
        tokens.append((user_id_var, user_id_var.set(user_id)))
    if conversation_id is not None:
        tokens.append((conversation_id_var, conversation_id_var.set(conversation_id)))
    try:
        yield
    finally:
        for variable, token in reversed(tokens):
            variable.reset(token)


def log_event(
    event: str,
    *,
    level: str = "INFO",
    request_id: str | None = None,
    agent_run_id: str | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    tool_name: str | None = None,
    document_id: str | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    message: str | None = None,
    **extra: Any,
) -> None:
    """写入一条结构化 JSONL 日志。"""

    normalized_level = level.upper()
    settings = get_settings()
    if _LEVELS.get(normalized_level, 20) < _LEVELS.get(settings.log_level.upper(), 20):
        return

    record = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "level": normalized_level,
        "event": event,
        "request_id": request_id or request_id_var.get(),
        "agent_run_id": agent_run_id or agent_run_id_var.get(),
        "user_id": user_id or user_id_var.get(),
        "conversation_id": conversation_id or conversation_id_var.get(),
        "tool_name": tool_name,
        "document_id": document_id,
        "status": status,
        "duration_ms": duration_ms,
        "error_code": error_code,
        "message": message,
    }
    record.update({key: value for key, value in extra.items() if value is not None})
    _append_jsonl(record)


def cleanup_old_logs() -> None:
    """删除超过保留天数的本地日志文件。"""

    settings = get_settings()
    log_dir = Path(settings.log_dir)
    if not log_dir.exists():
        return
    expire_before = time.time() - settings.log_retention_days * 24 * 60 * 60
    for path in log_dir.glob("file-agent-*.log"):
        if path.stat().st_mtime < expire_before:
            path.unlink(missing_ok=True)


def _append_jsonl(record: dict[str, Any]) -> None:
    """把日志记录追加到当天文件。"""

    settings = get_settings()
    log_dir = Path(settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"file-agent-{datetime.now().date().isoformat()}.log"
    with log_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str))
        file.write("\n")
