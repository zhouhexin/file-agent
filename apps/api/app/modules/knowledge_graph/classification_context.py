"""分类服务使用的图谱上下文及运行时工厂。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
import time
from typing import Any, Protocol

from app.core.logging import log_event
from app.modules.knowledge_graph.neo4j_repository import Neo4jGraphRepository
from app.modules.knowledge_graph.repository import GraphRepository
from app.modules.knowledge_graph.schemas import (
    GraphCandidateSeed,
    GraphClassificationResult,
)


class GraphClassificationContext(Protocol):
    """分类服务可使用的只读图谱协议。"""

    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        document_id: str,
        document_version_id: str | None,
        limit: int,
    ) -> GraphClassificationResult:
        """查询候选分类的图谱支持，不接收文件正文。"""

    def health_check(self) -> dict[str, str]:
        """返回图谱连接状态。"""


@dataclass(slots=True)
class NoOpGraphClassificationContext:
    """图谱关闭时保持现有分类行为。"""

    reason: str = "GRAPH_DISABLED"

    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        document_id: str,
        document_version_id: str | None,
        limit: int,
    ) -> GraphClassificationResult:
        """返回关闭状态，不修改候选。"""

        return GraphClassificationResult(status="DISABLED", warnings=[self.reason])

    def health_check(self) -> dict[str, str]:
        """返回关闭状态。"""

        return {"status": "disabled", "reason": self.reason}


@dataclass(slots=True)
class UnavailableGraphClassificationContext(NoOpGraphClassificationContext):
    """图谱启用但配置或依赖不可用时的降级上下文。"""

    reason: str = "GRAPH_UNAVAILABLE"

    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        document_id: str,
        document_version_id: str | None,
        limit: int,
    ) -> GraphClassificationResult:
        """返回降级状态。"""

        return GraphClassificationResult(status="DEGRADED", warnings=[self.reason])

    def health_check(self) -> dict[str, str]:
        """返回不可用状态。"""

        return {"status": "unavailable", "reason": self.reason}


@dataclass(slots=True)
class FakeGraphClassificationContext:
    """测试使用的确定性图谱上下文。"""

    result: GraphClassificationResult

    def expand_candidates(self, **kwargs: Any) -> GraphClassificationResult:
        """返回预置结果。"""

        return self.result

    def health_check(self) -> dict[str, str]:
        """返回测试健康状态。"""

        return {"status": "ok"}


class Neo4jGraphClassificationContext:
    """通过 Neo4j Repository 查询候选支持。"""

    def __init__(self, *, repository: GraphRepository, max_hops: int = 1) -> None:
        """保存图谱仓库和传播限制。"""

        self.repository = repository
        self.max_hops = max(1, min(2, max_hops))

    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        document_id: str,
        document_version_id: str | None,
        limit: int,
    ) -> GraphClassificationResult:
        """读取图谱候选，异常时返回可诊断降级结果。"""

        start = time.perf_counter()
        try:
            supports = self.repository.expand_candidates(
                candidates=candidates,
                max_hops=self.max_hops,
                limit=limit,
            )
        except Exception as exc:
            log_event(
                "classification.graph_query.degraded",
                level="WARNING",
                document_id=document_id or None,
                status="DEGRADED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message="图谱分类查询失败，已回退基础分类。",
            )
            return GraphClassificationResult(status="DEGRADED", warnings=["GRAPH_UNAVAILABLE"])
        log_event(
            "classification.graph_query.completed",
            document_id=document_id or None,
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="图谱分类候选查询完成",
            candidate_count=len(candidates),
            graph_candidate_count=len(supports),
        )
        return GraphClassificationResult(status="COMPLETED", candidates=supports)

    def health_check(self) -> dict[str, str]:
        """检查 Neo4j Repository。"""

        try:
            return self.repository.health_check()
        except Exception as exc:
            return {"status": "unavailable", "reason": exc.__class__.__name__}


_repository_cache: dict[tuple[str, str, str, str, int], Neo4jGraphRepository] = {}
_repository_cache_lock = Lock()


def build_graph_classification_context(settings: Any) -> GraphClassificationContext:
    """根据配置构造图谱上下文，缺少部署条件时关闭式降级。"""

    if not settings.graph_classification_enabled:
        return NoOpGraphClassificationContext()
    if not settings.neo4j_uri or not settings.neo4j_username or not settings.neo4j_password:
        return UnavailableGraphClassificationContext(reason="GRAPH_CONFIGURATION_MISSING")
    try:
        repository = get_graph_repository(settings)
    except Exception as exc:
        log_event(
            "classification.graph_context_loaded",
            level="WARNING",
            status="DEGRADED",
            error_code=exc.__class__.__name__,
            message="图谱运行时依赖不可用，已回退基础分类。",
        )
        return UnavailableGraphClassificationContext(reason="GRAPH_DEPENDENCY_UNAVAILABLE")
    log_event(
        "classification.graph_context_loaded",
        status="COMPLETED",
        message="图谱分类上下文已启用",
    )
    return Neo4jGraphClassificationContext(
        repository=repository,
        max_hops=settings.graph_classification_max_hops,
    )


def close_graph_resources() -> None:
    """关闭进程级 Neo4j Driver，并清空缓存。"""

    with _repository_cache_lock:
        repositories = list(_repository_cache.values())
        _repository_cache.clear()
    for repository in repositories:
        repository.close()


def get_graph_repository(settings: Any) -> Neo4jGraphRepository:
    """复用线程安全 Driver，避免每个 AgentRun 重建连接池。"""

    key = (
        settings.neo4j_uri,
        settings.neo4j_username,
        settings.neo4j_password,
        settings.neo4j_database,
        settings.neo4j_query_timeout_seconds,
    )
    with _repository_cache_lock:
        repository = _repository_cache.get(key)
        if repository is None:
            repository = Neo4jGraphRepository.connect(
                uri=settings.neo4j_uri,
                username=settings.neo4j_username,
                password=settings.neo4j_password,
                database=settings.neo4j_database,
                timeout_seconds=settings.neo4j_query_timeout_seconds,
            )
            _repository_cache[key] = repository
    return repository
