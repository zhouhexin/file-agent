"""受管文件用户快照关系仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import ManagedFileSnapshot, utcnow


class ManagedFileSnapshotRepository:
    """封装受管文件快照的查询和版本状态更新。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def get_by_source_version(
        self,
        *,
        user_id: str,
        managed_file_id: str,
        source_sha256: str,
    ) -> ManagedFileSnapshot | None:
        """按用户、受管文件和内容哈希查找可复用快照。"""

        return (
            self.db.query(ManagedFileSnapshot)
            .filter(
                ManagedFileSnapshot.user_id == user_id,
                ManagedFileSnapshot.managed_file_id == managed_file_id,
                ManagedFileSnapshot.source_sha256 == source_sha256,
            )
            .one_or_none()
        )

    def create(
        self,
        *,
        user_id: str,
        managed_file_id: str,
        document_id: str,
        source_fingerprint: str,
        source_sha256: str,
        source_size_bytes: int,
        source_modified_at,
    ) -> ManagedFileSnapshot:
        """创建新版本快照，并把同一文件的旧版本标记为 SUPERSEDED。"""

        (
            self.db.query(ManagedFileSnapshot)
            .filter(
                ManagedFileSnapshot.user_id == user_id,
                ManagedFileSnapshot.managed_file_id == managed_file_id,
                ManagedFileSnapshot.status == "ACTIVE",
            )
            .update(
                {"status": "SUPERSEDED", "updated_at": utcnow()},
                synchronize_session=False,
            )
        )
        snapshot = ManagedFileSnapshot(
            user_id=user_id,
            managed_file_id=managed_file_id,
            document_id=document_id,
            source_fingerprint=source_fingerprint,
            source_sha256=source_sha256,
            source_size_bytes=source_size_bytes,
            source_modified_at=source_modified_at,
            status="ACTIVE",
        )
        self.db.add(snapshot)
        self.db.flush()
        return snapshot
