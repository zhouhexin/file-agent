"""不可变分类用途包快照的持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import ClassificationPurposePackage
from app.modules.classification.purpose_policy import (
    PurposePackageMember,
    PurposePackageSnapshot,
)


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

    def find_by_manifest_digest(
        self, manifest_digest: str
    ) -> ClassificationPurposePackage | None:
        """按不可变成员摘要复用同一快照，避免 worker 重试生成重复包。"""

        return (
            self.db.query(ClassificationPurposePackage)
            .filter(ClassificationPurposePackage.manifest_digest == manifest_digest)
            .first()
        )

    def find_for_member(
        self,
        *,
        document_version_id: str,
        sha256: str,
        taxonomy_key: str,
        taxonomy_version: str,
    ) -> PurposePackageSnapshot | None:
        """查找包含当前固定版本和哈希的最新有效用途包。"""

        rows = (
            self.db.query(ClassificationPurposePackage)
            .filter(
                ClassificationPurposePackage.taxonomy_key == taxonomy_key,
                ClassificationPurposePackage.taxonomy_version == taxonomy_version,
            )
            .order_by(ClassificationPurposePackage.created_at.desc())
            .all()
        )
        for row in rows:
            if any(
                str(item.get("document_version_id") or "") == document_version_id
                and str(item.get("sha256") or "").casefold() == sha256.casefold()
                for item in (row.members_json or [])
                if isinstance(item, dict)
            ):
                return self.to_snapshot(row)
        return None

    @staticmethod
    def to_snapshot(record: ClassificationPurposePackage) -> PurposePackageSnapshot:
        """把持久化行恢复为严格校验的运行时快照。"""

        return PurposePackageSnapshot(
            id=record.id,
            workspace_id=record.workspace_id,
            root_key=record.root_key,
            source_container_id=record.source_container_id,
            purpose_category_id=record.purpose_category_id,
            taxonomy_key=record.taxonomy_key,
            taxonomy_version=record.taxonomy_version,
            policy_id=record.policy_id,
            policy_version=record.policy_version,
            manifest_digest=record.manifest_digest,
            members=tuple(
                PurposePackageMember.model_validate(item)
                for item in (record.members_json or [])
            ),
            authorization_source=record.authorization_source,
            source_request_id=record.source_request_id,
        )
