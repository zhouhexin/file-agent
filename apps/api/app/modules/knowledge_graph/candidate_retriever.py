"""分类建议与图谱候选协议之间的适配。"""

from __future__ import annotations

from app.modules.knowledge_graph.classification_context import GraphClassificationContext
from app.modules.knowledge_graph.schemas import (
    GraphCandidateSeed,
    GraphClassificationResult,
    category_graph_key,
)


def build_graph_candidate_seeds(categories: list[dict]) -> list[GraphCandidateSeed]:
    """从现有分类候选提取图谱查询种子，不传递正文。"""

    seeds: list[GraphCandidateSeed] = []
    for category in categories:
        category_id = str(category.get("category_id") or "")
        taxonomy_key = str(category.get("taxonomy_key") or "")
        taxonomy_version = str(category.get("taxonomy_version") or "")
        if not category_id or not taxonomy_key or not taxonomy_version:
            continue
        seeds.append(
            GraphCandidateSeed(
                category_id=category_id,
                graph_key=category_graph_key(
                    taxonomy_key=taxonomy_key,
                    taxonomy_version=taxonomy_version,
                    category_id=category_id,
                ),
                category_path=tuple(str(item) for item in category.get("category_path") or []),
                taxonomy_key=taxonomy_key,
                taxonomy_version=taxonomy_version,
                rule_score=_rule_score(category),
                negative_signals=tuple(str(item) for item in category.get("negative_signals") or []),
            )
        )
    return seeds


def retrieve_graph_candidates(
    *,
    context: GraphClassificationContext,
    categories: list[dict],
    document_id: str,
    document_version_id: str | None,
    limit: int,
) -> GraphClassificationResult:
    """调用图谱上下文查询候选扩展。"""

    seeds = build_graph_candidate_seeds(categories)
    if not seeds:
        return GraphClassificationResult(status="COMPLETED")
    return context.expand_candidates(
        candidates=seeds,
        document_id=document_id,
        document_version_id=document_version_id,
        limit=limit,
    )


def _rule_score(category: dict) -> float:
    """读取候选规则分数，并兼容尚未携带分量的旧结果。"""

    if category.get("rule_score") is not None:
        return _clamp(category.get("rule_score"))
    confidence = _clamp(category.get("confidence"))
    return _clamp((confidence - 0.45) / 0.5)


def _clamp(value) -> float:
    """把分数限制在零到一。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
