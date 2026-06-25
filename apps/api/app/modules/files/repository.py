"""文件模块持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Document, FileObject, utcnow


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

    def list_file_objects(self, document_id: str) -> list[FileObject]:
        """查询 Document 对应的所有文件对象。"""

        return self.db.query(FileObject).filter(FileObject.document_id == document_id).all()

    def delete_document_with_objects(self, document: Document) -> None:
        """删除 Document 及其 FileObject 记录。"""

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
            if document.status != "UPLOADED":
                from fastapi import HTTPException

                raise HTTPException(status_code=409, detail="Document already used in a message")
            document.status = "USED_IN_MESSAGE"
            document.locked_at = utcnow()
            document.locked_message_id = message_id
            document.locked_conversation_id = conversation_id
        self.db.flush()
