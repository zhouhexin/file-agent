"""分类体系配置加载测试。"""

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import flatten_category_paths


def test_default_taxonomy_loads_school_file_classification():
    """默认分类配置必须能加载学校文件归类表元数据。"""

    taxonomy = load_default_taxonomy()

    assert taxonomy.key == "school_file_classification"
    assert taxonomy.version == "2026-06"
    assert taxonomy.categories[0].name == "学校"


def test_flatten_category_paths_preserves_parent_path():
    """分类树展平后必须保留完整父子路径，供回执展示和未来落库迁移。"""

    taxonomy = load_default_taxonomy()
    paths = flatten_category_paths(taxonomy)

    assert ["学校", "人事师资", "职称"] in [item.path for item in paths]
    assert ["学院", "行政管理", "年度计划、总结"] in [item.path for item in paths]
