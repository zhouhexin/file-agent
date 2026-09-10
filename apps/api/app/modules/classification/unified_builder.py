"""把预置 taxonomy 和受管目录证据收敛为单一分类体系。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.modules.classification.schemas import CategoryNodeKind, Taxonomy


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

    for addition in inventory_payload.get("category_additions") or []:
        _add_category(payload=payload, nodes_by_id=nodes_by_id, addition=addition)

    fallback_override = inventory_payload.get("fallback_policy_override")
    if fallback_override is not None:
        payload["fallback_policy"] = deepcopy(fallback_override)

    _compile_snapshot_capabilities(payload)

    return Taxonomy.model_validate(payload).model_dump(mode="json", exclude_none=True)


def _add_category(
    *,
    payload: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    addition: dict[str, Any],
) -> None:
    """按稳定父 ID 添加新节点，不允许覆盖已有分类。"""

    node = deepcopy(dict(addition.get("node") or {}))
    category_id = str(node.get("id") or "").strip()
    if not category_id:
        raise ValueError("新增分类缺少稳定 ID")
    if category_id in nodes_by_id:
        raise ValueError(f"新增分类 ID 已存在：{category_id}")
    parent_category_id = str(addition.get("parent_category_id") or "").strip()
    if parent_category_id:
        parent = nodes_by_id.get(parent_category_id)
        if parent is None:
            raise ValueError(f"新增分类父 ID 不存在：{parent_category_id}")
        parent.setdefault("children", []).append(node)
    else:
        payload.setdefault("categories", []).append(node)
    nodes_by_id[category_id] = node


def _compile_snapshot_capabilities(payload: dict[str, Any]) -> None:
    """让生成文件字段齐全，运行时无需根据名称或后缀猜测节点角色。"""

    fallback_policy = dict(payload.get("fallback_policy") or {})
    historical_ids = {
        str(value) for value in fallback_policy.get("historical_category_ids") or []
    }

    def visit(node: dict[str, Any], *, depth: int) -> None:
        """新配置显式值优先；兼容节点依据冻结历史 ID 清单编译。"""

        category_id = str(node.get("id") or "")
        is_system_other = category_id == "system.other"
        is_historical_fallback = category_id in historical_ids
        if is_system_other or is_historical_fallback:
            default_kind = CategoryNodeKind.FALLBACK.value
        elif depth == 0:
            default_kind = CategoryNodeKind.GROUP.value
        else:
            default_kind = CategoryNodeKind.BUSINESS.value
        node.setdefault("node_kind", default_kind)
        node.setdefault(
            "recall_enabled",
            node["node_kind"] not in {
                CategoryNodeKind.GROUP.value,
                CategoryNodeKind.FALLBACK.value,
            },
        )
        node.setdefault(
            "primary_enabled",
            bool(
                node.get("organization_path")
                and node["node_kind"] != CategoryNodeKind.GROUP.value
            ),
        )
        node.setdefault("selectable", is_system_other or not is_historical_fallback)
        node.setdefault("visible", is_system_other or not is_historical_fallback)
        for child in node.get("children") or []:
            visit(child, depth=depth + 1)

    for root in payload.get("categories") or []:
        visit(root, depth=0)


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
