"""统一分类体系构建器测试。"""

from app.modules.classification.unified_builder import build_unified_taxonomy
from app.modules.classification.schemas import Taxonomy


def _base_taxonomy() -> dict:
    """构造最小预置分类体系。"""

    return {
        "key": "legacy",
        "name": "旧分类",
        "version": "v0",
        "source": "legacy.json",
        "categories": [
            {
                "id": "school",
                "name": "学校",
                "children": [
                    {
                        "id": "school.hr",
                        "name": "人事师资",
                        "aliases": ["人事"],
                        "positive_signals": ["人事"],
                    }
                ],
            }
        ],
    }


def test_builder_merges_managed_directory_terms_into_existing_category():
    """受管目录词应合并进稳定分类节点，不创建运行时映射。"""

    payload = build_unified_taxonomy(
        base_payload=_base_taxonomy(),
        inventory_payload={
            "snapshot_version": "managed-v1",
            "entries": [
                {
                    "name": "人事处",
                    "role": "DEPARTMENT",
                    "merge_into_category_id": "school.hr",
                    "aliases": ["人事处", "人事文件"],
                    "positive_signals": ["任职资格"],
                }
            ],
        },
        version="2026-07-v1",
    )

    taxonomy = Taxonomy.model_validate(payload)
    node = taxonomy.categories[0].children[0]
    assert taxonomy.key == "unified_school_file_classification"
    assert taxonomy.version == "2026-07-v1"
    assert node.id == "school.hr"
    assert node.aliases == ["人事", "人事处", "人事文件"]
    assert node.positive_signals == ["人事", "任职资格"]


def test_builder_excludes_structural_directories():
    """年份、临时和集合目录不得成为统一分类词。"""

    payload = build_unified_taxonomy(
        base_payload=_base_taxonomy(),
        inventory_payload={
            "snapshot_version": "managed-v1",
            "entries": [
                {
                    "name": "2026",
                    "role": "YEAR",
                    "merge_into_category_id": "school.hr",
                    "aliases": ["2026"],
                },
                {
                    "name": "临时",
                    "role": "TEMPORARY",
                    "merge_into_category_id": "school.hr",
                    "aliases": ["临时"],
                },
            ],
        },
        version="2026-07-v1",
    )

    node = Taxonomy.model_validate(payload).categories[0].children[0]
    assert "2026" not in node.aliases
    assert "临时" not in node.aliases


def test_builder_rejects_unknown_target_category():
    """构建输入不得把受管目录合并到不存在的分类 ID。"""

    try:
        build_unified_taxonomy(
            base_payload=_base_taxonomy(),
            inventory_payload={
                "snapshot_version": "managed-v1",
                "entries": [
                    {
                        "name": "未知目录",
                        "role": "CATEGORY",
                        "merge_into_category_id": "missing.category",
                    }
                ],
            },
            version="2026-07-v1",
        )
    except ValueError as exc:
        assert "missing.category" in str(exc)
    else:
        raise AssertionError("未知分类 ID 应导致构建失败。")


def test_builder_incrementally_preserves_previous_inventory_signals():
    """连续合并新快照时必须保留前一版本已经加入的分类信号。"""

    first = build_unified_taxonomy(
        base_payload=_base_taxonomy(),
        inventory_payload={
            "snapshot_version": "managed-v1",
            "entries": [
                {
                    "name": "人事处",
                    "role": "DEPARTMENT",
                    "merge_into_category_id": "school.hr",
                    "aliases": ["人事处"],
                }
            ],
        },
        version="v1",
    )
    second = build_unified_taxonomy(
        base_payload=first,
        inventory_payload={
            "snapshot_version": "managed-v2",
            "entries": [
                {
                    "name": "教师工作部",
                    "role": "DEPARTMENT",
                    "merge_into_category_id": "school.hr",
                    "aliases": ["教师工作部"],
                }
            ],
        },
        version="v2",
    )

    node = Taxonomy.model_validate(second).categories[0].children[0]
    assert node.aliases == ["人事", "人事处", "教师工作部"]
    assert second["version"] == "v2"
