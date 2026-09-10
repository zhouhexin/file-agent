"""分类体系配置加载器。"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.classification.schemas import CategoryNode, CategoryNodeKind, Taxonomy


TAXONOMY_DIR = Path(__file__).resolve().parent / "taxonomies"
DEFAULT_TAXONOMY_PATH = TAXONOMY_DIR / "unified_school_file_classification.json"


def load_taxonomy(path: Path) -> Taxonomy:
    """从 JSON 文件加载并校验分类体系。"""

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    taxonomy = Taxonomy.model_validate(payload)
    _materialize_fallback_nodes(taxonomy)
    _compile_node_capabilities(taxonomy)
    # 物化节点也必须重新经过稳定 ID 和安全物理路径校验。
    return Taxonomy.model_validate(taxonomy.model_dump())


def load_default_taxonomy() -> Taxonomy:
    """加载统一分类体系，每次读取以便版本文件更新后立即生效。"""

    taxonomy = load_taxonomy(DEFAULT_TAXONOMY_PATH)
    _validate_default_organization_paths(taxonomy)
    return taxonomy


def _validate_default_organization_paths(taxonomy: Taxonomy) -> None:
    """确保统一 taxonomy 中所有可参与分类的节点都有安全物理路径。"""

    missing: list[str] = []

    def walk(nodes, *, depth: int) -> None:
        """只校验允许成为主类的节点；历史隐藏节点仍可保留空路径读取。"""

        for node in nodes:
            if node.primary_enabled and not node.organization_path:
                missing.append(str(node.id or node.name))
            walk(node.children, depth=depth + 1)

    walk(taxonomy.categories, depth=0)
    if missing:
        raise ValueError(
            "统一分类体系存在未配置 organization_path 的候选分类："
            + ", ".join(missing)
        )


def _materialize_fallback_nodes(taxonomy: Taxonomy) -> None:
    """把策略模板展开为可查询、可投影、可解析物理路径的稳定分类节点。"""

    policy = taxonomy.fallback_policy
    if policy is None:
        return
    nodes_by_id: dict[str, CategoryNode] = {}

    def index(node: CategoryNode) -> None:
        """建立当前节点索引，已有显式节点优先于策略模板。"""

        if node.id:
            nodes_by_id[node.id] = node
        for child in node.children:
            index(child)

    for root in taxonomy.categories:
        index(root)
    base_ids = {
        *policy.department_category_ids,
        *[
            str(root.id)
            for root in taxonomy.categories
            if root.id and root.name in {"学校", "学院"}
        ],
    }
    for base_id in sorted(base_ids):
        base = nodes_by_id.get(base_id)
        if base is None:
            continue
        base_path = list(base.organization_path or [base.name])
        for leaf in (policy.issued, policy.other):
            if leaf is None:
                continue
            category_id = f"{base_id}.{leaf.id_suffix}"
            if category_id in nodes_by_id:
                continue
            child = CategoryNode(
                id=category_id,
                name=leaf.name,
                description=(
                    "未命中更具体业务分类时，由组织层级、部门和文号规则生成的兜底分类。"
                ),
                organization_path=[*base_path, leaf.name],
            )
            base.children.append(child)
            nodes_by_id[category_id] = child


def _compile_node_capabilities(taxonomy: Taxonomy) -> None:
    """把旧 taxonomy 编译成显式节点能力，同时保留历史 fallback ID 查询。"""

    historical_fallback_ids: set[str] = set()
    policy = taxonomy.fallback_policy
    if policy is not None:
        historical_fallback_ids.update(policy.historical_category_ids)
        leaves = [leaf for leaf in (policy.issued, policy.other) if leaf is not None]
        base_ids = {
            *policy.department_category_ids,
            *[
                str(root.id)
                for root in taxonomy.categories
                if root.id and root.name in {"学校", "学院"}
            ],
        }
        historical_fallback_ids = {
            f"{base_id}.{leaf.id_suffix}"
            for base_id in base_ids
            for leaf in leaves
        }

    def walk(node: CategoryNode, *, depth: int) -> None:
        """配置显式值优先；仅对缺失字段应用可复现的兼容编译规则。"""

        is_system_other = node.id == "system.other"
        is_historical_fallback = bool(node.id and node.id in historical_fallback_ids)
        if node.node_kind is None:
            if is_system_other:
                node.node_kind = CategoryNodeKind.FALLBACK
            elif depth == 0:
                node.node_kind = CategoryNodeKind.GROUP
            elif is_historical_fallback:
                node.node_kind = CategoryNodeKind.FALLBACK
            else:
                node.node_kind = CategoryNodeKind.BUSINESS
        if node.recall_enabled is None:
            node.recall_enabled = node.node_kind not in {
                CategoryNodeKind.GROUP,
                CategoryNodeKind.FALLBACK,
            }
        if node.primary_enabled is None:
            node.primary_enabled = bool(
                (depth > 0 or is_system_other)
                and node.organization_path
                and node.node_kind != CategoryNodeKind.GROUP
            )
        if node.selectable is None:
            node.selectable = not is_historical_fallback or is_system_other
        if node.visible is None:
            node.visible = not is_historical_fallback or is_system_other
        for child in node.children:
            walk(child, depth=depth + 1)

    for root in taxonomy.categories:
        walk(root, depth=0)
