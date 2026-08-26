"""文档派生件持久化仓储。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
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

    def get_for_version(
        self,
        *,
        document_version_id: str,
        artifact_type: str,
        source_sha256: str,
        converter_config_hash: str,
    ) -> DocumentArtifact | None:
        """读取明确内容版本对应的派生件记录。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.document_version_id == document_version_id,
                DocumentArtifact.artifact_type == artifact_type,
                DocumentArtifact.source_sha256 == source_sha256,
                DocumentArtifact.converter_config_hash == converter_config_hash,
            )
            .one_or_none()
        )

    def get_latest_for_version_source(
        self,
        *,
        document_version_id: str,
        artifact_type: str,
        source_sha256: str,
    ) -> DocumentArtifact | None:
        """转换器不可用时读取当前版本最近的同规则派生件候选。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.document_version_id == document_version_id,
                DocumentArtifact.artifact_type == artifact_type,
                DocumentArtifact.source_sha256 == source_sha256,
            )
            .order_by(DocumentArtifact.created_at.desc())
            .first()
        )

    def get_latest_reusable_source_artifact(
        self,
        *,
        artifact_type: str,
        source_sha256: str,
    ) -> DocumentArtifact | None:
        """转换器不可用时按源内容查找最近的物理派生件候选。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.artifact_type == artifact_type,
                DocumentArtifact.source_sha256 == source_sha256,
                DocumentArtifact.storage_backend == "local",
            )
            .order_by(DocumentArtifact.created_at.desc())
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
        metadata_json: dict | None = None,
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
        artifact.metadata_json = dict(metadata_json or {})
        self.db.flush()
        return artifact

    def upsert_version_link(
        self,
        *,
        document_id: str,
        document_version_id: str,
        artifact_type: str,
        storage_path: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
        source_sha256: str,
        converter_name: str,
        converter_version: str,
        converter_config_hash: str,
        metadata_json: dict,
    ) -> DocumentArtifact:
        """为内容版本登记独立引用，并用 savepoint 收敛并发创建竞争。"""

        artifact = self.get_for_version(
            document_version_id=document_version_id,
            artifact_type=artifact_type,
            source_sha256=source_sha256,
            converter_config_hash=converter_config_hash,
        )
        if artifact is None:
            candidate = DocumentArtifact(
                document_id=document_id,
                document_version_id=document_version_id,
                artifact_type=artifact_type,
                storage_backend="local",
                storage_path=storage_path,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                source_sha256=source_sha256,
                converter_name=converter_name,
                converter_version=converter_version,
                converter_config_hash=converter_config_hash,
                metadata_json=dict(metadata_json),
            )
            try:
                # 唯一约束冲突只回滚本次引用创建，不能使整个 AgentRun 事务失效。
                with self.db.begin_nested():
                    self.db.add(candidate)
                    self.db.flush()
                artifact = candidate
            except IntegrityError:
                artifact = self.get_for_version(
                    document_version_id=document_version_id,
                    artifact_type=artifact_type,
                    source_sha256=source_sha256,
                    converter_config_hash=converter_config_hash,
                )
                if artifact is None:
                    raise
        artifact.document_id = document_id
        artifact.document_version_id = document_version_id
        artifact.storage_backend = "local"
        artifact.storage_path = storage_path
        artifact.content_type = content_type
        artifact.size_bytes = size_bytes
        artifact.sha256 = sha256
        artifact.converter_name = converter_name
        artifact.converter_version = converter_version
        artifact.metadata_json = dict(metadata_json)
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

    def list_for_version(self, *, document_version_id: str) -> list[DocumentArtifact]:
        """返回明确内容版本的全部派生件引用。"""

        return (
            self.db.query(DocumentArtifact)
            .filter(DocumentArtifact.document_version_id == document_version_id)
            .all()
        )

    def update_physical_facts(
        self,
        *,
        storage_path: str,
        size_bytes: int,
        sha256: str,
    ) -> None:
        """原子替换共享物理文件后同步全部引用的大小和哈希事实。"""

        (
            self.db.query(DocumentArtifact)
            .filter(
                DocumentArtifact.storage_backend == "local",
                DocumentArtifact.storage_path == storage_path,
            )
            .update(
                {
                    DocumentArtifact.size_bytes: size_bytes,
                    DocumentArtifact.sha256: sha256,
                },
                synchronize_session="fetch",
            )
        )
