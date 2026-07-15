"""`neo4j-graphrag-python` 的受控适配边界。"""

from __future__ import annotations

from typing import Any

try:
    from neo4j_graphrag.retrievers import VectorCypherRetriever
except ImportError:  # pragma: no cover - 本地未安装 graph extras 时由 no-op 路径覆盖。
    VectorCypherRetriever = None


DOCUMENT_VECTOR_RETRIEVAL_QUERY = """
RETURN node.document_version_id AS document_version_id,
       node.document_id AS document_id,
       node.filename AS filename,
       score AS score
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
    embedder: Any,
) -> Any:
    """使用固定检索模板构造文档向量 Retriever，供第二阶段启用。"""

    if VectorCypherRetriever is None:
        raise GraphRAGDependencyError("未安装 neo4j-graphrag optional dependency。")
    return VectorCypherRetriever(
        driver=driver,
        index_name=index_name,
        embedder=embedder,
        retrieval_query=DOCUMENT_VECTOR_RETRIEVAL_QUERY,
    )
