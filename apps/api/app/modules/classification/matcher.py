"""基于分类体系配置的确定性文本匹配器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.modules.classification.schemas import CategoryNode, Taxonomy


@dataclass(frozen=True)
class FlattenedCategory:
    """展平后的分类路径，便于关键词匹配和回执展示。"""

    path: list[str]
    name: str
    order: int
    category_id: str | None = None
    description: str = ""
    aliases: list[str] | None = None
    positive_signals: list[str] | None = None
    negative_signals: list[str] | None = None
    examples: list[str] | None = None


@dataclass(frozen=True)
class DocumentFeatures:
    """用于分类候选召回的文档特征，不包含持久化依赖。"""

    filename: str = ""
    title: str = ""
    full_text: str = ""
    headings: list[str] | None = None
    sheet_names: list[str] | None = None


@dataclass(frozen=True)
class CategoryCandidate:
    """分类候选召回结果，只用于排序和后续判定，不直接等同最终分类。"""

    category_id: str | None
    category_path: list[str]
    name: str
    rule_score: float
    matched_signals: list[str]
    negative_signals: list[str]
    candidate_reason: str
    taxonomy_key: str
    taxonomy_version: str
    order: int


def flatten_category_paths(taxonomy: Taxonomy) -> list[FlattenedCategory]:
    """把树状分类配置展平成完整路径列表。"""

    flattened: list[FlattenedCategory] = []

    def walk(node: CategoryNode, parent_path: list[str]) -> None:
        """递归遍历分类树，并记录节点顺序用于稳定排序。"""

        path = [*parent_path, node.name]
        flattened.append(
            FlattenedCategory(
                path=path,
                name=node.name,
                order=len(flattened),
                category_id=node.id,
                description=node.description,
                aliases=list(node.aliases),
                positive_signals=list(node.positive_signals),
                negative_signals=list(node.negative_signals),
                examples=list(node.examples),
            )
        )
        for child in node.children:
            walk(child, path)

    for root in taxonomy.categories:
        walk(root, [])
    return flattened


def recall_category_candidates(
    document_features: DocumentFeatures,
    taxonomy: Taxonomy,
    *,
    limit: int = 5,
) -> list[CategoryCandidate]:
    """根据分类名、别名、正负信号召回 Top N 分类候选。"""

    title_text = _join_text(
        [
            document_features.filename,
            document_features.title,
            *(document_features.headings or []),
            *(document_features.sheet_names or []),
        ]
    )
    body_text = document_features.full_text or ""
    candidates: list[CategoryCandidate] = []
    for category in flatten_category_paths(taxonomy):
        if len(category.path) == 1:
            continue
        score, matched_signals, negative_signals, reasons = _score_category_candidate(
            category=category,
            title_text=title_text,
            body_text=body_text,
        )
        if score <= 0:
            continue
        candidates.append(
            CategoryCandidate(
                category_id=category.category_id,
                category_path=category.path,
                name="/".join(category.path),
                rule_score=round(score, 4),
                matched_signals=matched_signals,
                negative_signals=negative_signals,
                candidate_reason="；".join(reasons),
                taxonomy_key=taxonomy.key,
                taxonomy_version=taxonomy.version,
                order=category.order,
            )
        )

    candidates = _dedupe_candidates_and_remove_shorter_embedded_matches(candidates)
    candidates.sort(key=lambda item: (-item.rule_score, item.order))
    return candidates[:max(0, min(limit, 8))]


def match_document_text(text: str, taxonomy: Taxonomy) -> list[dict[str, Any]]:
    """基于候选召回生成 rule-only 分类建议，保留旧调用入口。"""

    candidates = recall_category_candidates(
        DocumentFeatures(full_text=text or ""),
        taxonomy,
        limit=5,
    )
    if not candidates:
        return [_other_category(taxonomy)]

    matches = [_candidate_to_category(candidate) for candidate in candidates]
    return _dedupe_and_remove_shorter_embedded_matches(matches)


def _dedupe_and_remove_shorter_embedded_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去除重复路径，并过滤被更长分类名包含的短词误命中。"""

    longest_evidence = [
        str((item.get("evidence") or [""])[0])
        for item in matches
    ]
    filtered: list[dict[str, Any]] = []
    seen_paths: set[tuple[str, ...]] = set()
    for item in matches:
        path = tuple(str(value) for value in item.get("category_path", []))
        evidence = str((item.get("evidence") or [""])[0])
        if path in seen_paths:
            continue
        if any(evidence != other and evidence in other for other in longest_evidence):
            continue
        seen_paths.add(path)
        filtered.append(item)
    return filtered


