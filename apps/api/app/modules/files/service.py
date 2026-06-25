"""文件上传业务服务。"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document, User
from app.modules.files.repository import FileRepository
from app.modules.files.schemas import FileUploadResponse


class FileUploadService:
    """处理用户文件落盘和数据库记录创建。"""

    def __init__(self, db: Session) -> None:
        """注入数据库会话。"""

        self.db = db
        self.repository = FileRepository(db)

    async def upload(self, file: UploadFile, current_user: User) -> FileUploadResponse:
        """保存上传文件，并创建 Document 和 FileObject。"""

        content = await file.read()
        filename = Path(file.filename or "uploaded-file").name
        content_type = file.content_type or "application/octet-stream"
        sha256 = hashlib.sha256(content).hexdigest()

        document = self.repository.create_document(
            user_id=current_user.id,
            workspace_id=current_user.default_workspace_id,
            original_filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=sha256,
        )
        relative_path = self._write_local_file(document=document, filename=filename, content=content)
        self.repository.create_file_object(
            document_id=document.id,
            storage_path=relative_path,
            size_bytes=len(content),
            sha256=sha256,
        )
        self.db.commit()
        self.db.refresh(document)

        return FileUploadResponse(
            document_id=document.id,
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            status=document.status,
        )

    def _write_local_file(self, *, document: Document, filename: str, content: bytes) -> str:
        """把原始文件写入本地存储目录，并返回相对存储路径。"""

        storage_root = Path(get_settings().file_storage_root)
        relative_path = Path(document.user_id) / document.id / filename
        target_path = storage_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)
        return relative_path.as_posix()
