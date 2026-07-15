"""`neo4j-graphrag-python` 的受控向量分类适配边界。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.modules.knowledge_graph.schemas import SemanticCategorySupport

try:
    from neo4j_graphrag.retrievers import VectorCypherRetriever
    from neo4j_graphrag.types import RetrieverResultItem
except ImportError:  # pragma: no cover - 本地未安装 graph extras 时由 no-op 路径覆盖。
    VectorCypherRetriever = None
    RetrieverResultItem = None


DOCUMENT_VECTOR_RETRIEVAL_QUERY = """
OPTIONAL MATCH (node)-[relation:CONFIRMED_AS|PATH_SUGGESTS]->(category:Category)
WITH node, score, collect(CASE WHEN category IS NULL THEN NULL ELSE {
    category_id: category.category_id,
    graph_key: category.graph_key,
    category_path: category.path,
    taxonomy_key: category.taxonomy_key,
    taxonomy_version: category.taxonomy_version,
    name: category.name,
    relation_type: type(relation)
} END) AS raw_classifications
RETURN node.document_version_id AS document_version_id,
       node.sha256 AS sha256,
       node.embedding_version AS embedding_version,
       score AS score,
       [item IN raw_classifications WHERE item IS NOT NULL | item] AS classifications
"""


class GraphRAGDependencyError(RuntimeError):
    """启用 GraphRAG Retriever 但 optional dependency 不可用。"""


def graphrag_capability_status() -> str:
    """返回 GraphRAG 包是否已安装，不触发任何外部连接。"""

    return "available" if VectorCypherRetriever is not None else "not_installed"


def create_document_vector_retriever(
    *,
    driver: Any,
    index_name: str,
    database: str = "neo4j",
) -> Any:
    """使用固定图遍历模板构造文档分类 Retriever。"""

    if VectorCypherRetriever is None or RetrieverResultItem is None:
        raise GraphRAGDependencyError("未安装 neo4j-graphrag optional dependency。")
    return VectorCypherRetriever(
        driver=driver,
        index_name=index_name,
        retrieval_query=DOCUMENT_VECTOR_RETRIEVAL_QUERY,
        result_formatter=_result_formatter,
        neo4j_database=database,
    )


class GraphRAGSemanticRetriever:
    """把相似文档结果聚合为不泄露来源身份的分类支持。"""

    def __init__(self, *, retriever: Any, embedding_version: str, min_score: float = 0.0) -> None:
        self.retriever = retriever
        self.embedding_version = embedding_version
        self.min_score = max(0.0, min(1.0, min_score))

    def search(
        self,
        *,
        query_vector: list[float],
        current_document_version_id: str,
        current_sha256: str,
        top_k: int,
    ) -> list[SemanticCategorySupport]:
        """执行固定向量查询，并按分类聚合可信支持。"""

        raw_result = self.retriever.search(
            query_vector=query_vector,
            top_k=max(1, min(50, top_k)),
        )
        grouped: dict[str, list[tuple[float, dict[str, Any]]]] = defaultdict(list)
        for item in getattr(raw_result, "items", []) or []:
            metadata = dict(getattr(item, "metadata", None) or {})
            if str(metadata.get("document_version_id") or "") == current_document_version_id:
                continue
            if current_sha256 and str(metadata.get("sha256") or "") == current_sha256:
                continue
            if str(metadata.get("embedding_version") or "") != self.embedding_version:
                continue
            score = _clamp(metadata.get("score"))
            if score < self.min_score:
                continue
            for classification in metadata.get("classifications") or []:
                relation_type = str(classification.get("relation_type") or "")
                if relation_type not in {"CONFIRMED_AS", "PATH_SUGGESTS"}:
                    continue
                category_id = str(classification.get("category_id") or "")
                if category_id:
                    grouped[category_id].append((score, dict(classification)))

        supports: list[SemanticCategorySupport] = []
        for category_id, matches in grouped.items():
            strongest = sorted(matches, key=lambda value: value[0], reverse=True)[:5]
            best_score, category = strongest[0]
            confirmed_count = sum(
                1 for _, value in strongest if value.get("relation_type") == "CONFIRMED_AS"
            )
            weak_count = len(strongest) - confirmed_count
            balanced_score = min(
                1.0,
                best_score * 0.75 + min(0.2, confirmed_count * 0.05) + min(0.05, weak_count * 0.01),
            )
            supports.append(
                SemanticCategorySupport(
                    category_id=category_id,
                    graph_key=str(category.get("graph_key") or ""),
                    category_path=[str(value) for value in category.get("category_path") or []],
                    taxonomy_key=str(category.get("taxonomy_key") or ""),
                    taxonomy_version=str(category.get("taxonomy_version") or ""),
                    name=str(category.get("name") or ""),
                    semantic_score=round(balanced_score, 4),
                    support_count=len(strongest),
                    source="confirmed_history" if confirmed_count else "managed_path_weak",
                )
            )
        return sorted(supports, key=lambda item: (-item.semantic_score, item.category_id))


def _result_formatter(record: Any) -> Any:
    """只返回分类召回所需元数据，不暴露文件名和正文。"""

    return RetrieverResultItem(
        content="",
        metadata={
            "document_version_id": record.get("document_version_id"),
            "sha256": record.get("sha256"),
            "embedding_version": record.get("embedding_version"),
            "score": record.get("score"),
            "classifications": record.get("classifications") or [],
        },
    )


def _clamp(value: Any) -> float:
    """把 Retriever 分数限制为零到一。"""

    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0
