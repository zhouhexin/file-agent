"""规则候选与图谱支持的确定性重排器。"""

from __future__ import annotations

from copy import deepcopy

from app.modules.knowledge_graph.candidate_retriever import _rule_score
from app.modules.knowledge_graph.schemas import (
    GraphCandidateSupport,
    GraphClassificationResult,
    GraphSemanticResult,
    SemanticCategorySupport,
)


class GraphClassificationReranker:
    """合并规则、图谱和可信样本分数，不生成最终置信度。"""

    def rerank(
        self,
        *,
        categories: list[dict],
        graph_result: GraphClassificationResult,
        semantic_result: GraphSemanticResult | None = None,
        limit: int = 8,
    ) -> list[dict]:
        """合并规则、语义与图谱分量；全部关闭时原样返回。"""

        semantic_result = semantic_result or GraphSemanticResult(status="DISABLED")
        allowed_taxonomies = {
            (
                str(category.get("taxonomy_key") or ""),
                str(category.get("taxonomy_version") or ""),
            )
            for category in categories
            if category.get("taxonomy_key") and category.get("taxonomy_version")
        }
        graph_candidates = (
            [
                candidate
                for candidate in graph_result.candidates
                if _belongs_to_active_taxonomy(candidate, allowed_taxonomies)
            ]
            if graph_result.status == "COMPLETED"
            else []
        )
        semantic_candidates = (
            [
                candidate
                for candidate in semantic_result.candidates
                if _belongs_to_active_taxonomy(candidate, allowed_taxonomies)
            ]
            if semantic_result.status == "COMPLETED"
            else []
        )
        if not graph_candidates and not semantic_candidates:
            return categories

        supports_by_id = {
            item.category_id: item
            for item in graph_candidates
            if item.category_id
        }
        semantic_by_id = {
            item.category_id: item
            for item in semantic_candidates
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
            semantic_support = semantic_by_id.get(category_id)
            scored = _apply_support(
                category=category,
                support=support,
                semantic_support=semantic_support,
            )
            reranked.append((True, scored["candidate_scores"]["combined"], order, scored))

        graph_only_order = len(reranked)
        all_extra_ids = {
            item.category_id for item in [*graph_candidates, *semantic_candidates] if item.category_id
        }
        for category_id in sorted(all_extra_ids):
            if category_id in existing_ids:
                continue
            category = _graph_only_category(
                support=supports_by_id.get(category_id),
                semantic_support=semantic_by_id.get(category_id),
            )
            reranked.append((False, category["candidate_scores"]["combined"], graph_only_order, category))
            graph_only_order += 1

        # 有正文规则信号的候选始终排在纯图扩展候选之前，防止关系传播替代内容证据。
        reranked.sort(key=lambda item: (not item[0], -item[1], item[2]))
        return [item[3] for item in reranked[: max(1, min(20, limit))]]


def _apply_support(
    *,
    category: dict,
    support: GraphCandidateSupport | None,
    semantic_support: SemanticCategorySupport | None,
) -> dict:
    """把语义和图谱支持分量附加到已有规则候选。"""

    rule_score = _rule_score(category)
    semantic_score = _clamp(semantic_support.semantic_score if semantic_support else 0.0)
    graph_score = _clamp(support.graph_score if support else 0.0)
    confirmed_score = _clamp(support.confirmed_support_score if support else 0.0)
    negative_penalty = min(0.25, len(category.get("negative_signals") or []) * 0.08)
    combined = _combined_score(
        rule_score=rule_score,
        semantic_score=semantic_score,
        graph_score=graph_score,
        confirmed_score=confirmed_score,
        negative_penalty=negative_penalty,
    )
    category["candidate_scores"] = {
        "rule": round(rule_score, 4),
        "semantic": round(semantic_score, 4),
        "graph": round(graph_score, 4),
        "confirmed_support": round(confirmed_score, 4),
        "negative_penalty": round(negative_penalty, 4),
        "combined": combined,
    }
    category["graph_evidence"] = list(support.paths) if support else []
    category["semantic_evidence"] = _semantic_evidence(semantic_support)
    return category


def _graph_only_category(
    *,
    support: GraphCandidateSupport | None,
    semantic_support: SemanticCategorySupport | None,
) -> dict:
    """创建待复核的图扩展候选，它不能排到规则候选之前。"""

    semantic_score = _clamp(semantic_support.semantic_score if semantic_support else 0.0)
    graph_score = _clamp(support.graph_score if support else 0.0)
    confirmed_score = _clamp(support.confirmed_support_score if support else 0.0)
    combined = _combined_score(
        rule_score=0.0,
        semantic_score=semantic_score,
        graph_score=graph_score,
        confirmed_score=confirmed_score,
        negative_penalty=0.0,
    )
    source = support or semantic_support
    if source is None:
        raise ValueError("语义或图谱候选至少需要一个支持来源。")
    path = list(source.category_path)
    return {
        "name": source.name or "/".join(path),
        "category_id": source.category_id,
        "category_path": path,
        "confidence": 0.2,
        "status": "NEEDS_REVIEW",
        "source": "graph",
        "evidence": [],
        "taxonomy_key": source.taxonomy_key,
        "taxonomy_version": source.taxonomy_version,
        "candidate_scores": {
            "rule": 0.0,
            "semantic": round(semantic_score, 4),
            "graph": round(graph_score, 4),
            "confirmed_support": round(confirmed_score, 4),
            "negative_penalty": 0.0,
            "combined": combined,
        },
        "graph_evidence": list(support.paths) if support else [],
        "semantic_evidence": _semantic_evidence(semantic_support),
    }


def _combined_score(
    *,
    rule_score: float,
    semantic_score: float,
    graph_score: float,
    confirmed_score: float,
    negative_penalty: float,
) -> float:
    """按第二版本固定初始权重计算仅用于排序的候选分数。"""

    score = (
        rule_score * 0.45
        + semantic_score * 0.30
        + graph_score * 0.15
        + confirmed_score * 0.10
        - negative_penalty
    )
    return round(max(0.0, min(1.0, score)), 4)


def _semantic_evidence(support: SemanticCategorySupport | None) -> dict:
    """生成脱敏语义支持，不返回来源文件标识。"""

    if support is None:
        return {}
    return {
        "support_count": support.support_count,
        "similarity_bucket": (
            "high" if support.semantic_score >= 0.8 else "medium" if support.semantic_score >= 0.6 else "low"
        ),
        "source": support.source,
    }


def _clamp(value) -> float:
    """把外部图谱分数限制在零到一。"""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _belongs_to_active_taxonomy(candidate, allowed_taxonomies: set[tuple[str, str]]) -> bool:
    """图谱只能增强本次分类已选 taxonomy，不能静默引入另一套业务目录。"""

    if not allowed_taxonomies:
        return False
    taxonomy_key = str(getattr(candidate, "taxonomy_key", "") or "")
    taxonomy_version = str(getattr(candidate, "taxonomy_version", "") or "")
    if taxonomy_key and taxonomy_version:
        return (taxonomy_key, taxonomy_version) in allowed_taxonomies
    graph_key = str(getattr(candidate, "graph_key", "") or "")
    return any(
        graph_key.startswith(f"{allowed_key}:{allowed_version}:")
        for allowed_key, allowed_version in allowed_taxonomies
    )
