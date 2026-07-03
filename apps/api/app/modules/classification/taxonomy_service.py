"""分类体系目录读取服务。

本服务只读取项目内固定 taxonomy 配置，用于回答“系统支持哪些分类”。
它不读取用户文件分类建议，也不允许 LLM 自由生成分类路径。
"""

from __future__ import annotations

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode


def read_default_taxonomy_catalog(*, detail_level: str = "brief", max_depth: int = 2) -> dict:
    """读取默认文件分类目录，并返回可安全展示的结构化摘要。"""

    taxonomy = load_default_taxonomy()
    depth = max(1, max_depth)
    return {
        "ok": True,
        "taxonomy": {
            "key": taxonomy.key,
            "name": taxonomy.name,
            "version": taxonomy.version,
            "source": taxonomy.source,
            "categories": [
                _node_to_catalog_item(node=node, detail_level=detail_level, max_depth=depth, current_depth=1)
                for node in taxonomy.categories
            ],
        },
    }


def _node_to_catalog_item(
    *,
    node: CategoryNode,
    detail_level: str,
    max_depth: int,
    current_depth: int,
) -> dict:
    """把分类节点转换为前端和 Agent response 可展示的轻量结构。"""

    item = {
        "id": node.id,
        "name": node.name,
    }
    if detail_level == "full":
        item.update(
            {
                "description": node.description,
                "aliases": node.aliases,
                "examples": node.examples,
            }
        )
    if current_depth < max_depth and node.children:
        item["children"] = [
            _node_to_catalog_item(
                node=child,
                detail_level=detail_level,
                max_depth=max_depth,
                current_depth=current_depth + 1,
            )
            for child in node.children
        ]
    return item
