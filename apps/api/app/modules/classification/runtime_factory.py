"""上传 Worker 与 Agent 共用的分类运行时工厂。"""

from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import log_event
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.llm_judge import LLMClassificationJudge
from app.modules.knowledge_graph.classification_context import (
    build_graph_classification_context,
    get_graph_repository,
)
from app.modules.knowledge_graph.semantic_context import (
    NoOpSemanticClassificationContext,
    build_semantic_classification_context,
)
from app.modules.llm.client import OpenAICompatibleLLMClient


class ClassificationRuntimeFactory:
    """按同一组开关构造请求级分类服务，避免入口间配置漂移。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """保存部署配置；服务对象和数据库会话不会进入 AgentGraphState。"""

        self.settings = settings or get_settings()

    def create(self, *, db: Session, user_id: str) -> DocumentClassificationService:
        """为一次 Worker 任务或 AgentRun 创建隔离的分类运行时。"""

        settings = self.settings
        graph_mode = self.graph_mode_for_user(user_id=user_id)
        return DocumentClassificationService(
            db=db,
            llm_judge=self._build_llm_judge(),
            mode=settings.llm_classification_mode,
            graph_context=build_graph_classification_context(settings),
            graph_top_k=settings.graph_classification_top_k,
            graph_mode=graph_mode,
            semantic_context=self._build_semantic_context(graph_mode=graph_mode),
        )

    def graph_mode_for_user(self, *, user_id: str) -> str:
        """按稳定用户桶执行图谱 enabled 灰度，未命中用户继续 Shadow。"""

        configured = self.settings.graph_classification_mode
        if configured != "enabled":
            return configured
        rollout = int(self.settings.graph_classification_rollout_percent)
        if rollout >= 100:
            return "enabled"
        if rollout <= 0:
            return "shadow"
        bucket = int(hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8], 16) % 100
        return "enabled" if bucket < rollout else "shadow"

    def classifier_version_for_user(self, *, user_id: str) -> str:
        """不构造外部依赖即可计算指定用户当前分类器版本。"""

        graph_mode = self.graph_mode_for_user(user_id=user_id)
        summary_mode = (
            "summary"
            if self.settings.llm_classification_summary_enabled
            else "fulltext"
        )
        return (
            f"taxonomy-{summary_mode}-first-"
            f"{self.settings.llm_classification_mode}-graph-{graph_mode}-v5"
        )

    def _build_llm_judge(self) -> LLMClassificationJudge | None:
        """仅在显式启用 LLM 判定模式时构造外部模型客户端。"""

        settings = self.settings
        if not settings.llm_enabled or settings.llm_classification_mode not in {
            "hybrid",
            "review_only",
        }:
            return None
        return LLMClassificationJudge(
            client=OpenAICompatibleLLMClient(
                api_key=settings.llm_api_key,
                base_url=settings.llm_base_url,
                model=settings.llm_chat_model,
                timeout_seconds=settings.llm_timeout_seconds,
            ),
            allow_free_category_paths=settings.llm_classification_allow_free_paths,
        )

    def _build_semantic_context(self, *, graph_mode: str):
        """构造语义召回上下文，依赖不完整时关闭式降级。"""

        settings = self.settings
        if (
            not settings.graph_classification_enabled
            or not settings.graph_embedding_enabled
            or graph_mode == "off"
        ):
            return NoOpSemanticClassificationContext()
        try:
            repository = get_graph_repository(settings)
            return build_semantic_classification_context(
                settings=settings,
                repository=repository,
            )
        except Exception as exc:
            log_event(
                "graph.semantic_context.loaded",
                level="WARNING",
                status="DEGRADED",
                error_code=exc.__class__.__name__,
                message="语义分类运行时不可用，已回退基础分类",
            )
            return NoOpSemanticClassificationContext(
                reason="SEMANTIC_CONTEXT_UNAVAILABLE"
            )
