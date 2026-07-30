"""AgentRun 中文运维诊断投影。

本模块把 AgentRun、ToolInvocation、异步文件任务和本地 JSONL 日志合并为管理员
可读时间线。它只提供诊断投影，不替代数据库审计，也不向普通用户开放。
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.models import AgentRun, FilesystemJob, FilesystemJobEvent, ToolInvocation


_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\r\n\t\"']+")
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![\w:])/(?:[^/\s]+/)+[^,\s;]*")
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(password|api[_-]?key|token|secret)\b(\s*[:=]\s*)([^\s,;]+)"
)


class AgentRunListItem(BaseModel):
    """管理员任务列表中的脱敏摘要。"""

    id: str
    conversation_id: str
    user_id: str
    intent: str | None
    status: str
    planner_mode: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class AgentDiagnosticEvent(BaseModel):
    """一次可供管理员直接理解的任务诊断事件。"""

    occurred_at: datetime
    stage: str
    event_title: str
    status: str | None = None
    operator_message: str
    cause_code: str | None = None
    recommended_action: str | None = None
    duration_ms: int | None = None
    tool_name: str | None = None
    document_id: str | None = None
    document_version_id: str | None = None
    filesystem_job_id: str | None = None


class AgentRunDiagnosticsResponse(BaseModel):
    """管理员查看单次 AgentRun 时使用的诊断响应。"""

    run: AgentRunListItem
    summary: str
    recommended_actions: list[str] = Field(default_factory=list)
    events: list[AgentDiagnosticEvent] = Field(default_factory=list)


class AgentRunDiagnosticsService:
    """聚合持久化审计和服务器日志，输出中文运维视图。"""

    def __init__(self, *, db: Session, settings: Settings | None = None) -> None:
        """保存请求级数据库会话和日志配置。"""

        self.db = db
        self.settings = settings or get_settings()

    def list_runs(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[AgentRunListItem]:
        """按更新时间倒序列出最近任务，可选按状态过滤。"""

        query = self.db.query(AgentRun)
        if status:
            query = query.filter(AgentRun.status == status)
        rows = query.order_by(AgentRun.updated_at.desc()).limit(limit).all()
        return [self._run_item(row) for row in rows]

    def get_diagnostics(self, *, agent_run_id: str) -> AgentRunDiagnosticsResponse | None:
        """返回单次任务的中文时间线；不存在时返回 None。"""

        run = self.db.get(AgentRun, agent_run_id)
        if run is None:
            return None
        events = self._database_events(run)
        events.extend(self._log_events(run.id))
        # SQLite 测试库可能返回 naive datetime，而 JSONL 使用带时区 ISO 时间；
        # 转为时间戳排序，避免诊断接口因数据库驱动差异返回 500。
        events.sort(key=lambda item: item.occurred_at.timestamp())
        summary, actions = self._diagnostic_summary(run=run, events=events)
        return AgentRunDiagnosticsResponse(
            run=self._run_item(run),
            summary=summary,
            recommended_actions=actions,
            events=events[-300:],
        )

    def _database_events(self, run: AgentRun) -> list[AgentDiagnosticEvent]:
        """从可靠的数据库审计事实构造基础时间线。"""

        events = [
            AgentDiagnosticEvent(
                occurred_at=run.created_at,
                stage="AGENT",
                event_title="收到用户任务",
                status="RECEIVED",
                operator_message="系统已创建任务并开始理解用户请求。",
            )
        ]
        invocations = (
            self.db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == run.id)
            .order_by(ToolInvocation.created_at.asc())
            .all()
        )
        for invocation in invocations:
            output = (
                invocation.output_json
                if isinstance(invocation.output_json, dict)
                else {}
            )
            error = output.get("error") if isinstance(output.get("error"), dict) else {}
            events.append(
                AgentDiagnosticEvent(
                    occurred_at=invocation.finished_at or invocation.created_at,
                    stage="TOOL",
                    event_title=_tool_title(invocation.tool_name),
                    status=invocation.status,
                    operator_message=_tool_message(
                        tool_name=invocation.tool_name,
                        status=invocation.status,
                        output=output,
                    ),
                    cause_code=str(error.get("code") or "") or None,
                    recommended_action=_tool_recommended_action(
                        status=invocation.status,
                        output=output,
                    ),
                    tool_name=invocation.tool_name,
                )
            )
        job_ids = _async_job_ids(run.graph_state_json)
        if job_ids:
            jobs = (
                self.db.query(FilesystemJob)
                .filter(FilesystemJob.id.in_(job_ids))
                .all()
            )
            jobs_by_id = {job.id: job for job in jobs}
            job_events = (
                self.db.query(FilesystemJobEvent)
                .filter(FilesystemJobEvent.job_id.in_(job_ids))
                .order_by(FilesystemJobEvent.created_at.asc())
                .all()
            )
            for job_id in job_ids:
                job = jobs_by_id.get(job_id)
                if job is None:
                    events.append(
                        AgentDiagnosticEvent(
                            occurred_at=run.updated_at,
                            stage="ASYNC_JOB",
                            event_title="后台任务记录缺失",
                            status="MISSING",
                            operator_message="任务引用了一个不存在的后台文件任务。",
                            cause_code="FILESYSTEM_JOB_MISSING",
                            recommended_action="检查数据库清理策略和任务创建事务。",
                            filesystem_job_id=job_id,
                        )
                    )
                    continue
                events.append(
                    AgentDiagnosticEvent(
                        occurred_at=job.updated_at,
                        stage="ASYNC_JOB",
                        event_title="后台文件任务状态",
                        status=job.status,
                        operator_message=_job_message(job),
                        cause_code=(
                            "FILESYSTEM_JOB_FAILED"
                            if job.status == "FAILED"
                            else None
                        ),
                        recommended_action=_job_recommended_action(job),
                        filesystem_job_id=job.id,
                    )
                )
            for item in job_events:
                events.append(
                    AgentDiagnosticEvent(
                        occurred_at=item.created_at,
                        stage="ASYNC_JOB",
                        event_title="后台任务进度",
                        status=item.level,
                        operator_message=item.message or "后台任务记录了一次进度更新。",
                        filesystem_job_id=item.job_id,
                    )
                )
        events.append(
            AgentDiagnosticEvent(
                occurred_at=run.updated_at,
                stage="AGENT",
                event_title="当前任务状态",
                status=run.status,
                operator_message=_run_status_message(run),
                cause_code="AGENT_RUN_FAILED" if run.status == "FAILED" else None,
                recommended_action=(
                    "查看失败事件和后台任务状态，修复后让用户重新发起请求。"
                    if run.status == "FAILED"
                    else None
                ),
            )
        )
        return events

    def _log_events(self, agent_run_id: str) -> list[AgentDiagnosticEvent]:
        """读取匹配 AgentRun 的结构化日志，只投影运维安全字段。"""

        log_dir = Path(self.settings.log_dir)
        if not log_dir.exists():
            return []
        events: deque[AgentDiagnosticEvent] = deque(maxlen=500)
        for path in sorted(log_dir.glob("file-agent-*.log")):
            try:
                file = path.open("r", encoding="utf-8")
            except OSError:
                continue
            # 日志文件可能很大，诊断接口必须逐行扫描，不能一次性读入内存。
            # 只保留最近 500 条匹配事件，避免单个异常任务放大 API 响应。
            with file:
                for line in file:
                    try:
                        record = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if str(record.get("agent_run_id") or "") != agent_run_id:
                        continue
                    occurred_at = _parse_time(record.get("ts"))
                    if occurred_at is None:
                        continue
                    events.append(
                        AgentDiagnosticEvent(
                            occurred_at=occurred_at,
                            stage=str(record.get("stage") or "SYSTEM"),
                            event_title=str(
                                record.get("event_title") or "系统处理事件"
                            ),
                            status=(
                                str(record.get("status"))
                                if record.get("status") is not None
                                else None
                            ),
                            operator_message=_safe_operator_text(
                                record.get("operator_message")
                                or record.get("message"),
                                fallback="系统记录了一次处理事件。",
                            ),
                            cause_code=(
                                str(
                                    record.get("cause_code")
                                    or record.get("error_code")
                                )
                                if record.get("cause_code")
                                or record.get("error_code")
                                else None
                            ),
                            recommended_action=(
                                _safe_operator_text(
                                    record.get("recommended_action")
                                )
                                if record.get("recommended_action")
                                else None
                            ),
                            duration_ms=(
                                int(record["duration_ms"])
                                if record.get("duration_ms") is not None
                                else None
                            ),
                            tool_name=(
                                str(record.get("tool_name"))
                                if record.get("tool_name")
                                else None
                            ),
                            document_id=(
                                str(record.get("document_id"))
                                if record.get("document_id")
                                else None
                            ),
                            document_version_id=(
                                str(record.get("document_version_id"))
                                if record.get("document_version_id")
                                else None
                            ),
                            filesystem_job_id=(
                                str(record.get("filesystem_job_id"))
                                if record.get("filesystem_job_id")
                                else None
                            ),
                        )
                    )
        return list(events)

    @staticmethod
    def _run_item(run: AgentRun) -> AgentRunListItem:
        """把 ORM 对象转换成管理员列表模型。"""

        return AgentRunListItem(
            id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.user_id,
            intent=run.intent,
            status=run.status,
            planner_mode=run.planner_mode,
            error_message=(
                _safe_operator_text(run.error_message)
                if run.error_message
                else None
            ),
            created_at=run.created_at,
            updated_at=run.updated_at,
        )

    @staticmethod
    def _diagnostic_summary(
        *,
        run: AgentRun,
        events: list[AgentDiagnosticEvent],
    ) -> tuple[str, list[str]]:
        """根据终态和异步依赖生成简短运维结论。"""

        if run.status == "COMPLETED":
            return "任务已经完成，当前未发现阻塞状态。", []
        if run.status == "WAITING_FOR_ASYNC_JOB":
            failed = any(
                event.stage == "ASYNC_JOB" and event.status == "FAILED"
                for event in events
            )
            if failed:
                return (
                    "任务仍显示处理中，但依赖的后台任务已经失败。",
                    ["检查 worker 错误后终止或重新调度该任务。"],
                )
            return (
                "任务正在等待文件解析或索引完成。",
                ["确认 worker 正常运行并持续刷新本页面。"],
            )
        if run.status == "FAILED":
            return (
                "任务执行失败，时间线中已列出最近的失败阶段。",
                ["优先处理带有错误编号的事件，再让用户重新发起请求。"],
            )
        if run.status == "WAITING_FOR_CONFIRMATION":
            return "任务正在等待用户确认高风险操作。", []
        return "任务尚未进入最终状态。", ["检查最近事件是否长时间没有更新。"]


def _async_job_ids(graph_state: Any) -> list[str]:
    """从安全状态快照读取异步任务 ID，去重并限制查询数量。"""

    if not isinstance(graph_state, dict):
        return []
    values = graph_state.get("async_job_ids") or []
    if graph_state.get("async_job_id"):
        values = [*values, graph_state["async_job_id"]]
    return list(dict.fromkeys(str(value) for value in values if str(value)))[:100]


def _parse_time(value: Any) -> datetime | None:
    """解析 JSONL ISO 时间，坏日志不影响诊断页面。"""

    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _tool_title(tool_name: str) -> str:
    """把内部 Tool 名称转换成管理员业务阶段名称。"""

    labels = {
        "hybrid-search": "查找文件",
        "evidence-answer": "读取文件并回答",
        "extract-document-text": "解析文件正文",
        "document-classification": "生成分类建议",
        "operation-plan-create": "生成操作确认计划",
        "confirmed-file-action": "执行已确认的文件操作",
    }
    return labels.get(tool_name, "执行文件能力")


def _tool_message(*, tool_name: str, status: str, output: dict[str, Any]) -> str:
    """根据有限 Tool 输出生成不含正文和路径的运维说明。"""

    if status == "FAILED" or output.get("ok") is False:
        error = output.get("error") if isinstance(output.get("error"), dict) else {}
        return _safe_operator_text(
            error.get("message"),
            fallback="该处理阶段执行失败。",
        )
    if output.get("kind") == "filesystem_job":
        return "文件尚未准备完成，任务已转入后台处理。"
    if tool_name == "hybrid-search":
        return f"文件检索完成，返回 {int(output.get('total_returned') or 0)} 个结果。"
    return "该处理阶段已完成。"


def _tool_recommended_action(*, status: str, output: dict[str, Any]) -> str | None:
    """为 Tool 失败或异步等待提供管理员处置建议。"""

    if output.get("kind") == "filesystem_job":
        return "确认 worker 正常运行，并检查后台任务进度。"
    if status == "FAILED" or output.get("ok") is False:
        return "根据错误编号检查服务配置、索引状态或输入范围。"
    return None


def _job_message(job: FilesystemJob) -> str:
    """生成后台任务当前状态说明。"""

    if job.status == "COMPLETED":
        return "后台文件处理已经完成，等待 AgentRun 汇总结果。"
    if job.status == "FAILED":
        return _safe_operator_text(
            job.error_message,
            fallback="后台文件处理失败。",
        )
    if job.status == "RUNNING":
        return f"后台任务执行中，进度 {job.progress_current}/{job.progress_total}。"
    return "后台任务等待 worker 领取。"


def _job_recommended_action(job: FilesystemJob) -> str | None:
    """根据后台任务状态给出有限运维建议。"""

    if job.status == "FAILED":
        return "检查失败文件和 worker 日志，确认是否已达到最大重试次数。"
    if job.status == "PENDING" and not job.lease_owner:
        return "确认 worker 已启动并监听对应队列。"
    return None


def _run_status_message(run: AgentRun) -> str:
    """将 AgentRun 枚举转换为管理员易懂结论。"""

    labels = {
        "COMPLETED": "任务已经完成并生成用户回执。",
        "FAILED": _safe_operator_text(
            run.error_message,
            fallback="任务执行失败。",
        ),
        "WAITING_FOR_ASYNC_JOB": "任务正在等待后台文件处理完成。",
        "WAITING_FOR_CONFIRMATION": "任务正在等待用户确认操作计划。",
        "NEEDS_REVIEW": "任务需要人工复核后才能继续。",
    }
    return labels.get(run.status, f"任务当前状态为 {run.status}。")


def _safe_operator_text(value: Any, *, fallback: str = "") -> str:
    """清理运维投影中的路径和凭据，原始审计仍保留在受控数据库或服务器日志。"""

    text = str(value or "").strip() or fallback
    text = _WINDOWS_ABSOLUTE_PATH.sub("<服务器路径>", text)
    text = _POSIX_ABSOLUTE_PATH.sub("<服务器路径>", text)
    text = _SENSITIVE_VALUE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
        text,
    )
    return text[:2000]
