"""分类体系配置加载器。"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.classification.schemas import CategoryNode, Taxonomy


TAXONOMY_DIR = Path(__file__).resolve().parent / "taxonomies"
DEFAULT_TAXONOMY_PATH = TAXONOMY_DIR / "unified_school_file_classification.json"


def load_taxonomy(path: Path) -> Taxonomy:
    """从 JSON 文件加载并校验分类体系。"""

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    taxonomy = Taxonomy.model_validate(payload)
    _materialize_fallback_nodes(taxonomy)
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
        """根节点只用于分组，根节点以下均属于当前 matcher 的候选空间。"""

        for node in nodes:
            if depth > 0 and not node.organization_path:
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