def _dedupe_candidates_and_remove_shorter_embedded_matches(
    candidates: list[CategoryCandidate],
) -> list[CategoryCandidate]:
    """去除重复路径，候选阶段保留相近分类交给后续判定。"""

    filtered: list[CategoryCandidate] = []
    seen_paths: set[tuple[str, ...]] = set()
    for candidate in candidates:
        path = tuple(candidate.category_path)
        if path in seen_paths:
            continue
        seen_paths.add(path)
        filtered.append(candidate)
    return filtered


def _score_category_candidate(
    *,
    category: FlattenedCategory,
    title_text: str,
    body_text: str,
) -> tuple[float, list[str], list[str], list[str]]:
    """计算分类候选召回分数，并记录命中信号。"""

    title_signals = _unique_signals(
        [category.name, *(category.aliases or []), *(category.positive_signals or [])]
    )
    body_signals = title_signals
    matched_title = [signal for signal in title_signals if signal and signal in title_text]
    matched_body = [signal for signal in body_signals if signal and signal in body_text]
    matched_examples = [
        example
        for example in (category.examples or [])
        if example and (example in title_text or example in body_text)
    ]
    negative_signals = [
        signal
        for signal in _unique_signals(category.negative_signals or [])
        if signal and (signal in title_text or signal in body_text)
    ]
    matched_signals = _unique_signals([*matched_title, *matched_body, *matched_examples])
    if not matched_signals:
        return 0.0, [], negative_signals, []

    score = 0.0
    if category.name in title_text:
        score += 0.3 + min(0.08, len(category.name) * 0.01)
    if category.name in body_text:
        score += 0.2 + min(0.06, len(category.name) * 0.01)
    root_name = category.path[0] if category.path else ""
    if root_name and root_name in title_text:
        score += 0.08
    if root_name and root_name in body_text:
        score += 0.05
    if score > 0 and root_name == "学校" and "学院" not in title_text and "学院" not in body_text:
        score += 0.01
    combined_text = f"{title_text}\n{body_text}"
    if root_name == "学院" and "学校" in combined_text and "学院" not in combined_text:
        score -= 0.08
    score += min(0.25, 0.08 * len([signal for signal in matched_title if signal != category.name]))
    score += min(0.2, 0.045 * len([signal for signal in matched_body if signal != category.name]))
    score += min(0.15, 0.05 * len(matched_examples))
    score -= min(0.25, 0.08 * len(negative_signals))

    reasons: list[str] = []
    if matched_title:
        reasons.append(f"标题/文件名命中：{'、'.join(matched_title[:5])}")
    if matched_body:
        reasons.append(f"正文命中：{'、'.join(matched_body[:5])}")
    if matched_examples:
        reasons.append(f"示例命中：{'、'.join(matched_examples[:2])}")
    if negative_signals:
        reasons.append(f"负向信号降分：{'、'.join(negative_signals[:5])}")
    return max(0.0, score), matched_signals, negative_signals, reasons


def _candidate_to_category(candidate: CategoryCandidate) -> dict[str, Any]:
    """把候选召回结果转换为现有 rule-only 分类建议结构。"""

    return {
        "name": candidate.name,
        "category_id": candidate.category_id,
        "category_path": candidate.category_path,
        "confidence": min(0.95, round(0.45 + candidate.rule_score * 0.5, 2)),
        "status": "SUGGESTED",
        "source": "rule",
        "evidence": candidate.matched_signals[:5],
        "rule_score": candidate.rule_score,
        "matched_signals": candidate.matched_signals,
        "negative_signals": candidate.negative_signals,
        "taxonomy_key": candidate.taxonomy_key,
        "taxonomy_version": candidate.taxonomy_version,
        "candidate_reason": candidate.candidate_reason,
    }


def _join_text(values: list[str]) -> str:
    """合并文档标题类字段，供候选召回计算。"""

    return "\n".join(value for value in values if value)


def _unique_signals(values: list[str]) -> list[str]:
    """保留顺序去重，避免重复信号放大分数。"""

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        signal = value.strip()
        if not signal or signal in seen:
            continue
        seen.add(signal)
        result.append(signal)
    return result


def _other_category(taxonomy: Taxonomy) -> dict[str, Any]:
    """生成无法命中时的兜底分类建议。"""

    return {
        "name": "其他",
        "category_path": ["其他"],
        "confidence": 0.2,
        "status": "SUGGESTED",
        "evidence": [],
        "taxonomy_key": taxonomy.key,
        "taxonomy_version": taxonomy.version,
    }
