"""Neo4j Repository 参数化查询测试。"""

from app.modules.knowledge_graph.neo4j_repository import Neo4jGraphRepository
from app.modules.knowledge_graph.schemas import CategoryProjection, GraphCandidateSeed


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
