"""分类体系配置加载测试。"""

import pytest
from pydantic import ValidationError

from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.matcher import flatten_category_paths
from app.modules.classification.schemas import Taxonomy


def test_default_taxonomy_loads_school_file_classification():
    """默认分类配置必须能加载学校文件归类表元数据。"""

    taxonomy = load_default_taxonomy()

    assert taxonomy.key == "school_file_classification"
    assert taxonomy.version == "2026-06-v2"
    assert taxonomy.categories[0].name == "学校"


def test_flatten_category_paths_preserves_parent_path():
    """分类树展平后必须保留完整父子路径，供回执展示和未来落库迁移。"""

    taxonomy = load_default_taxonomy()
    paths = flatten_category_paths(taxonomy)

    assert ["学校", "人事师资", "职称"] in [item.path for item in paths]
    assert ["学院", "行政管理", "年度计划、总结"] in [item.path for item in paths]


def test_legacy_taxonomy_without_v2_fields_still_loads():
    """旧版 name/children 分类配置必须继续兼容加载。"""

    taxonomy = Taxonomy.model_validate(
        {
            "key": "legacy",
            "name": "旧分类",
            "version": "2026-06",
            "source": "test",
            "categories": [
                {
                    "name": "学校",
                    "children": [{"name": "科研"}],
                }
            ],
        }
    )

    leaf = taxonomy.categories[0].children[0]
    assert leaf.id is None
    assert leaf.description == ""
    assert leaf.aliases == []
    assert leaf.positive_signals == []
    assert leaf.negative_signals == []
    assert leaf.examples == []


def test_default_taxonomy_contains_v2_metadata_for_high_frequency_categories():
    """默认分类中高频末级分类必须具备稳定 id、定义、别名、信号和示例。"""

    taxonomy = load_default_taxonomy()
    flattened = flatten_category_paths(taxonomy)
    enriched_leaves = [
        item
        for item in flattened
        if item.category_id and item.description and item.positive_signals and item.examples
    ]
    appointment = next(item for item in flattened if item.category_id == "school.hr.appointment-assessment")

    assert len(enriched_leaves) >= 20
    assert appointment.path == ["学校", "人事师资", "考核聘任"]
    assert "聘期考核" in appointment.aliases
    assert "教师" in appointment.positive_signals
    assert "奖学金" in appointment.negative_signals
    assert appointment.examples


def test_taxonomy_rejects_duplicate_category_ids():
    """分类 id 必须唯一，避免后续用显示名称作为外键。"""

    with pytest.raises(ValidationError, match="分类 id 重复"):
        Taxonomy.model_validate(
            {
                "key": "invalid",
                "name": "重复 id 分类",
                "version": "2026-06",
                "source": "test",
                "categories": [
                    {"id": "same.id", "name": "学校"},
                    {"id": "same.id", "name": "学院"},
                ],
            }
        )
