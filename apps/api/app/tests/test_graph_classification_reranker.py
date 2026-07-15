"""图谱分类候选重排测试。"""

from app.modules.knowledge_graph.reranker import GraphClassificationReranker
from app.modules.knowledge_graph.schemas import GraphCandidateSupport, GraphClassificationResult


def _category(*, category_id: str, name: str, rule_score: float) -> dict:
    """构造最小规则候选。"""

    return {
        "category_id": category_id,
        "name": name,
        "category_path": name.split("/"),
        "rule_score": rule_score,
        "confidence": 0.7,
        "status": "SUGGESTED",
        "source": "rule",
        "evidence": [name.split("/")[-1]],
        "negative_signals": [],
        "taxonomy_key": "school_file_classification",
        "taxonomy_version": "2026-06-v2",
    }


def test_graph_support_can_rerank_existing_rule_candidates():
    """可信图谱支持应能重排已有候选，同时保留各分量分数。"""

    categories = [
        _category(category_id="category-a", name="学校/分类A", rule_score=0.8),
        _category(category_id="category-b", name="学校/分类B", rule_score=0.6),
    ]
    graph_result = GraphClassificationResult(
        status="COMPLETED",
        candidates=[
            GraphCandidateSupport(
                category_id="category-b",
                graph_key="school_file_classification:2026-06-v2:category-b",
                category_path=["学校", "分类B"],
                graph_score=1.0,
                confirmed_support_score=1.0,
                support_count=4,
                paths=[{"type": "CONFIRMED_NEIGHBOR", "support_count": 4}],
            )
        ],
    )

    reranked = GraphClassificationReranker().rerank(categories=categories, graph_result=graph_result)

    assert reranked[0]["category_id"] == "category-b"
    assert reranked[0]["candidate_scores"] == {
        "rule": 0.6,
        "graph": 1.0,
        "confirmed_support": 1.0,
        "negative_penalty": 0.0,
        "combined": 0.74,
    }
    assert reranked[0]["graph_evidence"][0]["support_count"] == 4


def test_graph_only_candidate_cannot_outrank_rule_candidate():
    """没有正文规则信号的图谱扩展候选不得直接成为首选分类。"""

    categories = [_category(category_id="category-a", name="学校/分类A", rule_score=0.2)]
    graph_result = GraphClassificationResult(
        status="COMPLETED",
        candidates=[
            GraphCandidateSupport(
                category_id="category-c",
                graph_key="school_file_classification:2026-06-v2:category-c",
                category_path=["学校", "分类C"],
                graph_score=1.0,
                confirmed_support_score=1.0,
                support_count=20,
                paths=[{"type": "PARENT", "support_count": 20}],
            )
        ],
    )

    reranked = GraphClassificationReranker().rerank(categories=categories, graph_result=graph_result)

    assert reranked[0]["category_id"] == "category-a"
    assert reranked[1]["category_id"] == "category-c"
    assert reranked[1]["status"] == "NEEDS_REVIEW"
    assert reranked[1]["source"] == "graph"


def test_degraded_graph_result_keeps_rule_order():
    """图谱降级时必须保持现有规则候选顺序。"""

    categories = [
        _category(category_id="category-a", name="学校/分类A", rule_score=0.8),
        _category(category_id="category-b", name="学校/分类B", rule_score=0.6),
    ]

    reranked = GraphClassificationReranker().rerank(
        categories=categories,
        graph_result=GraphClassificationResult(status="DEGRADED", warnings=["GRAPH_UNAVAILABLE"]),
    )

    assert [item["category_id"] for item in reranked] == ["category-a", "category-b"]
    assert all("candidate_scores" not in item for item in reranked)
