"""分类体系配置加载器。"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.classification.schemas import Taxonomy


TAXONOMY_DIR = Path(__file__).resolve().parent / "taxonomies"
DEFAULT_TAXONOMY_PATH = TAXONOMY_DIR / "unified_school_file_classification.json"


def load_taxonomy(path: Path) -> Taxonomy:
    """从 JSON 文件加载并校验分类体系。"""

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return Taxonomy.model_validate(payload)


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
