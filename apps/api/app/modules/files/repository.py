"""文件模块持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Document, FileObject


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
