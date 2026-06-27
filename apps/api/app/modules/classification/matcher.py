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


def flatten_category_paths(taxonomy: Taxonomy) -> list[FlattenedCategory]:
    """把树状分类配置展平成完整路径列表。"""

    flattened: list[FlattenedCategory] = []

    def walk(node: CategoryNode, parent_path: list[str]) -> None:
        """递归遍历分类树，并记录节点顺序用于稳定排序。"""

        path = [*parent_path, node.name]
        flattened.append(FlattenedCategory(path=path, name=node.name, order=len(flattened)))
        for child in node.children:
            walk(child, path)

    for root in taxonomy.categories:
        walk(root, [])
    return flattened


def match_document_text(text: str, taxonomy: Taxonomy) -> list[dict[str, Any]]:
    """用分类名称匹配正文，返回带 taxonomy 元数据的分类建议。"""

    normalized_text = text or ""
    matches: list[dict[str, Any]] = []
    for category in flatten_category_paths(taxonomy):
        if len(category.path) == 1:
            continue
        if category.name not in normalized_text:
            continue
        matches.append(
            {
                "name": "/".join(category.path),
                "category_path": category.path,
                "confidence": _confidence_for_category(category, normalized_text),
                "status": "SUGGESTED",
                "evidence": [category.name],
                "taxonomy_key": taxonomy.key,
                "taxonomy_version": taxonomy.version,
                "_order": category.order,
            }
        )

    if not matches:
        return [_other_category(taxonomy)]

    matches.sort(key=lambda item: (-float(item["confidence"]), int(item["_order"])))
    for item in matches:
        item.pop("_order", None)
    return matches[:5]


def _confidence_for_category(category: FlattenedCategory, text: str) -> float:
    """根据路径深度、分类名长度和一级域上下文给出规则置信度。"""

    root_bonus = 0.08 if category.path and category.path[0] in text else 0
    return min(0.95, round(0.58 + len(category.path) * 0.04 + len(category.name) * 0.01 + root_bonus, 2))


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
