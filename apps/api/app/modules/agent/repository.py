"""AgentRun 和 ToolInvocation 的持久化仓库。

LangGraph 节点仍只负责状态流转，运行审计数据通过仓库统一写入数据库。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentRun, ToolInvocation, utcnow
from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.classification.service import persist_document_results_classifications


class AgentRunRepository:
    """封装 AgentRun 和 ToolInvocation 的数据库操作。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def create_run(self, conversation_id: str, message_id: str, user_id: str) -> AgentRun:
        """创建 RECEIVED 状态的 AgentRun。"""

        run = AgentRun(
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            status="RECEIVED",
        )
        self.db.add(run)
        self.db.flush()
        return run

    def update_run_from_state(self, run: AgentRun, state: dict[str, Any]) -> AgentRun:
        """用 LangGraph 最终状态更新 AgentRun 审计字段。"""

        run.intent = state.get("intent")
        run.status = state.get("status", run.status)
        run.selected_skills_json = state.get("selected_skills", [])
        run.plan_json = state.get("tool_plan", {})
        run.graph_state_json = _safe_graph_state_snapshot(state)
        run.final_response = state.get("final_response")
        run.error_message = "; ".join(state.get("errors", [])) or None
        run.updated_at = utcnow()
        persist_document_results_classifications(
            db=self.db,
            agent_run_id=run.id,
            document_results=state.get("document_results", []),
        )
        self.db.flush()
        return run

    def mark_failed(self, run: AgentRun, error_message: str) -> AgentRun:
        """运行失败时记录 FAILED 状态和错误信息。"""

        run.status = "FAILED"
        run.error_message = error_message
        run.updated_at = utcnow()
        self.db.flush()
        return run

    def create_tool_invocation(self, agent_run_id: str, record: ToolInvocationRecord) -> ToolInvocation:
        """把一次 Tool 调用记录写入数据库。"""

        invocation = ToolInvocation(
            id=record.id,
            agent_run_id=agent_run_id,
            tool_name=record.tool_name,
            input_json=record.input_json,
            output_json=record.output_json,
            status=record.status,
            changeset_id=record.changeset_id,
            operation_plan_id=record.operation_plan_id,
            finished_at=utcnow(),
        )
        self.db.add(invocation)
        self.db.flush()
        return invocation

    def get_run(self, agent_run_id: str) -> AgentRun | None:
        """按 id 查询 AgentRun。"""

        return self.db.get(AgentRun, agent_run_id)

    def list_tool_invocations(self, agent_run_id: str) -> list[ToolInvocation]:
        """查询某次 AgentRun 的 Tool 调用记录。"""

        return (
            self.db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == agent_run_id)
            .order_by(ToolInvocation.created_at.asc())
            .all()
        )

    def to_result(self, run: AgentRun, invocations: list[ToolInvocation] | None = None) -> AgentRunResult:
        """把 ORM AgentRun 转为 API 返回模型。"""

        invocation_models = [
            ToolInvocationRecord(
                id=item.id,
                tool_name=item.tool_name,
                input_json=item.input_json,
                output_json=item.output_json,
                status=item.status,
                changeset_id=item.changeset_id,
                operation_plan_id=item.operation_plan_id,
            )
            for item in (invocations if invocations is not None else self.list_tool_invocations(run.id))
        ]
        return AgentRunResult(
            agent_run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.user_id,
            message_id=run.message_id,
            intent=run.intent,
            status=run.status,
            selected_skills=run.selected_skills_json,
            tool_plan=run.plan_json,
            tool_results=[item.output_json for item in invocation_models],
            tool_invocations=invocation_models,
            changeset_id=_last_non_empty([item.changeset_id for item in invocation_models]),
            operation_plan_id=_last_non_empty([item.operation_plan_id for item in invocation_models]),
            final_response=run.final_response,
            errors=[run.error_message] if run.error_message else [],
        )


def _safe_graph_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """保存可 JSON 序列化的图状态快照，避免把 registry/planner 对象写入数据库。"""

    return {
        "agent_run_id": state.get("agent_run_id"),
        "conversation_id": state.get("conversation_id"),
        "message_id": state.get("message_id"),
        "planner_mode": state.get("planner_mode"),
        "status": state.get("status"),
        "intent": state.get("intent"),
        "slots": state.get("slots", {}),
        "selected_skills": state.get("selected_skills", []),
        "context_documents": state.get("context_documents", []),
        "user_intent_plan": state.get("user_intent_plan", {}),
        "tool_results": state.get("tool_results", []),
        "document_results": state.get("document_results", []),
        "changeset_id": state.get("changeset_id"),
        "operation_plan_id": state.get("operation_plan_id"),
        "final_response": state.get("final_response"),
        "errors": state.get("errors", []),
    }


def _last_non_empty(values: list[str | None]) -> str | None:
    """从列表中取最后一个非空标识。"""

    for value in reversed(values):
        if value:
            return value
    return None
