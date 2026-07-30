"""用于查看 Agent Runtime 状态和能力的 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.agent.capabilities.service import load_agent_capabilities
from app.modules.agent.diagnostics import (
    AgentRunDiagnosticsResponse,
    AgentRunDiagnosticsService,
    AgentRunListItem,
)
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.auth.dependencies import require_ops_or_admin

router = APIRouter(prefix="/api/agent", tags=["agent"])
agent_runs_router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])
admin_agent_runs_router = APIRouter(
    prefix="/api/admin/agent-runs",
    tags=["admin-agent-runs"],
)


class ToolInvocationsResponse(BaseModel):
    """AgentRun Tool 调用列表响应。"""

    tool_invocations: list[ToolInvocationRecord]


@admin_agent_runs_router.get("", response_model=list[AgentRunListItem])
def list_admin_agent_runs(
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> list[AgentRunListItem]:
    """向 ops/admin 返回最近任务摘要，不暴露文件正文或模型 Prompt。"""

    safe_limit = min(max(limit, 1), 200)
    return AgentRunDiagnosticsService(db=db).list_runs(
        status=status,
        limit=safe_limit,
    )


@admin_agent_runs_router.get(
    "/{agent_run_id}/diagnostics",
    response_model=AgentRunDiagnosticsResponse,
)
def get_admin_agent_run_diagnostics(
    agent_run_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> AgentRunDiagnosticsResponse:
    """向 ops/admin 返回中文诊断时间线，普通用户无权访问。"""

    result = AgentRunDiagnosticsService(db=db).get_diagnostics(
        agent_run_id=agent_run_id
    )
    if result is None:
        raise HTTPException(status_code=404, detail="AgentRun not found")
    return result


@router.get("/tools")
def list_agent_tools(
    _current_user: User = Depends(require_ops_or_admin),
) -> dict:
    """向 ops/admin 返回白名单 Registry 暴露的内部 Tool 目录。"""

    return {"tools": ToolRegistry().list_tools()}


@router.get("/capabilities")
def get_agent_capabilities() -> dict:
    """返回前端新手引导和能力介绍使用的固定能力清单。"""

    return load_agent_capabilities(detail_level="brief")


@agent_runs_router.get("/{agent_run_id}", response_model=AgentRunResult)
def get_agent_run(
    agent_run_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> AgentRunResult:
    """允许 ops/admin 按 id 查询持久化 AgentRun 审计详情。"""

    repository = AgentRunRepository(db)
    run = repository.get_run(agent_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="AgentRun not found")
    return repository.to_result(run)


@agent_runs_router.get("/{agent_run_id}/tool-invocations", response_model=ToolInvocationsResponse)
def list_tool_invocations(
    agent_run_id: str,
    db: Session = Depends(get_db),
    _current_user: User = Depends(require_ops_or_admin),
) -> ToolInvocationsResponse:
    """允许 ops/admin 查询某次 AgentRun 的持久化 Tool 调用记录。"""

    repository = AgentRunRepository(db)
    run = repository.get_run(agent_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="AgentRun not found")
    return ToolInvocationsResponse(
        tool_invocations=[
            ToolInvocationRecord(
                id=item.id,
                tool_name=item.tool_name,
                input_json=item.input_json,
                output_json=item.output_json,
                status=item.status,
                changeset_id=item.changeset_id,
                operation_plan_id=item.operation_plan_id,
            )
            for item in repository.list_tool_invocations(agent_run_id)
        ],
    )
