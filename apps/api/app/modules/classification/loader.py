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

    return load_taxonomy(DEFAULT_TAXONOMY_PATH)
