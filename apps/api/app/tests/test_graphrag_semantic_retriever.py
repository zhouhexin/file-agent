"""GraphRAG 相似分类聚合与脱敏测试。"""

from types import SimpleNamespace

from app.modules.knowledge_graph.graphrag_adapter import (
    DOCUMENT_VECTOR_RETRIEVAL_QUERY,
    GraphRAGSemanticRetriever,
)


class FakeVectorRetriever:
    """返回预置 GraphRAG items。"""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(items=self.items)


def _item(**metadata):
    return SimpleNamespace(metadata=metadata)


def test_semantic_retriever_excludes_current_and_duplicate_content_and_hides_source_identity():
    """当前版本、相同哈希和来源文件身份不得进入分类支持。"""

    classification = {
        "category_id": "school.hr.title-review",
        "graph_key": "taxonomy:v2:school.hr.title-review",
        "category_path": ["学校", "人事师资", "职称"],
        "taxonomy_key": "taxonomy",
        "taxonomy_version": "v2",
        "name": "职称",
        "relation_type": "CONFIRMED_AS",
    }
    retriever = FakeVectorRetriever(
        [
            _item(
                document_version_id="current",
                sha256="current-sha",
                embedding_version="v2",
                score=0.99,
                classifications=[classification],
                filename="不应返回.docx",
            ),
            _item(
                document_version_id="duplicate",
                sha256="current-sha",
                embedding_version="v2",
                score=0.95,
                classifications=[classification],
            ),
            _item(
                document_version_id="supporting",
                sha256="other-sha",
                embedding_version="v2",
                score=0.86,
                classifications=[classification],
                filename="其他用户文件.docx",
            ),
        ]
    )

    supports = GraphRAGSemanticRetriever(
        retriever=retriever,
        embedding_version="v2",
        min_score=0.5,
    ).search(
        query_vector=[0.1, 0.2],
        current_document_version_id="current",
        current_sha256="current-sha",
        top_k=10,
    )

    assert len(supports) == 1
    assert supports[0].category_id == "school.hr.title-review"
    assert supports[0].support_count == 1
    assert not hasattr(supports[0], "filename")
    assert retriever.calls[0]["query_vector"] == [0.1, 0.2]


def test_vector_retrieval_query_accepts_empty_classification_relationship_schema():
    """新图谱没有分类关系时，查询不能静态引用尚不存在的关系类型。"""

    assert "[relation:CONFIRMED_AS|PATH_SUGGESTS]" not in DOCUMENT_VECTOR_RETRIEVAL_QUERY
    assert "OPTIONAL MATCH (node)-[relation]->(category:Category)" in DOCUMENT_VECTOR_RETRIEVAL_QUERY
    assert "type(relation) IN ['CONFIRMED_AS', 'PATH_SUGGESTS']" in DOCUMENT_VECTOR_RETRIEVAL_QUERY
