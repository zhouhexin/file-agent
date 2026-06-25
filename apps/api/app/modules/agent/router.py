"""用于查看 Agent Runtime 状态和能力的 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry

router = APIRouter(prefix="/api/agent", tags=["agent"])
agent_runs_router = APIRouter(prefix="/api/agent-runs", tags=["agent-runs"])


class ToolInvocationsResponse(BaseModel):
    """AgentRun Tool 调用列表响应。"""

    tool_invocations: list[ToolInvocationRecord]


@router.get("/tools")
def list_agent_tools() -> dict:
    """返回白名单 Registry 暴露的 MVP Tool 目录。"""

    return {"tools": ToolRegistry().list_tools()}


@agent_runs_router.get("/{agent_run_id}", response_model=AgentRunResult)
def get_agent_run(agent_run_id: str, db: Session = Depends(get_db)) -> AgentRunResult:
    """按 id 查询持久化 AgentRun。"""

    repository = AgentRunRepository(db)
    run = repository.get_run(agent_run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="AgentRun not found")
    return repository.to_result(run)


@agent_runs_router.get("/{agent_run_id}/tool-invocations", response_model=ToolInvocationsResponse)
def list_tool_invocations(
    agent_run_id: str,
    db: Session = Depends(get_db),
) -> ToolInvocationsResponse:
    """查询某次 AgentRun 的持久化 Tool 调用记录。"""

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
