"""分类服务使用的文档语义召回上下文。"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any, Protocol

from app.core.logging import log_event
from app.modules.knowledge_graph.embedding import (
    DocumentEmbeddingService,
    LocalSentenceTransformerProvider,
)
from app.modules.knowledge_graph.graphrag_adapter import (
    GraphRAGSemanticRetriever,
    create_document_vector_retriever,
)
from app.modules.knowledge_graph.repository import GraphRepository
from app.modules.knowledge_graph.schemas import GraphSemanticResult


class SemanticClassificationContext(Protocol):
    """完整正文语义召回的运行时协议。"""

    def retrieve(
        self,
        *,
        document_id: str,
        document_version_id: str,
        sha256: str,
        filename: str,
        full_text: str,
        limit: int,
    ) -> GraphSemanticResult:
        """生成当前文档向量并召回相似分类。"""


@dataclass(slots=True)
class NoOpSemanticClassificationContext:
    """语义分类关闭时保持现有行为。"""

    reason: str = "GRAPH_EMBEDDING_DISABLED"

    def retrieve(self, **kwargs: Any) -> GraphSemanticResult:
        return GraphSemanticResult(status="DISABLED", warnings=[self.reason])


class Neo4jSemanticClassificationContext:
    """本地 Embedding 与 GraphRAG Retriever 的组合运行时。"""

    def __init__(
        self,
        *,
        embedding_service: DocumentEmbeddingService,
        retriever: GraphRAGSemanticRetriever,
        top_k: int,
    ) -> None:
        self.embedding_service = embedding_service
        self.retriever = retriever
        self.top_k = max(1, min(50, top_k))

    def retrieve(
        self,
        *,
        document_id: str,
        document_version_id: str,
        sha256: str,
        filename: str,
        full_text: str,
        limit: int,
    ) -> GraphSemanticResult:
        """分块生成向量并查询相似已确认分类，失败时结构化降级。"""

        start = time.perf_counter()
        try:
            embedding = self.embedding_service.embed_document(
                document_id=document_id,
                document_version_id=document_version_id,
                sha256=sha256,
                filename=filename,
                full_text=full_text,
            )
            if embedding.status != "COMPLETED" or not embedding.vector:
                return GraphSemanticResult(
                    status="DEGRADED",
                    warnings=[embedding.warning or "EMBEDDING_UNAVAILABLE"],
                )
            supports = self.retriever.search(
                query_vector=list(embedding.vector),
                current_document_version_id=document_version_id,
                current_sha256=sha256,
                top_k=max(limit, self.top_k),
            )
        except Exception as exc:
            log_event(
                "graph.semantic_retrieval.degraded",
                level="WARNING",
                document_id=document_id,
                status="DEGRADED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message="语义分类召回失败，已回退基础分类",
            )
            return GraphSemanticResult(status="DEGRADED", warnings=["SEMANTIC_RETRIEVAL_UNAVAILABLE"])
        log_event(
            "graph.semantic_retrieval.completed",
            document_id=document_id,
            status="COMPLETED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="语义分类召回完成",
            candidate_count=len(supports),
        )
        return GraphSemanticResult(status="COMPLETED", candidates=supports)


def build_semantic_classification_context(
    *,
    settings: Any,
    repository: GraphRepository | None,
) -> SemanticClassificationContext:
    """根据开关构造语义上下文；配置不完整时安全关闭。"""

    if (
        repository is None
        or not settings.graph_classification_enabled
        or not settings.graph_embedding_enabled
        or settings.graph_classification_mode == "off"
    ):
        return NoOpSemanticClassificationContext()
    if settings.graph_embedding_provider != "local":
        return NoOpSemanticClassificationContext(reason="UNSUPPORTED_EMBEDDING_PROVIDER")
    provider = LocalSentenceTransformerProvider(
        model_path=settings.graph_embedding_model_path,
        model_name=settings.graph_embedding_model_name,
        dimension=settings.graph_embedding_dimension,
    )
    repository.ensure_vector_index(
        index_name=settings.graph_vector_index_name,
        dimension=settings.graph_embedding_dimension,
    )
    vector_retriever = create_document_vector_retriever(
        driver=repository.get_driver(),
        index_name=settings.graph_vector_index_name,
        database=settings.neo4j_database,
    )
    return Neo4jSemanticClassificationContext(
        embedding_service=DocumentEmbeddingService(
            repository=repository,
            provider=provider,
            embedding_version=settings.graph_embedding_version,
        ),
        retriever=GraphRAGSemanticRetriever(
            retriever=vector_retriever,
            embedding_version=settings.graph_embedding_version,
            min_score=settings.graph_vector_min_score,
        ),
        top_k=settings.graph_vector_top_k,
    )
