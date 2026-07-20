"""文件上传业务服务。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document, User
from app.modules.files.artifact_repository import DocumentArtifactRepository
from app.modules.files.repository import FileRepository
from app.modules.files.schemas import FileDeleteResponse, FileUploadResponse


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
        storage_root = Path(get_settings().file_storage_root)
        existing_document = self.repository.get_reusable_draft_document(
            user_id=current_user.id,
            workspace_id=current_user.default_workspace_id,
            sha256=sha256,
            original_filename=filename,
        )
        if existing_document is not None:
            return self._to_upload_response(existing_document, deduplicated=True)

        existing_file_object = self.repository.get_existing_file_object_by_hash(
            sha256=sha256,
            size_bytes=len(content),
        )
        reusable_storage_path = (
            existing_file_object.storage_path
            if existing_file_object and (storage_root / existing_file_object.storage_path).exists()
            else None
        )
        document = self.repository.create_document(
            user_id=current_user.id,
            workspace_id=current_user.default_workspace_id,
            original_filename=filename,
            content_type=content_type,
            size_bytes=len(content),
            sha256=sha256,
        )
        relative_path = reusable_storage_path or self._write_local_file(
            document=document,
            filename=filename,
            content=content,
        )
        self.repository.create_file_object(
            document_id=document.id,
            storage_path=relative_path,
            size_bytes=len(content),
            sha256=sha256,
        )
        self._run_deterministic_ingest(document=document, content=content)
        self.db.commit()
        self.db.refresh(document)
        return self._to_upload_response(document=document, deduplicated=reusable_storage_path is not None)

    def _to_upload_response(self, document: Document, deduplicated: bool) -> FileUploadResponse:
        """把 Document 转换为上传响应。"""

        return FileUploadResponse(
            document_id=document.id,
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            status=document.status,
            ingest_status=document.ingest_status,
            deduplicated=deduplicated,
        )

    def delete(self, document_id: str, current_user: User) -> FileDeleteResponse:
        """删除尚未进入对话的上传文件。"""

        document = self.repository.get_document_for_user(document_id=document_id, user_id=current_user.id)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        if document.status != "UPLOADED":
            raise HTTPException(status_code=409, detail="Document already used in a message")

        storage_root = Path(get_settings().file_storage_root)
        artifact_repository = DocumentArtifactRepository(self.db)
        artifacts = artifact_repository.list_for_document(document_id=document.id)
        for artifact in artifacts:
            if artifact.storage_backend != "local":
                continue
            artifact_path = self._resolve_local_storage_path(
                storage_root=storage_root,
                storage_path=artifact.storage_path,
            )
            if artifact_path is None:
                continue
            reference_count = artifact_repository.count_by_storage_path(storage_path=artifact.storage_path)
            if reference_count <= 1:
                artifact_path.unlink(missing_ok=True)
                self._remove_empty_parent_dirs(artifact_path.parent, stop_at=storage_root)

        file_objects = self.repository.list_file_objects(document_id=document.id)
        for file_object in file_objects:
            # 只删除本地存储文件；后续接对象存储时这里应抽成 StorageService。
            if file_object.storage_backend == "local":
                file_path = storage_root / file_object.storage_path
                reference_count = self.repository.count_file_objects_by_storage_path(
                    storage_backend=file_object.storage_backend,
                    storage_path=file_object.storage_path,
                )
                if reference_count <= 1:
                    file_path.unlink(missing_ok=True)
                    self._remove_empty_parent_dirs(file_path.parent, stop_at=storage_root)

        self.repository.delete_document_with_objects(document)
        self.db.commit()
        return FileDeleteResponse(deleted=True)

    @staticmethod
    def _resolve_local_storage_path(*, storage_root: Path, storage_path: str) -> Path | None:
        """把相对存储路径限制在本地存储根目录内。"""

        resolved_root = storage_root.resolve()
        candidate = (resolved_root / storage_path).resolve()
        if candidate == resolved_root or resolved_root not in candidate.parents:
            return None
        return candidate

    def get_content_response(self, document_id: str, current_user: User) -> FileResponse:
        """按 document_id 返回原始文件内容。

        当前用于对话附件点击查看，必须校验 Document.user_id，避免跨用户读取附件。
        """

        document = self.repository.get_document_for_user(document_id=document_id, user_id=current_user.id)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")

        file_object = next(
            (item for item in self.repository.list_file_objects(document_id=document.id) if item.storage_backend == "local"),
            None,
        )
        if file_object is None:
            raise HTTPException(status_code=404, detail="File object not found")

        file_path = Path(get_settings().file_storage_root) / file_object.storage_path
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Stored file not found")

        return FileResponse(
            path=file_path,
            media_type=document.content_type,
            filename=document.original_filename,
        )

    def _write_local_file(self, *, document: Document, filename: str, content: bytes) -> str:
        """把原始文件写入本地存储目录，并返回相对存储路径。"""

        storage_root = Path(get_settings().file_storage_root)
        relative_path = Path(document.user_id) / document.id / filename
        target_path = storage_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(content)
        return relative_path.as_posix()

    @staticmethod
    def _remove_empty_parent_dirs(start_dir: Path, *, stop_at: Path) -> None:
        """删除空父目录，但不能越过文件存储根目录。"""

        stop_at = stop_at.resolve()
        current_dir = start_dir.resolve()
        while current_dir != stop_at and stop_at in current_dir.parents:
            try:
                current_dir.rmdir()
            except OSError:
                break
            current_dir = current_dir.parent

    def _run_deterministic_ingest(self, *, document: Document, content: bytes) -> None:
        """上传后执行固定 ingest：分类并提取关键词信息。"""

        document.ingest_status = "INGESTING"
        text = content.decode("utf-8", errors="ignore")
        keywords = self._extract_keywords(text=text, filename=document.original_filename)
        labels = self._classify_document(filename=document.original_filename, content_type=document.content_type)
        summary = f"文件 {document.original_filename} 已完成基础处理，识别标签 {', '.join(labels)}。"
        self.repository.create_or_update_insight(
            document_id=document.id,
            keywords=keywords,
            labels=labels,
            summary=summary,
        )
        document.ingest_status = "INGESTED"
        self.db.flush()

    @staticmethod
    def _extract_keywords(*, text: str, filename: str) -> list[str]:
        """使用确定性规则提取文件名和文本中的关键词。"""

        tokens = re.findall(r"[\w\u4e00-\u9fff]+", f"{filename} {text}".lower())
        seen: set[str] = set()
        keywords: list[str] = []
        for token in tokens:
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            keywords.append(token)
            if len(keywords) >= 10:
                break
        return keywords

    @staticmethod
    def _classify_document(*, filename: str, content_type: str) -> list[str]:
        """使用确定性规则生成基础文件分类标签。"""

        lowered_name = filename.lower()
        labels = ["uploaded-document"]
        if content_type.startswith("image/"):
            labels.append("image")
        elif any(lowered_name.endswith(ext) for ext in [".xls", ".xlsx", ".csv"]):
            labels.append("spreadsheet")
        elif any(lowered_name.endswith(ext) for ext in [".pdf", ".doc", ".docx", ".txt", ".md"]):
            labels.append("text-document")
        else:
            labels.append("other-file")
        return labels
