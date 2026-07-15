"""图谱分类候选重排测试。"""

from app.modules.knowledge_graph.reranker import GraphClassificationReranker
from app.modules.knowledge_graph.schemas import (
    GraphCandidateSupport,
    GraphClassificationResult,
    GraphSemanticResult,
    SemanticCategorySupport,
)


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
        "semantic": 0.0,
        "graph": 1.0,
        "confirmed_support": 1.0,
        "negative_penalty": 0.0,
        "combined": 0.52,
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


def test_semantic_support_adds_score_without_exposing_source_document():
    """语义支持只附加聚合信息，不返回相似文件身份。"""

    categories = [_category(category_id="category-a", name="学校/分类A", rule_score=0.6)]
    semantic_result = GraphSemanticResult(
        status="COMPLETED",
        candidates=[
            SemanticCategorySupport(
                category_id="category-a",
                graph_key="school_file_classification:2026-06-v2:category-a",
                category_path=["学校", "分类A"],
                semantic_score=0.9,
                support_count=3,
                taxonomy_key="school_file_classification",
                taxonomy_version="2026-06-v2",
            )
        ],
    )

    reranked = GraphClassificationReranker().rerank(
        categories=categories,
        graph_result=GraphClassificationResult(status="DISABLED"),
        semantic_result=semantic_result,
    )

    assert reranked[0]["candidate_scores"]["semantic"] == 0.9
    assert reranked[0]["semantic_evidence"] == {
        "support_count": 3,
        "similarity_bucket": "high",
        "source": "confirmed_history",
    }
    assert "document_id" not in reranked[0]["semantic_evidence"]


def test_semantic_support_cannot_mix_another_taxonomy_into_managed_catalog():
    """启用受管全局目录后，语义召回不能混入预置业务 taxonomy。"""

    categories = [
        {
            **_category(category_id="managed.global.a", name="人事处/职称评定", rule_score=0.6),
            "taxonomy_key": "managed_global_categories",
            "taxonomy_version": "managed-global-v1",
        }
    ]
    semantic_result = GraphSemanticResult(
        status="COMPLETED",
        candidates=[
            SemanticCategorySupport(
                category_id="preset-category",
                graph_key="school_file_classification:2026-06-v2:preset-category",
                category_path=["学校", "行政管理"],
                semantic_score=0.95,
                support_count=10,
                taxonomy_key="school_file_classification",
                taxonomy_version="2026-06-v2",
            )
        ],
    )

    reranked = GraphClassificationReranker().rerank(
        categories=categories,
        graph_result=GraphClassificationResult(status="DISABLED"),
        semantic_result=semantic_result,
    )

    assert [item["category_id"] for item in reranked] == ["managed.global.a"]
    assert "candidate_scores" not in reranked[0]
