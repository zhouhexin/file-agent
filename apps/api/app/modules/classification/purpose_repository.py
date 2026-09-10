"""不可变分类用途包快照的持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import ClassificationPurposePackage
from app.modules.classification.purpose_policy import PurposePackageSnapshot


class PurposePackageRepository:
    """只允许创建和读取用途包；更新必须由调用方创建新 ID/摘要。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(self, snapshot: PurposePackageSnapshot) -> ClassificationPurposePackage:
        """持久化完整成员事实，不接受只包含可变路径的包。"""

        members = [member.model_dump(mode="json") for member in snapshot.members]
        if not members or any(
            not member.get("document_version_id") or not member.get("sha256")
            for member in members
        ):
            raise ValueError("用途包成员必须绑定 document_version_id 和 sha256")
        record = ClassificationPurposePackage(
            id=snapshot.id,
            workspace_id=snapshot.workspace_id,
            root_key=snapshot.root_key,
            source_container_id=snapshot.source_container_id,
            purpose_category_id=snapshot.purpose_category_id,
            taxonomy_key=snapshot.taxonomy_key,
            taxonomy_version=snapshot.taxonomy_version,
            policy_id=snapshot.policy_id,
            policy_version=snapshot.policy_version,
            manifest_digest=snapshot.manifest_digest,
            members_json=members,
            authorization_source=snapshot.authorization_source,
            source_request_id=snapshot.source_request_id,
        )
        self.db.add(record)
        self.db.flush()
        return record

    def get(self, package_id: str) -> ClassificationPurposePackage | None:
        """按稳定 ID 读取原始不可变快照。"""

        return self.db.get(ClassificationPurposePackage, package_id)
