"""文件模块持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Document, DocumentInsight, FileObject, utcnow


class FileRepository:
    """封装 Document 和 FileObject 的数据库写入。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def create_document(
        self,
        *,
        user_id: str,
        workspace_id: str | None,
        original_filename: str,
        content_type: str,
        size_bytes: int,
        sha256: str,
    ) -> Document:
        """创建用户上传文件对应的 Document。"""

        document = Document(
            user_id=user_id,
            workspace_id=workspace_id,
            original_filename=original_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            sha256=sha256,
            status="UPLOADED",
        )
        self.db.add(document)
        self.db.flush()
        return document

    def create_file_object(
        self,
        *,
        document_id: str,
        storage_path: str,
        size_bytes: int,
        sha256: str,
    ) -> FileObject:
        """创建本地文件对象记录。"""

        file_object = FileObject(
            document_id=document_id,
            storage_backend="local",
            storage_path=storage_path,
            size_bytes=size_bytes,
            sha256=sha256,
        )
        self.db.add(file_object)
        self.db.flush()
        return file_object

    def get_document_for_user(self, document_id: str, user_id: str) -> Document | None:
        """按 document_id 和 user_id 查询当前用户自己的 Document。"""

        return (
            self.db.query(Document)
            .filter(Document.id == document_id, Document.user_id == user_id)
            .one_or_none()
        )

    def get_document(self, document_id: str) -> Document | None:
        """按 document_id 查询 Document，不限制所属用户。"""

        return self.db.get(Document, document_id)

    def list_file_objects(self, document_id: str) -> list[FileObject]:
        """查询 Document 对应的所有文件对象。"""

        return self.db.query(FileObject).filter(FileObject.document_id == document_id).all()

    def get_existing_file_object_by_hash(self, *, sha256: str, size_bytes: int) -> FileObject | None:
        """按 sha256 和大小全局查找可复用的本地文件对象。"""

        return (
            self.db.query(FileObject)
            .filter(
                FileObject.sha256 == sha256,
                FileObject.size_bytes == size_bytes,
                FileObject.storage_backend == "local",
            )
            .order_by(FileObject.created_at.asc())
            .first()
        )

    def count_file_objects_by_storage_path(self, *, storage_backend: str, storage_path: str) -> int:
        """统计同一底层存储对象仍被多少 FileObject 引用。"""

        return (
            self.db.query(FileObject)
            .filter(
                FileObject.storage_backend == storage_backend,
                FileObject.storage_path == storage_path,
            )
            .count()
        )

    def get_reusable_draft_document(
        self,
        *,
        user_id: str,
        workspace_id: str | None,
        sha256: str,
        original_filename: str,
    ) -> Document | None:
        """查找可幂等复用的同名未发送草稿。

        Document 承担文件名和消息生命周期，不能因为内容哈希相同就复用已经进入消息的
        Document 或受管文件快照；跨文档去重只能发生在 FileObject 物理对象层。
        """

        query = self.db.query(Document).filter(
            Document.user_id == user_id,
            Document.sha256 == sha256,
            Document.original_filename == original_filename,
            Document.status == "UPLOADED",
        )
        if workspace_id is None:
            query = query.filter(Document.workspace_id.is_(None))
        else:
            query = query.filter(Document.workspace_id == workspace_id)
        return query.order_by(Document.created_at.desc()).first()

    def create_or_update_insight(
        self,
        *,
        document_id: str,
        keywords: list[str],
        labels: list[str],
        summary: str,
    ) -> DocumentInsight:
        """创建或更新 deterministic ingest 的基础洞察结果。"""

        insight = (
            self.db.query(DocumentInsight)
            .filter(DocumentInsight.document_id == document_id)
            .one_or_none()
        )
        if insight is None:
            insight = DocumentInsight(
                document_id=document_id,
                keywords_json=keywords,
                labels_json=labels,
                summary=summary,
                extracted_at=utcnow(),
            )
            self.db.add(insight)
        else:
            insight.keywords_json = keywords
            insight.labels_json = labels
            insight.summary = summary
            insight.extracted_at = utcnow()
        self.db.flush()
        return insight

    def delete_document_with_objects(self, document: Document) -> None:
        """删除 Document、派生件及其 FileObject 记录。"""

        for insight in list(document.insights):
            self.db.delete(insight)
        for artifact in list(document.artifacts):
            self.db.delete(artifact)
        for file_object in self.list_file_objects(document.id):
            self.db.delete(file_object)
        self.db.delete(document)
        self.db.flush()

    def lock_documents_for_message(
        self,
        *,
        document_ids: list[str],
        user_id: str,
        conversation_id: str,
        message_id: str,
    ) -> None:
        """把本次消息引用的文件标记为已进入对话。"""

        if not document_ids:
            return

        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        found_ids = {document.id for document in documents}
        missing_ids = set(document_ids) - found_ids
        if missing_ids:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Document not found")

        for document in documents:
            if document.status == "USED_IN_MESSAGE":
                continue
            if document.status != "UPLOADED":
                from fastapi import HTTPException

                raise HTTPException(status_code=409, detail="Document already used in a message")
            document.status = "USED_IN_MESSAGE"
            document.locked_at = utcnow()
            document.locked_message_id = message_id
            document.locked_conversation_id = conversation_id
        self.db.flush()
