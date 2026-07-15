"""Neo4j Repository 参数化查询测试。"""

import pytest

from app.modules.knowledge_graph.neo4j_repository import Neo4jGraphRepository
from app.modules.knowledge_graph.schemas import (
    CategoryProjection,
    DocumentEmbeddingProjection,
    FolderCategoryRelationProjection,
    GraphCandidateSeed,
    ManagedFolderProjection,
    ManagedRootProjection,
    SuggestedClassificationProjection,
)


class RecordingResult:
    """可迭代的 Neo4j 测试结果。"""

    def __init__(self, rows=None) -> None:
        self.rows = rows or []

    def __iter__(self):
        return iter(self.rows)


class RecordingSession:
    """记录固定查询和独立参数。"""

    def __init__(self, calls, rows=None) -> None:
        self.calls = calls
        self.rows = rows or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def run(self, query, *, parameters):
        self.calls.append({"query": str(query), "parameters": parameters})
        return RecordingResult(self.rows)


class RecordingDriver:
    """提供测试 Session 的 Driver。"""

    def __init__(self, rows=None) -> None:
        self.calls = []
        self.rows = rows or []
        self.closed = False

    def session(self, *, database):
        self.database = database
        return RecordingSession(self.calls, self.rows)

    def close(self):
        self.closed = True


def test_upsert_categories_keeps_values_out_of_cypher_text():
    """分类名称即使包含 Cypher 片段，也只能作为参数传入。"""

    driver = RecordingDriver()
    repository = Neo4jGraphRepository(driver=driver, database="file_agent", timeout_seconds=4)
    unsafe_name = "制度'}) DETACH DELETE node //"

    repository.upsert_categories(
        categories=[
            CategoryProjection(
                graph_key="taxonomy:v1:rules",
                category_id="rules",
                taxonomy_key="taxonomy",
                taxonomy_version="v1",
                name=unsafe_name,
                path=[unsafe_name],
            )
        ],
        relations=[],
    )

    call = driver.calls[0]
    assert unsafe_name not in call["query"]
    assert call["parameters"]["rows"][0]["name"] == unsafe_name
    assert repository.timeout_seconds == 4
    assert driver.database == "file_agent"


def test_expand_candidates_uses_bounded_parameters_and_maps_support():
    """候选扩展必须限制跳数和数量，并返回结构化支持。"""

    driver = RecordingDriver(
        rows=[
            {
                "seed_category_id": "rules",
                "graph_key": "taxonomy:v1:rules",
                "category_id": "rules",
                "category_path": ["学校", "规章制度"],
                "taxonomy_key": "taxonomy",
                "taxonomy_version": "v1",
                "name": "规章制度",
                "relation_type": "EXACT",
                "hops": 0,
                "support_count": 3,
            }
        ]
    )
    repository = Neo4jGraphRepository(driver=driver)

    supports = repository.expand_candidates(
        candidates=[
            GraphCandidateSeed(
                category_id="rules",
                graph_key="taxonomy:v1:rules",
                category_path=("学校", "规章制度"),
                taxonomy_key="taxonomy",
                taxonomy_version="v1",
                rule_score=0.8,
            )
        ],
        max_hops=20,
        limit=500,
    )

    assert driver.calls[0]["parameters"]["max_hops"] == 2
    assert driver.calls[0]["parameters"]["limit"] == 50
    assert supports[0].category_id == "rules"
    assert supports[0].confirmed_support_score == 0.6
    assert supports[0].paths[0]["type"] == "EXACT"


def test_vector_index_rejects_unsafe_identifier_and_embedding_is_parameterized():
    """向量索引名必须受控，向量数值只能通过参数写入。"""

    driver = RecordingDriver()
    repository = Neo4jGraphRepository(driver=driver)

    with pytest.raises(ValueError, match="索引名"):
        repository.ensure_vector_index(index_name="index`) MATCH (n) DELETE n", dimension=384)

    repository.ensure_vector_index(index_name="document_embedding_v1", dimension=4)
    repository.upsert_document_embeddings(
        projections=[
            DocumentEmbeddingProjection(
                document_version_id="document-1",
                document_id="document-1",
                sha256="a" * 64,
                filename="材料.docx",
                embedding=(0.1, 0.2, 0.3, 0.4),
                embedding_model="local-model",
                embedding_version="v1",
                embedding_dimension=4,
                successful_chunks=2,
                failed_chunks=0,
            )
        ]
    )

    assert "document_embedding_v1" in driver.calls[0]["query"]
    assert driver.calls[1]["parameters"]["rows"][0]["embedding"] == [0.1, 0.2, 0.3, 0.4]


def test_replace_suggested_classifications_uses_multiple_parameterized_relations():
    """一个文件的多条分类建议必须分别投影，且值不能拼入 Cypher。"""

    driver = RecordingDriver()
    repository = Neo4jGraphRepository(driver=driver)
    repository.replace_suggested_classifications(
        relations=[
            SuggestedClassificationProjection(
                document_version_id="document-1",
                category_graph_key="managed:v1:category-a",
                suggestion_id="suggestion-a",
                confidence=0.82,
                status="SUGGESTED",
                source="managed_global_catalog",
            ),
            SuggestedClassificationProjection(
                document_version_id="document-1",
                category_graph_key="managed:v1:category-b",
                suggestion_id="suggestion-b",
                confidence=0.76,
                status="SUGGESTED",
                source="managed_global_catalog",
            ),
        ]
    )

    assert "DELETE relation" in driver.calls[0]["query"]
    assert len(driver.calls[1]["parameters"]["rows"]) == 2
    assert "suggestion-a" not in driver.calls[1]["query"]


def test_upsert_managed_hierarchy_rebuilds_folder_category_mappings():
    """当前 Profile 投影前必须清理旧目录分类映射。"""

    driver = RecordingDriver()
    repository = Neo4jGraphRepository(driver=driver)

    repository.upsert_managed_hierarchy(
        roots=[
            ManagedRootProjection(
                root_key="classified_library",
                display_name="分类资料库",
                classification_mode="PATH_AS_CATEGORY",
            )
        ],
        folders=[
            ManagedFolderProjection(
                graph_key="classified_library:personnel",
                root_key="classified_library",
                relative_path="人事处",
                name="人事处",
                depth=1,
                classification_mode="PATH_AS_CATEGORY",
            )
        ],
        relations=[],
        folder_category_relations=[
            FolderCategoryRelationProjection(
                folder_graph_key="classified_library:personnel",
                category_graph_key="managed:v1:personnel",
                source_type="managed_path_category_source",
            )
        ],
    )

    assert "DELETE relation" in driver.calls[0]["query"]
    assert "MAPS_TO" in driver.calls[0]["query"]
    assert "MERGE (folder)-[relation:MAPS_TO]" in driver.calls[-1]["query"]
