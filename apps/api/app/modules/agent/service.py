"""启动 AgentRun 的服务门面。

服务支持两种模式：测试可继续使用内存态运行；HTTP 消息入口会传入数据库会话并启用持久化。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.modules.agent.context import AgentContextLoader
from app.modules.agent.graph import build_agent_graph
from app.modules.agent.planner import DeterministicPlanner
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.llm.service import LLMIntentService


class AgentRuntimeService:
    """协调 Planner、Tool Registry 和 LangGraph 执行。"""

    def __init__(self, registry: Optional[ToolRegistry] = None, llm_intent_service: Any = None) -> None:
        """注入 Tool Registry 和 LLM 意图服务，便于测试替换外部模型。"""

        self.registry = registry
        self.llm_intent_service = llm_intent_service or LLMIntentService()
        self.graph = build_agent_graph()

    def run_message(
        self,
        conversation_id: str,
        user_id: str,
        message_id: str,
        message: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        planner: Optional[DeterministicPlanner] = None,
        db: Session | None = None,
    ) -> AgentRunResult:
        """从一条会话消息启动一次 AgentRun。

        如果传入数据库会话，本方法会持久化 AgentRun 和 ToolInvocation；否则保持内存态结果。
        """

        repository = AgentRunRepository(db) if db is not None else None
        run = (
            repository.create_run(conversation_id=conversation_id, message_id=message_id, user_id=user_id)
            if repository is not None
            else None
        )
        agent_run_id = run.id if run is not None else str(uuid4())

        planner_mode = "deterministic" if planner is not None or not self.llm_intent_service.enabled else "llm"
        runtime_context = self._build_runtime_context(db=db, user_id=user_id, planner=planner)
        initial_state = self._build_initial_state(
            agent_run_id=agent_run_id,
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message_id,
            message=message,
            attachments=attachments or [],
            planner_mode=planner_mode,
        )
        try:
            final_state = self.graph.invoke(
                initial_state,
                config={"configurable": {"thread_id": agent_run_id}},
                context=runtime_context,
            )
        except Exception as exc:
            if repository is not None and run is not None:
                repository.mark_failed(run, str(exc))
            raise

        invocation_records = [
            ToolInvocationRecord.model_validate(item)
            for item in final_state.get("tool_invocations", [])
        ]
        if repository is not None and run is not None:
            for record in invocation_records:
                repository.create_tool_invocation(agent_run_id=run.id, record=record)
            repository.update_run_from_state(run, final_state)
            return repository.to_result(run)

        return AgentRunResult(
            agent_run_id=final_state["agent_run_id"],
            conversation_id=final_state["conversation_id"],
            user_id=final_state["user_id"],
            message_id=final_state["message_id"],
            intent=final_state.get("intent"),
            status=final_state["status"],
            selected_skills=final_state.get("selected_skills", []),
            tool_plan=final_state.get("tool_plan", {}),
            tool_results=final_state.get("tool_results", []),
            tool_invocations=invocation_records,
            changeset_id=final_state.get("changeset_id"),
            operation_plan_id=final_state.get("operation_plan_id"),
            final_response=final_state.get("final_response"),
            errors=final_state.get("errors", []),
        )

    def _build_runtime_context(
        self,
        *,
        db: Session | None,
        user_id: str,
        planner: Optional[DeterministicPlanner],
    ) -> AgentRuntimeContext:
        """为单次 AgentRun 构造运行时依赖，避免服务对象进入 State。"""

        return AgentRuntimeContext(
            planner=planner or DeterministicPlanner(),
            registry=self.registry or ToolRegistry(db=db, user_id=user_id),
            context_loader=AgentContextLoader(db),
            llm_intent_service=self.llm_intent_service,
        )

    def _build_initial_state(
        self,
        *,
        agent_run_id: str,
        conversation_id: str,
        user_id: str,
        message_id: str,
        message: str,
        attachments: List[Dict[str, Any]],
        planner_mode: str,
    ) -> Dict[str, Any]:
        """构造只包含可持久化业务状态的 LangGraph 初始 State。"""

        return {
            "agent_run_id": agent_run_id,
            "conversation_id": conversation_id,
            "user_id": user_id,
            "message_id": message_id,
            "message": message,
            "attachments": attachments,
            "context_documents": [],
            "user_intent_plan": {},
            "planner_mode": planner_mode,
            "status": "RECEIVED",
            "intent": None,
            "slots": {},
            "selected_skills": [],
            "tool_plan": {},
            "tool_results": [],
            "tool_invocations": [],
            "changeset_id": None,
            "operation_plan_id": None,
            "final_response": None,
            "errors": [],
        }
