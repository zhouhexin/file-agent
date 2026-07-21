"""原始文件定位与解析结果持久化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document, DocumentElement, DocumentExtractionRun, DocumentPage, FileObject, utcnow
from app.modules.file_lifecycle.storage import FileLifecycleStorageService


class FileExtractionRepository:
    """封装文件读取权限、路径安全和解析结果写入。"""

    def __init__(self, db: Session, user_id: str | None) -> None:
        """保存数据库会话和当前调用用户。"""

        self.db = db
        self.user_id = user_id
        self.storage_root = Path(get_settings().file_storage_root).resolve()

    def get_original_file_metadata(self, document_id: str) -> Dict[str, Any]:
        """返回当前用户可读取的原始文件元信息。"""

        resolved = self.resolve_original_file(document_id)
        if not resolved["ok"]:
            return resolved
        document = resolved["document"]
        file_object = resolved["file_object"]
        file_path = resolved["file_path"]
        return {
            "ok": True,
            "document_id": document.id,
            "filename": document.original_filename,
            "content_type": document.content_type,
            "size_bytes": document.size_bytes,
            "sha256": document.sha256,
            "storage_backend": file_object.storage_backend,
            "exists": file_path.exists(),
        }

    def resolve_original_file(self, document_id: str) -> Dict[str, Any]:
        """校验用户权限并解析本地文件路径。"""

        if self.user_id is None:
            return _error("AUTH_REQUIRED", "缺少当前用户，不能读取文件。")
        document = self.get_document_for_current_user(document_id)
        if document is None:
            return _error("DOCUMENT_NOT_FOUND", "文件不存在或不属于当前用户。")
        return self.resolve_original_file_for_document(document)

    def get_document_for_current_user(self, document_id: str) -> Document | None:
        """只校验当前用户是否拥有文档，不要求原件必须存在。"""

        if self.user_id is None:
            return None
        return (
            self.db.query(Document)
            .filter(Document.id == document_id, Document.user_id == self.user_id)
            .one_or_none()
        )

    def resolve_original_file_for_document(self, document: Document) -> Dict[str, Any]:
        """在已校验文档归属后解析本地原件路径。"""

        file_object = (
            self.db.query(FileObject)
            .filter(
                FileObject.document_id == document.id,
                FileObject.storage_backend.in_({"local", "working_copy_local", "trash_local"}),
            )
            .order_by(FileObject.created_at.asc())
            .first()
        )
        if file_object is None:
            return _error("FILE_OBJECT_NOT_FOUND", "文件对象不存在。")
        try:
            file_path = FileLifecycleStorageService().file_object_path(file_object)
        except ValueError:
            return _error("UNSAFE_STORAGE_PATH", "文件存储路径越界，已拒绝读取。")
        if not file_path.exists():
            return _error("FILE_NOT_FOUND_ON_DISK", "本地文件不存在。")
        return {"ok": True, "document": document, "file_object": file_object, "file_path": file_path}

    def create_extraction_run(
        self,
        *,
        document_id: str,
        extractor: str,
        parser_name: str = "",
        parser_version: str = "",
        parser_config_hash: str = "",
    ) -> DocumentExtractionRun:
        """创建 RUNNING 状态的解析运行记录。"""

        run = DocumentExtractionRun(
            document_id=document_id,
            status="RUNNING",
            extractor=extractor,
            parser_name=parser_name,
            parser_version=parser_version,
            parser_config_hash=parser_config_hash,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def get_latest_successful_extraction(
        self,
        *,
        document_id: str,
        parser_config_hash: str | None = None,
    ) -> Dict[str, Any] | None:
        """读取同一文件最近一次成功解析结果，用于避免重复解析和重复写页。"""

        query = self.db.query(DocumentExtractionRun).filter(
            DocumentExtractionRun.document_id == document_id,
            DocumentExtractionRun.status == "COMPLETED",
        )
        if parser_config_hash is not None:
            query = query.filter(DocumentExtractionRun.parser_config_hash == parser_config_hash)
        run = (
            query
            .order_by(DocumentExtractionRun.updated_at.desc())
            .first()
        )
        if run is None:
            return None
        pages = (
            self.db.query(DocumentPage)
            .filter(DocumentPage.extraction_run_id == run.id)
            .order_by(DocumentPage.page_number.asc().nullslast(), DocumentPage.created_at.asc())
            .all()
        )
        if not pages:
            return None
        elements = (
            self.db.query(DocumentElement)
            .filter(DocumentElement.extraction_run_id == run.id)
            .order_by(DocumentElement.element_index.asc())
            .all()
        )
        return {"run": run, "pages": pages, "elements": elements}

    def complete_extraction_run(
        self,
        *,
        run: DocumentExtractionRun,
        pages: List[Dict[str, Any]],
        elements: List[Dict[str, Any]] | None = None,
    ) -> None:
        """写入页面文本并标记解析完成。"""

        for page in pages:
            self.db.add(
                DocumentPage(
                    document_id=run.document_id,
                    extraction_run_id=run.id,
                    page_number=page.get("page_number"),
                    sheet_name=page.get("sheet_name"),
                    text_content=page.get("text", ""),
                    metadata_json=page.get("metadata", {}),
                )
            )
        for element in elements or []:
            self.db.add(
                DocumentElement(
                    document_id=run.document_id,
                    extraction_run_id=run.id,
                    element_index=int(element.get("element_index") or 0),
                    label=str(element.get("label") or "text"),
                    text_content=str(element.get("text") or element.get("text_content") or ""),
                    page_number=element.get("page_number"),
                    bbox_json=element.get("bbox") or element.get("bbox_json") or {},
                    content_layer=str(element.get("content_layer") or "body"),
                    parent_ref=element.get("parent_ref"),
                    metadata_json=element.get("metadata") or element.get("metadata_json") or {},
                )
            )
        run.status = "COMPLETED"
        run.updated_at = utcnow()
        self.db.flush()

    def fail_extraction_run(self, *, run: DocumentExtractionRun, error_message: str) -> None:
        """标记解析运行失败。"""

        run.status = "FAILED"
        run.error_message = error_message
        run.updated_at = utcnow()
        self.db.flush()


def _is_relative_to(path: Path, root: Path) -> bool:
    """兼容 Python 3.9 的路径包含关系判断。"""

    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _error(code: str, message: str) -> Dict[str, Any]:
    """构造文件读取结构化错误。"""

    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": False,
            "user_action_required": False,
        },
    }
