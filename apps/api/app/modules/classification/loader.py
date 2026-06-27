"""分类体系配置加载器。"""

from __future__ import annotations

import json
from pathlib import Path

from app.modules.classification.schemas import Taxonomy


TAXONOMY_DIR = Path(__file__).resolve().parent / "taxonomies"
DEFAULT_TAXONOMY_PATH = TAXONOMY_DIR / "school_file_classification.json"


def load_taxonomy(path: Path) -> Taxonomy:
    """从 JSON 文件加载并校验分类体系。"""

    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    return Taxonomy.model_validate(payload)


def load_default_taxonomy() -> Taxonomy:
    """加载当前系统默认启用的学校文件归类表，每次读取以便配置变更立即生效。"""

    return load_taxonomy(DEFAULT_TAXONOMY_PATH)
