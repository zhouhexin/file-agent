"""把预置 taxonomy 和受管目录证据收敛为单一分类体系。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.modules.classification.schemas import Taxonomy


_MERGEABLE_ROLES = {"CATEGORY", "DEPARTMENT"}


def build_unified_taxonomy(
    *,
    base_payload: dict[str, Any],
    inventory_payload: dict[str, Any],
    version: str,
) -> dict[str, Any]:
    """保留稳定分类 ID，把经过清洗的目录词合并到节点信号中。"""

    payload = deepcopy(base_payload)
    payload["key"] = "unified_school_file_classification"
    payload["name"] = "学校文件统一分类体系"
    payload["version"] = str(version).strip()
    snapshot_version = str(inventory_payload.get("snapshot_version") or "unknown").strip()
    payload["source"] = (
        f"{base_payload.get('source') or 'preset-taxonomy'} + managed-directory-inventory@{snapshot_version}"
    )

    nodes_by_id = _nodes_by_id(payload.get("categories") or [])
    for entry in inventory_payload.get("entries") or []:
        role = str(entry.get("role") or "UNKNOWN").strip().upper()
        if role not in _MERGEABLE_ROLES:
            continue
        category_id = str(entry.get("merge_into_category_id") or "").strip()
        if not category_id:
            continue
        node = nodes_by_id.get(category_id)
        if node is None:
            raise ValueError(f"受管目录指向了不存在的分类 ID：{category_id}")
        node["aliases"] = _ordered_unique(
            [*(node.get("aliases") or []), *(entry.get("aliases") or []), entry.get("name")]
        )
        node["positive_signals"] = _ordered_unique(
            [*(node.get("positive_signals") or []), *(entry.get("positive_signals") or [])]
        )

    for enrichment in inventory_payload.get("taxonomy_enrichments") or []:
        category_id = str(enrichment.get("category_id") or "").strip()
        node = nodes_by_id.get(category_id)
        if node is None:
            raise ValueError(f"统一分类增强指向了不存在的分类 ID：{category_id}")
        for field_name in ("aliases", "positive_signals", "negative_signals", "examples"):
            node[field_name] = _ordered_unique(
                [*(node.get(field_name) or []), *(enrichment.get(field_name) or [])]
            )
        description = str(enrichment.get("description") or "").strip()
        if description:
            node["description"] = description

    Taxonomy.model_validate(payload)
    return payload


def _nodes_by_id(categories: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """递归索引所有稳定分类节点。"""

    result: dict[str, dict[str, Any]] = {}

    def visit(node: dict[str, Any]) -> None:
        category_id = str(node.get("id") or "").strip()
        if category_id:
            if category_id in result:
                raise ValueError(f"分类 ID 重复：{category_id}")
            result[category_id] = node
        for child in node.get("children") or []:
            visit(child)

    for category in categories:
        visit(category)
    return result


def _ordered_unique(values: list[Any]) -> list[str]:
    """清理空值并按输入顺序去重。"""

    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
