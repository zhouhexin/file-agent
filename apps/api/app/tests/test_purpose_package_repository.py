"""不可变分类用途包持久化测试。"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Workspace
from app.modules.classification.purpose_policy import (
    PurposePackageMember,
    PurposePackageSnapshot,
)
from app.modules.classification.purpose_repository import PurposePackageRepository


def test_purpose_package_persists_members_and_rejects_in_place_update():
    """用途或成员变化必须创建新快照，不能改写既有授权事实。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        workspace = Workspace(id="purpose-workspace", name="用途包测试")
        db.add(workspace)
        db.flush()
        snapshot = PurposePackageSnapshot(
            id="purpose-package-1",
            workspace_id=workspace.id,
            root_key="managed",
            source_container_id="container-1",
            purpose_category_id="college.hr.faculty-recruitment",
            taxonomy_key="unified_school_file_classification",
            taxonomy_version="2026-09-v10",
            policy_id="recruitment-package",
            policy_version="1",
            manifest_digest="a" * 64,
            members=(
                PurposePackageMember(
                    document_version_id="version-1",
                    sha256="b" * 64,
                ),
            ),
            authorization_source="INITIAL_INGEST_TASK",
            source_request_id="ingest-item-1",
        )
        record = PurposePackageRepository(db).create(snapshot)
        assert record.members_json[0]["document_version_id"] == "version-1"

        record.purpose_category_id = "school.finance"
        with pytest.raises(ValueError, match="不可修改"):
            db.flush()
    finally:
        db.rollback()
        db.close()
