"""受管目录全局分类候选服务测试。"""

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import ManagedFile, ManagedRoot
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.managed_catalog import GlobalManagedCategoryCatalogService
from app.modules.knowledge_graph.managed_path_profile import ManagedPathProfileRegistry


def test_global_catalog_deduplicates_same_path_across_roots(tmp_path):
    """相同分类路径必须全局合并，稳定 ID 不能包含 root_key。"""

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    for root_key in ["archive_a", "archive_b"]:
        (profile_dir / f"{root_key}.json").write_text(
            json.dumps(
                {
                    "root_key": root_key,
                    "version": "v1",
                    "rules": [
                        {
                            "path_prefix": "人事处/职称评定",
                            "role": "CATEGORY",
                            "category_path": ["人事处", "职称评定"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        for index, root_key in enumerate(["archive_a", "archive_b"], start=1):
            root = ManagedRoot(
                id=f"root-{index}",
                root_key=root_key,
                display_name=root_key,
                container_path=str(tmp_path / root_key),
                classification_mode="PATH_AS_CATEGORY",
            )
            db.add(root)
            db.add(
                ManagedFile(
                    id=f"file-{index}",
                    root_id=root.id,
                    relative_path="人事处/职称评定/示例.docx",
                    category_path="人事处/职称评定",
                    filename="示例.docx",
                    extension=".docx",
                    size_bytes=10,
                    fingerprint=str(index) * 64,
                    status="ACTIVE",
                )
            )
        db.flush()

        catalog_service = GlobalManagedCategoryCatalogService(
            db=db,
            profile_registry=ManagedPathProfileRegistry.load(profile_dir),
        )
        catalog = catalog_service.load()

        assert len(catalog.categories) == 1
        category = catalog.categories[0]
        assert category.category_id.startswith("managed.global.")
        assert category.category_path == ("人事处", "职称评定")
        assert category.source_roots == ("archive_a", "archive_b")
        assert catalog.taxonomy_key == "managed_global_categories"
        assert catalog.taxonomy_version.startswith("managed-global-")

        classifier = DocumentClassificationService(
            db=db,
            graph_mode="off",
            managed_catalog_service=catalog_service,
        )
        managed_result = classifier.classify(
            document_id="managed-document",
            extraction_run_id="",
            filename="受管职称材料.txt",
            fallback_text="本材料用于教师职称评定。",
        )
        uploaded_result = classifier.classify(
            document_id="uploaded-document",
            extraction_run_id="",
            filename="上传职称材料.txt",
            fallback_text="本材料用于教师职称评定。",
        )
        managed_ids = {item["category_id"] for item in managed_result["categories"]}
        uploaded_ids = {item["category_id"] for item in uploaded_result["categories"]}
        assert "school.hr.title-review" in managed_ids
        assert managed_ids == uploaded_ids
        assert {
            item["taxonomy_key"] for item in managed_result["categories"]
        } == {"unified_school_file_classification"}
        assert {
            item["taxonomy_version"] for item in uploaded_result["categories"]
        } == {"2026-07-v2"}
    finally:
        db.close()


def test_global_catalog_excludes_non_category_profile_roles(tmp_path):
    """年份和临时目录不得进入全局业务分类目录。"""

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "archive.json").write_text(
        json.dumps(
            {
                "root_key": "archive",
                "version": "v1",
                "rules": [
                    {"path_prefix": "人事处/2026", "role": "YEAR"},
                    {"path_prefix": "临时", "role": "TEMPORARY"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        root = ManagedRoot(
            id="root-archive",
            root_key="archive",
            display_name="archive",
            container_path=str(tmp_path / "archive"),
            classification_mode="PATH_AS_CATEGORY",
        )
        db.add(root)
        for index, category_path in enumerate(["人事处/2026", "临时"], start=1):
            db.add(
                ManagedFile(
                    id=f"file-role-{index}",
                    root_id=root.id,
                    relative_path=f"{category_path}/示例{index}.docx",
                    category_path=category_path,
                    filename=f"示例{index}.docx",
                    extension=".docx",
                    size_bytes=10,
                    fingerprint=str(index) * 64,
                    status="ACTIVE",
                )
            )
        db.flush()

        catalog = GlobalManagedCategoryCatalogService(
            db=db,
            profile_registry=ManagedPathProfileRegistry.load(profile_dir),
        ).load()

        assert catalog.categories == ()
    finally:
        db.close()


def test_global_catalog_excludes_disabled_category_source_roots(tmp_path):
    """停用的分类来源根即使仍有 Profile 和索引记录，也不能进入全局目录。"""

    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    for root_key in ["active_archive", "disabled_archive"]:
        (profile_dir / f"{root_key}.json").write_text(
            json.dumps(
                {
                    "root_key": root_key,
                    "version": "v1",
                    "rules": [
                        {
                            "path_prefix": f"{root_key}/分类",
                            "role": "CATEGORY",
                            "category_path": [root_key, "分类"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        for index, root_key in enumerate(["active_archive", "disabled_archive"], start=1):
            root = ManagedRoot(
                id=f"root-source-{index}",
                root_key=root_key,
                display_name=root_key,
                container_path=str(tmp_path / root_key),
                classification_mode="PATH_AS_CATEGORY",
                enabled=root_key == "active_archive",
            )
            db.add(root)
            db.add(
                ManagedFile(
                    id=f"file-source-{index}",
                    root_id=root.id,
                    relative_path=f"{root_key}/分类/示例.docx",
                    category_path=f"{root_key}/分类",
                    filename="示例.docx",
                    extension=".docx",
                    size_bytes=10,
                    fingerprint=str(index) * 64,
                    status="ACTIVE",
                )
            )
        db.flush()

        catalog = GlobalManagedCategoryCatalogService(
            db=db,
            profile_registry=ManagedPathProfileRegistry.load(profile_dir),
        ).load()

        assert [category.category_path for category in catalog.categories] == [
            ("active_archive", "分类")
        ]
        assert catalog.source_root_count == 1
    finally:
        db.close()
