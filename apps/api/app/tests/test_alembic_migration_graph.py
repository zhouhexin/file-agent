"""验证 File Agent 数据库迁移图保持唯一、可升级的正式主线。"""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def _migration_scripts() -> ScriptDirectory:
    """加载项目迁移目录，不连接或修改任何数据库。"""

    api_root = Path(__file__).resolve().parents[2]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    return ScriptDirectory.from_config(config)


def test_database_migrations_have_one_current_head() -> None:
    """并行功能迁移汇合后，新增迁移也必须保持唯一正式主线。"""

    scripts = _migration_scripts()

    assert scripts.get_heads() == ["20260908_0005"]
    request_revision = scripts.get_revision("20260908_0005")
    assert request_revision is not None
    assert request_revision.down_revision == "20260908_0004"
    organization_revision = scripts.get_revision("20260908_0004")
    assert organization_revision is not None
    assert organization_revision.down_revision == "20260908_0003"
    extraction_revision = scripts.get_revision("20260908_0003")
    assert extraction_revision is not None
    assert extraction_revision.down_revision == "20260908_0002"
    duplicate_revision = scripts.get_revision("20260908_0002")
    assert duplicate_revision is not None
    assert duplicate_revision.down_revision == "20260908_0001"
    ingest_revision = scripts.get_revision("20260908_0001")
    assert ingest_revision is not None
    assert ingest_revision.down_revision == "20260901_0001"
    merge_revision = scripts.get_revision("20260825_0001")
    assert merge_revision is not None
    assert set(merge_revision.down_revision) == {
        "20260813_0001",
        "20260824_0001",
    }


def test_merge_head_contains_both_feature_migration_paths() -> None:
    """从共同基线升级到唯一 head 时必须执行两侧功能迁移。"""

    scripts = _migration_scripts()
    revision_ids = {
        item.revision
        for item in scripts.iterate_revisions("20260827_0001", "20260730_0001")
    }

    assert {
        "20260813_0001",
        "20260824_0001",
        "20260825_0001",
        "20260826_0001",
        "20260827_0001",
    } <= revision_ids
