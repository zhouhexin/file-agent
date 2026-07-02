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


class ClassificationServiceProtocol(Protocol):
    """分类服务的最小接口，支持测试 fake 和真实服务互换。"""

    def classify(self, **kwargs: Any) -> dict[str, Any]:
        """根据 document_id 和 extraction_run_id 返回分类结果。"""


class DocumentSummaryServiceProtocol(Protocol):
    """文档总结服务的最小接口，支持真实 LLM 和测试 fake 互换。"""

    def summarize_documents(self, **kwargs: Any) -> str | None:
        """基于完整文档正文返回总结文本。"""


@dataclass(slots=True)
class AgentRuntimeContext:
    """一次 AgentRun 所需的运行依赖。

    注意：该对象属于运行时上下文，不属于可持久化业务状态。
    """

    planner: PlannerProtocol
    registry: ToolRegistry
    context_loader: AgentContextLoader
    llm_intent_service: LLMIntentService
    classification_service: ClassificationServiceProtocol
    document_summary_service: DocumentSummaryServiceProtocol
