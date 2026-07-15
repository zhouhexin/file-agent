"""规则候选与图谱支持的确定性重排器。"""

from __future__ import annotations

from copy import deepcopy

from app.modules.knowledge_graph.candidate_retriever import _rule_score
from app.modules.knowledge_graph.schemas import GraphCandidateSupport, GraphClassificationResult


class GraphClassificationReranker:
    """合并规则、图谱和可信样本分数，不生成最终置信度。"""

    def rerank(
        self,
        *,
        categories: list[dict],
        graph_result: GraphClassificationResult,
        limit: int = 8,
    ) -> list[dict]:
        """重排候选；图谱关闭或降级时原样返回。"""

        if graph_result.status != "COMPLETED" or not graph_result.candidates:
            return categories

        supports_by_id = {
            item.category_id: item
            for item in graph_result.candidates
            if item.category_id
        }
        reranked: list[tuple[bool, float, int, dict]] = []
        existing_ids: set[str] = set()
        for order, original in enumerate(categories):
            category = deepcopy(original)
            category_id = str(category.get("category_id") or "")
            if category_id:
                existing_ids.add(category_id)
            support = supports_by_id.get(category_id)
            if support is None:
                reranked.append((True, _rule_score(category) * 0.65, order, category))
                continue
            scored = _apply_support(category=category, support=support)
            reranked.append((True, scored["candidate_scores"]["combined"], order, scored))

        graph_only_order = len(reranked)
        for support in graph_result.candidates:
            if not support.category_id or support.category_id in existing_ids:
                continue
            category = _graph_only_category(support)
            reranked.append((False, category["candidate_scores"]["combined"], graph_only_order, category))
            graph_only_order += 1

        # 有正文规则信号的候选始终排在纯图扩展候选之前，防止关系传播替代内容证据。
        reranked.sort(key=lambda item: (not item[0], -item[1], item[2]))
        return [item[3] for item in reranked[: max(1, min(20, limit))]]


def _apply_support(*, category: dict, support: GraphCandidateSupport) -> dict:
    """把图谱支持分量附加到已有规则候选。"""

    rule_score = _rule_score(category)
    graph_score = _clamp(support.graph_score)
    confirmed_score = _clamp(support.confirmed_support_score)
    negative_penalty = min(0.25, len(category.get("negative_signals") or []) * 0.08)
    combined = _combined_score(
        rule_score=rule_score,
        graph_score=graph_score,
        confirmed_score=confirmed_score,
        negative_penalty=negative_penalty,
    )
    category["candidate_scores"] = {
        "rule": round(rule_score, 4),
        "graph": round(graph_score, 4),
        "confirmed_support": round(confirmed_score, 4),
        "negative_penalty": round(negative_penalty, 4),
        "combined": combined,
    }
    category["graph_evidence"] = list(support.paths)
    return category


def _graph_only_category(support: GraphCandidateSupport) -> dict:
    """创建待复核的图扩展候选，它不能排到规则候选之前。"""

    graph_score = _clamp(support.graph_score)
    confirmed_score = _clamp(support.confirmed_support_score)
    combined = _combined_score(
        rule_score=0.0,
        graph_score=graph_score,
        confirmed_score=confirmed_score,
        negative_penalty=0.0,
    )
    path = list(support.category_path)
    return {
        "name": support.name or "/".join(path),
        "category_id": support.category_id,
        "category_path": path,
        "confidence": 0.2,
        "status": "NEEDS_REVIEW",
        "source": "graph",
        "evidence": [],
        "taxonomy_key": support.taxonomy_key,
        "taxonomy_version": support.taxonomy_version,
        "candidate_scores": {
            "rule": 0.0,
            "graph": round(graph_score, 4),
            "confirmed_support": round(confirmed_score, 4),
            "negative_penalty": 0.0,
            "combined": combined,
        },
        "graph_evidence": list(support.paths),
    }


def _combined_score(
    *,
    rule_score: float,
    graph_score: float,
    confirmed_score: float,
    negative_penalty: float,
) -> float:
    """按第一版本固定权重计算仅用于排序的候选分数。"""

    score = rule_score * 0.65 + graph_score * 0.20 + confirmed_score * 0.15 - negative_penalty
    return round(max(0.0, min(1.0, score)), 4)


def _clamp(value) -> float:
    """把外部图谱分数限制在零到一。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
