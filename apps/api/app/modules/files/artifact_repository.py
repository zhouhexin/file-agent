"""文档派生件持久化仓储。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import DocumentArtifact


class DocumentArtifactRepository:
    """封装派生件查询、登记和引用计数。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def get_for_document(
        self,
        *,
        document_id: str,
        artifact_type: str,
        source_sha256: str,
        converter_config_hash: str,
    ) -> DocumentArtifact | None:
        """读取当前 Document 对应的派生件记录。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.document_id == document_id,
                DocumentArtifact.artifact_type == artifact_type,
                DocumentArtifact.source_sha256 == source_sha256,
                DocumentArtifact.converter_config_hash == converter_config_hash,
            )
            .one_or_none()
        )

    def get_reusable_physical_artifact(
        self,
        *,
        artifact_type: str,
        source_sha256: str,
        converter_config_hash: str,
    ) -> DocumentArtifact | None:
        """按源哈希全局查找可复用的物理派生文件。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.artifact_type == artifact_type,
                DocumentArtifact.source_sha256 == source_sha256,
                DocumentArtifact.converter_config_hash == converter_config_hash,
                DocumentArtifact.storage_backend == "local",
            )
            .order_by(DocumentArtifact.created_at.asc())
            .first()
        )

    def upsert_link(
        self,
        *,
        document_id: str,
        artifact_type: str,
        storage_path: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        source_sha256: str,
        converter_name: str,
        converter_version: str,
        converter_config_hash: str,
    ) -> DocumentArtifact:
        """为当前 Document 创建或更新独立派生件记录。"""

        artifact = self.get_for_document(
            document_id=document_id,
            artifact_type=artifact_type,
            source_sha256=source_sha256,
            converter_config_hash=converter_config_hash,
        )
        if artifact is None:
            artifact = DocumentArtifact(
                document_id=document_id,
                artifact_type=artifact_type,
                source_sha256=source_sha256,
                converter_config_hash=converter_config_hash,
            )
            self.db.add(artifact)
        artifact.storage_backend = "local"
        artifact.storage_path = storage_path
        artifact.content_type = content_type
        artifact.size_bytes = size_bytes
        artifact.sha256 = sha256
        artifact.converter_name = converter_name
        artifact.converter_version = converter_version
        self.db.flush()
        return artifact

    def count_by_storage_path(self, *, storage_path: str) -> int:
        """统计仍引用同一物理派生文件的记录数量。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.storage_backend == "local",
                DocumentArtifact.storage_path == storage_path,
            )
            .count()
        )

    def list_for_document(self, *, document_id: str) -> list[DocumentArtifact]:
        """返回当前 Document 的全部派生件。"""

        return self.db.query(DocumentArtifact).filter(DocumentArtifact.document_id == document_id).all()
