"""LangGraph 运行时依赖上下文。

本模块只定义运行服务对象的容器。它不能写入 AgentGraphState、checkpoint 或 graph_state_json。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.modules.agent.context import AgentContextLoader
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.llm.service import LLMIntentService


class PlannerProtocol(Protocol):
    """Planner 的最小接口，支持 deterministic、LLM 或测试 fake 互换。"""

    def plan(self, **kwargs: Any) -> Any:
        """根据消息上下文生成声明式 ToolPlan。"""


@dataclass(slots=True)
class AgentRuntimeContext:
    """一次 AgentRun 所需的运行依赖。

    注意：该对象属于运行时上下文，不属于可持久化业务状态。
    """

    planner: PlannerProtocol
    registry: ToolRegistry
    context_loader: AgentContextLoader
    llm_intent_service: LLMIntentService
