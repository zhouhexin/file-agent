"""受管目录角色 Profile 和弱标签投影测试。"""

import json

from app.modules.knowledge_graph.managed_path_profile import ManagedPathProfileRegistry
from app.modules.knowledge_graph.projection_service import GraphProjectionService
from app.tests.test_graph_projection_service import RecordingGraphRepository


def test_profile_uses_longest_path_prefix(tmp_path):
    """具体业务目录规则必须优先于上级部门规则。"""

    (tmp_path / "downloads.json").write_text(
        json.dumps(
            {
                "root_key": "downloads",
                "version": "v1",
                "default_role": "UNKNOWN",
                "rules": [
                    {"path_prefix": "人事处", "role": "DEPARTMENT"},
                    {
                        "path_prefix": "人事处/职称评定",
                        "role": "CATEGORY",
                        "category_path": ["人事处", "职称评定"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    registry = ManagedPathProfileRegistry.load(tmp_path)

    assert registry.resolve(root_key="downloads", relative_path="人事处/2026").role == "DEPARTMENT"
    resolved = registry.resolve(root_key="downloads", relative_path="人事处/职称评定/2026")
    assert resolved.role == "CATEGORY"
    assert resolved.category_path == ("人事处", "职称评定")


def test_weak_label_projection_only_maps_category_roles(tmp_path):
    """弱标签根只能指向全局已有分类，不能把年份和临时目录建成新分类。"""

    (tmp_path / "downloads.json").write_text(
        json.dumps(
            {
                "root_key": "downloads",
                "version": "v1",
                "rules": [
                    {"path_prefix": "党办", "role": "DEPARTMENT"},
                    {"path_prefix": "党办/2026", "role": "YEAR"},
                    {
                        "path_prefix": "党办/2026/科学发展观",
                        "role": "CATEGORY",
                        "category_path": ["党办", "科学发展观"],
                    },
                    {"path_prefix": "临时", "role": "TEMPORARY"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "classified_archive.json").write_text(
        json.dumps(
            {
                "root_key": "classified_archive",
                "version": "v1",
                "rules": [
                    {
                        "path_prefix": "归档/科学发展观",
                        "role": "CATEGORY",
                        "category_path": ["党办", "科学发展观"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repository = RecordingGraphRepository()
    service = GraphProjectionService(
        repository=repository,
        profile_registry=ManagedPathProfileRegistry.load(tmp_path),
    )

    service.sync_managed_paths(
        [
            (
                "classified_archive",
                "分类档案",
                "PATH_AS_CATEGORY",
                "归档/科学发展观",
                5,
            ),
            ("downloads", "downloads", "PATH_AS_WEAK_LABEL", "党办/2026/科学发展观", 3),
            ("downloads", "downloads", "PATH_AS_WEAK_LABEL", "临时/待处理", 2),
        ]
    )

    assert {folder.relative_path for folder in repository.folders} >= {
        "党办",
        "党办/2026",
        "党办/2026/科学发展观",
        "临时",
        "临时/待处理",
    }
    mapped_paths = {
        tuple(category.path)
        for category in repository.categories
        if category.graph_key in {
            relation.category_graph_key for relation in repository.folder_category_relations
        }
    }
    assert mapped_paths == {("党办", "科学发展观")}
