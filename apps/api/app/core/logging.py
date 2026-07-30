"""轻量结构化文件日志。

本模块不接外部日志平台，只负责把关键运行事件写入服务器本地 JSONL 文件。
"""

from __future__ import annotations

import json
import re
import time
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from app.core.config import Settings, get_settings

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
agent_run_id_var: ContextVar[str | None] = ContextVar("agent_run_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
conversation_id_var: ContextVar[str | None] = ContextVar("conversation_id", default=None)

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_MAX_EXCEPTION_TRACEBACK_CHARS = 50_000
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"authorization|secret)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_TOKEN_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


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
    settings: Settings | None = None,
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
    event_title: str | None = None,
    stage: str | None = None,
    operator_message: str | None = None,
    cause_code: str | None = None,
    recommended_action: str | None = None,
    document_version_id: str | None = None,
    filesystem_job_id: str | None = None,
    **extra: Any,
) -> None:
    """写入一条结构化 JSONL 日志。

    请求级服务可以传入已经完成校验的 Settings，避免日志组件再次读取全局环境，
    同时保证单元测试和多配置运行场景不会因为日志依赖而改变业务执行结果。
    """

    normalized_level = level.upper()
    resolved_settings = settings or get_settings()
    if _LEVELS.get(normalized_level, 20) < _LEVELS.get(resolved_settings.log_level.upper(), 20):
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
        # 以下字段面向管理员诊断时间线。保留稳定 code 便于机器筛选，同时提供
        # 中文标题、结论和处置建议，运维人员无需理解内部节点或 Tool 名称。
        "event_title": event_title or _default_event_title(event),
        "stage": stage or _default_stage(event),
        "operator_message": operator_message or message,
        "cause_code": cause_code or error_code,
        "recommended_action": recommended_action,
        "document_version_id": document_version_id,
        "filesystem_job_id": filesystem_job_id,
    }
    record.update({key: value for key, value in extra.items() if value is not None})
    _append_jsonl(record, settings=resolved_settings)


def _default_event_title(event: str) -> str:
    """为未显式提供标题的旧日志生成可读中文事件名。"""

    if event.startswith("api."):
        return "接口请求"
    if event.startswith("agent.node."):
        return "任务阶段执行"
    if event.startswith("tool."):
        return "文件能力执行"
    if event.startswith("retrieval."):
        return "文件检索"
    if event.startswith("evidence_answer."):
        return "文件内容回答"
    if event.startswith("filesystem."):
        return "后台文件任务"
    if event.startswith("classification."):
        return "文件分类"
    return "系统处理事件"


def _default_stage(event: str) -> str:
    """从稳定事件名推导管理员可筛选的业务阶段。"""

    if event.startswith("api."):
        return "API"
    if event.startswith("agent."):
        return "AGENT"
    if event.startswith("tool."):
        return "TOOL"
    if event.startswith("retrieval."):
        return "SEARCH"
    if event.startswith("evidence_answer."):
        return "EVIDENCE"
    if event.startswith("filesystem."):
        return "ASYNC_JOB"
    if event.startswith("classification."):
        return "CLASSIFICATION"
    return "SYSTEM"


def format_exception_traceback(error: BaseException, *, settings: Settings | None = None) -> str:
    """生成仅供服务器日志使用的脱敏异常堆栈。

    traceback 用于运维定位代码位置，不能写入数据库或普通用户响应。已知配置密钥和
    常见凭据赋值会被替换；同时限制单条堆栈长度，避免异常对象造成日志无限膨胀。
    """

    resolved_settings = settings or get_settings()
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    )
    for sensitive_value in (
        resolved_settings.jwt_secret_key,
        resolved_settings.llm_api_key,
        resolved_settings.neo4j_password,
    ):
        if sensitive_value and len(sensitive_value) >= 4:
            formatted = formatted.replace(sensitive_value, "<redacted>")
    formatted = _BEARER_TOKEN_PATTERN.sub("Bearer <redacted>", formatted)
    formatted = _SENSITIVE_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        formatted,
    )
    if len(formatted) > _MAX_EXCEPTION_TRACEBACK_CHARS:
        return formatted[:_MAX_EXCEPTION_TRACEBACK_CHARS] + "\n...[traceback truncated]"
    return formatted


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


def _append_jsonl(record: dict[str, Any], *, settings: Settings | None = None) -> None:
    """把日志记录追加到当天文件。"""

    resolved_settings = settings or get_settings()
    log_dir = Path(resolved_settings.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"file-agent-{datetime.now().date().isoformat()}.log"
    with log_path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str))
        file.write("\n")
