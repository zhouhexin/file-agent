"""启动 AgentRun 的服务门面。

服务支持两种模式：测试可继续使用内存态运行；HTTP 消息入口会传入数据库会话并启用持久化。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.agent.context import AgentContextLoader
from app.modules.agent.graph import build_agent_graph
from app.modules.agent.planner import DeterministicPlanner
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry
from app.core.logging import log_context, log_event
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.llm_judge import LLMClassificationJudge
from app.modules.llm.client import OpenAICompatibleLLMClient
from app.modules.llm.document_summary import LLMDocumentSummaryService
from app.modules.llm.service import LLMIntentService
from app.modules.changesets.service import persist_changeset_from_document_results
from app.modules.knowledge_graph.classification_context import (
    build_graph_classification_context,
    get_graph_repository,
)
from app.modules.knowledge_graph.semantic_context import (
    NoOpSemanticClassificationContext,
    build_semantic_classification_context,
)

class AgentRuntimeService:
    """协调 Planner、Tool Registry 和 LangGraph 执行。"""

    def __init__(
        self,
        registry_factory: Optional[Callable[[Session | None, str], ToolRegistry]] = None,
        llm_intent_service: Any = None,
        document_summary_service: Any = None,
    ) -> None:
        """注入 Registry 工厂、LLM 意图服务和文档总结服务。"""

        self.registry_factory = registry_factory or _default_registry_factory
        self.llm_intent_service = llm_intent_service or LLMIntentService()
        self.document_summary_service = document_summary_service
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
        start = time.perf_counter()
        with log_context(agent_run_id=agent_run_id, user_id=user_id, conversation_id=conversation_id):
            log_event(
                "agent.run.started",
                status="RECEIVED",
                message="AgentRun 开始",
                planner_mode=planner_mode,
                attachment_count=len(attachments or []),
            )
            try:
                final_state = self.graph.invoke(
                    initial_state,
                    config={"configurable": {"thread_id": agent_run_id}},
                    context=runtime_context,
                )
            except Exception as exc:
                duration_ms = int((time.perf_counter() - start) * 1000)
                log_event(
                    "agent.run.failed",
                    level="ERROR",
                    status="FAILED",
                    duration_ms=duration_ms,
                    error_code=exc.__class__.__name__,
                    message=str(exc),
                )
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

                changeset = persist_changeset_from_document_results(
                    db=db,
                    run=run,
                    document_results=final_state.get("document_results", []),
                )

                if changeset is not None:
                    final_state["changeset_id"] = changeset.id
                    run.changeset_id = changeset.id

                db.commit()
                db.refresh(run)


                result = repository.to_result(run)
            else:
                result = AgentRunResult(
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
                    document_results=final_state.get("document_results", []),
                    async_job_ids=final_state.get("async_job_ids", []),
                    changeset_id=final_state.get("changeset_id"),
                    operation_plan_id=final_state.get("operation_plan_id"),
                    final_response=final_state.get("final_response"),
                    errors=final_state.get("errors", []),
                )
            log_event(
                "agent.run.completed",
                status=result.status,
                duration_ms=int((time.perf_counter() - start) * 1000),
                message="AgentRun 完成",
                intent=result.intent,
                tool_count=len(result.tool_invocations),
            )
            return result

    def _build_runtime_context(
        self,
        *,
        db: Session | None,
        user_id: str,
        planner: Optional[DeterministicPlanner],
    ) -> AgentRuntimeContext:
        """为单次 AgentRun 构造运行时依赖，避免服务对象进入 State。"""

        settings = get_settings()
        llm_judge = _build_classification_judge(settings)
        graph_context = build_graph_classification_context(settings)
        semantic_context = _build_semantic_context(settings)
        graph_mode = _graph_mode_for_user(settings=settings, user_id=user_id)
        return AgentRuntimeContext(
            planner=planner or DeterministicPlanner(),
            registry=self.registry_factory(db, user_id),
            context_loader=AgentContextLoader(db),
            llm_intent_service=self.llm_intent_service,
            classification_service=DocumentClassificationService(
                db=db,
                llm_judge=llm_judge,
                mode=settings.llm_classification_mode,
                graph_context=graph_context,
                graph_top_k=settings.graph_classification_top_k,
                graph_mode=graph_mode,
                semantic_context=semantic_context,
            ),
            document_summary_service=self.document_summary_service
            or _build_document_summary_service(settings=settings, db=db),
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
            "result_summary": {},
            "document_results": [],
            "async_job_ids": [],
            "changeset_id": None,
            "operation_plan_id": None,
            "final_response": None,
            "errors": [],
        }


def _default_registry_factory(db: Session | None, user_id: str) -> ToolRegistry:
    """为每次 AgentRun 创建新的用户级 ToolRegistry。"""

    return ToolRegistry(db=db, user_id=user_id)


def _build_classification_judge(settings) -> LLMClassificationJudge | None:
    """按配置构造分类 LLM 判定器；默认不启用。"""

    if not settings.llm_enabled:
        return None
    if settings.llm_classification_mode not in {"hybrid", "review_only"}:
        return None
    client = OpenAICompatibleLLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_chat_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    return LLMClassificationJudge(
        client=client,
        allow_free_category_paths=settings.llm_classification_allow_free_paths,
    )


def _build_semantic_context(settings):
    """构造第二版语义分类上下文，依赖不完整时保持关闭式降级。"""

    if (
        not settings.graph_classification_enabled
        or not settings.graph_embedding_enabled
        or settings.graph_classification_mode == "off"
    ):
        return NoOpSemanticClassificationContext()
    try:
        repository = get_graph_repository(settings)
        return build_semantic_classification_context(settings=settings, repository=repository)
    except Exception as exc:
        log_event(
            "graph.semantic_context.loaded",
            level="WARNING",
            status="DEGRADED",
            error_code=exc.__class__.__name__,
            message="语义分类运行时不可用，已回退基础分类",
        )
        return NoOpSemanticClassificationContext(reason="SEMANTIC_CONTEXT_UNAVAILABLE")


def _graph_mode_for_user(*, settings, user_id: str) -> str:
    """按稳定用户桶执行 enabled 灰度，未命中用户继续使用 Shadow。"""

    if settings.graph_classification_mode != "enabled":
        return settings.graph_classification_mode
    rollout = int(settings.graph_classification_rollout_percent)
    if rollout >= 100:
        return "enabled"
    if rollout <= 0:
        return "shadow"
    bucket = int(hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8], 16) % 100
    return "enabled" if bucket < rollout else "shadow"


def _build_document_summary_service(*, settings, db: Session | None) -> LLMDocumentSummaryService:
    """为用户明确提出的总结请求构造 LLM 服务。

    上传和分类阶段的持久化摘要由独立的本地抽取式 Provider 负责；这里不能因为后台
    摘要启用就自动外发正文，只有聊天摘要 Provider 和全局 LLM 同时启用才允许调用模型。
    """

    if not settings.llm_enabled or settings.chat_document_summary_provider != "llm":
        return LLMDocumentSummaryService(db=db, enabled=False)
    client = OpenAICompatibleLLMClient(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        model=settings.llm_chat_model,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    return LLMDocumentSummaryService(db=db, client=client, enabled=True)
