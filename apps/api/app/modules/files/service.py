"""文件上传业务服务。"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Document, User, WorkingCopy
from app.modules.file_lifecycle.service import UploadLifecycleService
from app.modules.file_lifecycle.storage import FileLifecycleStorageService
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.modules.conversations.repository import ConversationRepository
from app.modules.files.artifact_repository import DocumentArtifactRepository
from app.modules.files.extraction_repository import FileExtractionRepository
from app.modules.files.repository import FileRepository
from app.modules.files.schemas import (
    FileDeleteResponse,
    FilePreviewResponse,
    FilePreviewSection,
    FileUploadResponse,
    SpreadsheetCellPreview,
    SpreadsheetPreviewResponse,
    SpreadsheetSelectedSheet,
    SpreadsheetSheetSummary,
)
from app.modules.files.content_types import infer_content_type


class FileUploadService:
    """处理用户文件落盘和数据库记录创建。"""

    def __init__(self, db: Session) -> None:
        """注入数据库会话。"""

        self.db = db
        self.repository = FileRepository(db)

    async def upload(
        self,
        file: UploadFile,
        current_user: User,
        conversation_id: str | None = None,
    ) -> FileUploadResponse:
        """只保存上传暂存；用户发送请求前不得创建任何处理任务。

        上传请求不得执行或排队查重、归档、导入或分类，也不能因为哈希相同直接复用其他
        Document。文件夹选择只是一种浏览器选取方式，目录相对路径不进入后端数据。
        """

        filename = Path(file.filename or "uploaded-file").name
        # 浏览器可能把合法图片上报为 application/octet-stream；统一推断可避免同一文件在
        # 上传、受管目录导入和工作副本导入三条链路中得到不同 MIME。
        content_type = infer_content_type(
            filename=filename,
            declared_content_type=file.content_type,
        )
        self._validate_upload_metadata(filename=filename, content_type=content_type)
        incoming_path, size_bytes, sha256 = await self._stream_upload_to_quarantine(file=file)
        relative_path: str | None = None
        try:
            if conversation_id:
                # 聊天页允许先选附件、后发送文字；因此上传必须在同一事务中创建
                # 当前用户的空会话，不能把尚未创建的前端会话 ID 直接写入外键。
                ConversationRepository(self.db).ensure_conversation(
                    conversation_id=conversation_id,
                    user_id=current_user.id,
                )
            document = self.repository.create_document(
                user_id=current_user.id,
                workspace_id=current_user.default_workspace_id,
                original_filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
            )
            relative_path = self._publish_quarantine_file(
                document=document,
                filename=filename,
                incoming_path=incoming_path,
            )
            self.repository.create_file_object(
                document_id=document.id,
                storage_path=relative_path,
                size_bytes=size_bytes,
                sha256=sha256,
            )
            version, archive, review = UploadLifecycleService(self.db).register_upload(
                document=document,
                storage_path=relative_path,
                conversation_id=conversation_id,
            )
            document.ingest_status = "STAGED"
            self.db.commit()
        except Exception:
            self.db.rollback()
            incoming_path.unlink(missing_ok=True)
            if relative_path:
                (Path(get_settings().file_storage_root) / relative_path).unlink(missing_ok=True)
            raise
        self.db.refresh(document)
        return self._to_upload_response(
            document=document,
            version_id=version.id,
            review_id=review.id,
            job_id=None,
            archive_status=archive.status,
            review_status=review.status,
            relative_path=None,
        )

    def _to_upload_response(
        self,
        *,
        document: Document,
        version_id: str,
        review_id: str,
        job_id: str | None,
        archive_status: str,
        review_status: str,
        relative_path: str | None,
    ) -> FileUploadResponse:
        """把 Document 转换为上传响应。"""

        return FileUploadResponse(
            document_id=document.id,
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            status=document.status,
            ingest_status=document.ingest_status,
            deduplicated=False,
            upload_document_version_id=version_id,
            duplicate_review_id=review_id,
            filesystem_job_id=job_id,
            archive_status=archive_status,
            duplicate_review_status=review_status,
            relative_path=relative_path,
        )

    def delete(self, document_id: str, current_user: User) -> FileDeleteResponse:
        """只允许上传所有者取消尚未进入对话的私有暂存文件。

        共享 ``ACTIVE`` 工作副本虽然可以被所有登录用户读取，但绝不能经过上传
        暂存删除接口处理；共享文件删除必须走确认后的 OperationPlan。
        """

        document = self.repository.get_document_for_user(
            document_id=document_id,
            user_id=current_user.id,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        if document.status == "USED_IN_MESSAGE":
            raise HTTPException(status_code=409, detail="Document already used in a message")
        if document.status not in {"UPLOADED", "UPLOAD_CANCELLED"}:
            raise HTTPException(status_code=409, detail="Document is not an upload draft")
        if document.status == "UPLOAD_CANCELLED":
            return FileDeleteResponse(deleted=True)
        cleanup_job = UploadLifecycleService(self.db).cancel_unsent_upload(document=document)
        document.status = "UPLOAD_CANCELLED"
        self.db.commit()
        return FileDeleteResponse(
            deleted=True,
            cleanup_job_id=cleanup_job.id if cleanup_job else None,
        )

    @staticmethod
    def _resolve_local_storage_path(*, storage_root: Path, storage_path: str) -> Path | None:
        """把相对存储路径限制在本地存储根目录内。"""

        resolved_root = storage_root.resolve()
        candidate = (resolved_root / storage_path).resolve()
        if candidate == resolved_root or resolved_root not in candidate.parents:
            return None
        return candidate

    def get_content_response(self, document_id: str, current_user: User) -> FileResponse:
        """按 document_id 返回私有上传附件或共享活动工作副本内容。

        其他用户的上传暂存 Document 仍不可读；共享权限只来自活动工作副本关系。
        """

        document = self._get_readable_document(
            document_id=document_id,
            current_user=current_user,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        lifecycle = FileExtractionRepository(
            self.db,
            current_user.id,
        ).resolve_original_file_for_document(document)
        if not lifecycle.get("ok"):
            error = lifecycle.get("error") or {}
            if error.get("code") == "FILE_TRASHED":
                raise HTTPException(status_code=410, detail=str(error.get("message") or "文件已删除，请先恢复。"))
            raise HTTPException(status_code=404, detail=str(error.get("message") or "Stored file not found"))

        file_object = next(
            (
                item
                for item in self.repository.list_file_objects(document_id=document.id)
                if item.storage_backend in {"local", "working_copy_local", "trash_local"}
            ),
            None,
        )
        if file_object is None:
            raise HTTPException(status_code=404, detail="File object not found")

        try:
            file_path = FileLifecycleStorageService().file_object_path(file_object)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Stored file not found") from exc
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Stored file not found")

        return FileResponse(
            path=file_path,
            media_type=document.content_type,
            filename=document.original_filename,
        )

    def get_preview(
        self,
        *,
        document_id: str,
        current_user: User,
        max_chars: int = 100_000,
    ) -> FilePreviewResponse:
        """返回私有上传附件或共享活动工作副本的受控正文预览。

        Office 文件不能依赖浏览器原生渲染，因此只返回已经持久化的 ``document_pages`` 文本。
        预览不能触发临时解析、不能读取其他用户上传暂存文件，也不能返回存储路径或解析器内部信息。
        """

        document = self._get_readable_document(
            document_id=document_id,
            current_user=current_user,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        lifecycle = FileExtractionRepository(
            self.db,
            current_user.id,
        ).resolve_original_file_for_document(document)
        if not lifecycle.get("ok"):
            error = lifecycle.get("error") or {}
            if error.get("code") == "FILE_TRASHED":
                raise HTTPException(status_code=410, detail=str(error.get("message") or "文件已删除，请先恢复。"))
            raise HTTPException(status_code=404, detail=str(error.get("message") or "文件不可读取"))
        extraction = FileExtractionRepository(
            self.db,
            current_user.id,
        ).get_latest_successful_extraction(document_id=document.id)
        if extraction is None:
            raise HTTPException(status_code=409, detail="文件正文尚未完成解析，暂时无法预览")

        sections: list[FilePreviewSection] = []
        remaining = max(1, max_chars)
        truncated = False
        for page in extraction["pages"]:
            text = str(page.text_content or "")
            if not text:
                continue
            visible_text = text[:remaining]
            sections.append(
                FilePreviewSection(
                    page_number=page.page_number,
                    sheet_name=page.sheet_name,
                    text=visible_text,
                )
            )
            remaining -= len(visible_text)
            if len(visible_text) < len(text) or remaining <= 0:
                truncated = True
                break
        return FilePreviewResponse(
            document_id=document.id,
            filename=document.original_filename,
            content_type=document.content_type,
            sections=sections,
            truncated=truncated,
        )

    def get_spreadsheet_preview(
        self,
        *,
        document_id: str,
        current_user: User,
        sheet_name: str | None,
        row_offset: int,
        row_limit: int,
        column_offset: int,
        column_limit: int,
    ) -> SpreadsheetPreviewResponse:
        """按真实 Sheet 和单元格坐标返回已持久化的 Excel 结构化预览。

        接口只读取成功解析运行中的 ``table_cell`` 元素。浏览器上传草稿的即时预览仍在
        本地 Worker 中完成，不能借此接口绕过文件生命周期或读取其他用户暂存文件。
        """

        document = self._get_readable_document(
            document_id=document_id,
            current_user=current_user,
        )
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        lifecycle = FileExtractionRepository(
            self.db,
            current_user.id,
        ).resolve_original_file_for_document(document)
        if not lifecycle.get("ok"):
            error = lifecycle.get("error") or {}
            if error.get("code") == "FILE_TRASHED":
                raise HTTPException(status_code=410, detail=str(error.get("message") or "文件已删除，请先恢复。"))
            raise HTTPException(status_code=404, detail=str(error.get("message") or "文件不可读取"))

        extraction = FileExtractionRepository(
            self.db,
            current_user.id,
        ).get_latest_successful_extraction(document_id=document.id)
        if extraction is None:
            raise HTTPException(status_code=409, detail="Excel 尚未完成结构化解析")
        sheet_pages = [page for page in extraction["pages"] if page.sheet_name]
        if not sheet_pages:
            raise HTTPException(status_code=409, detail="该文件没有可用的 Excel 结构化解析结果")
        selected_page = next(
            (page for page in sheet_pages if page.sheet_name == sheet_name),
            sheet_pages[0] if sheet_name is None else None,
        )
        if selected_page is None:
            raise HTTPException(status_code=404, detail="工作表不存在")

        metadata = dict(selected_page.metadata_json or {})
        selected_name = str(selected_page.sheet_name)
        cells: list[SpreadsheetCellPreview] = []
        row_start = row_offset + 1
        row_end = row_offset + row_limit
        column_start = column_offset + 1
        column_end = column_offset + column_limit
        for element in extraction["elements"]:
            if element.label != "table_cell":
                continue
            cell = dict(element.metadata_json or {})
            if str(cell.get("sheet_name") or "") != selected_name:
                continue
            row = int(cell.get("row") or 0)
            column = int(cell.get("column") or 0)
            if not (row_start <= row <= row_end and column_start <= column <= column_end):
                continue
            cells.append(
                SpreadsheetCellPreview(
                    row=row,
                    column=column,
                    address=str(cell.get("address") or ""),
                    raw_value=cell.get("raw_value"),
                    display_value=str(cell.get("display_value") or element.text_content or ""),
                    value_type=str(cell.get("value_type") or "string"),
                    formula=cell.get("formula"),
                    cached_result=cell.get("cached_result"),
                    cached_result_available=bool(cell.get("cached_result_available")),
                    number_format=cell.get("number_format"),
                    merge_range=cell.get("merge_range"),
                )
            )
        cells.sort(key=lambda item: (item.row, item.column))

        sheet_summaries = []
        for page in sheet_pages:
            page_metadata = dict(page.metadata_json or {})
            sheet_summaries.append(
                SpreadsheetSheetSummary(
                    name=str(page.sheet_name),
                    row_count=int(page_metadata.get("max_row") or 0),
                    column_count=int(page_metadata.get("max_column") or 0),
                    hidden=str(page_metadata.get("sheet_state") or "visible") != "visible",
                )
            )
        warnings = [str(item) for item in metadata.get("warnings") or []]
        structure_complete = bool(metadata.get("structure_complete", True))
        return SpreadsheetPreviewResponse(
            document_id=document.id,
            filename=document.original_filename,
            sheets=sheet_summaries,
            selected_sheet=SpreadsheetSelectedSheet(
                name=selected_name,
                row_count=int(metadata.get("max_row") or 0),
                column_count=int(metadata.get("max_column") or 0),
                row_offset=row_offset,
                row_limit=row_limit,
                column_offset=column_offset,
                column_limit=column_limit,
                merged_ranges=[str(item) for item in metadata.get("merged_ranges") or []],
                hidden_rows=[int(item) for item in metadata.get("hidden_rows") or []],
                hidden_columns=[str(item) for item in metadata.get("hidden_columns") or []],
                freeze_panes=metadata.get("freeze_panes"),
                structure_complete=structure_complete,
                cells=cells,
            ),
            truncated=not structure_complete,
            warnings=warnings,
        )

    def _get_readable_document(
        self,
        *,
        document_id: str,
        current_user: User,
    ) -> Document | None:
        """读取私有上传附件或共享活动工作副本文档。

        共享权限只由 ``WorkingCopy.workspace_id + ACTIVE`` 授予；仅知道其他用户
        上传暂存 Document 的 ID 不能读取隔离区原件。
        """

        shared_copies = (
            self.db.query(WorkingCopy)
            .filter(
                WorkingCopy.document_id == document_id,
                WorkingCopy.workspace_id == get_shared_workspace_id(self.db),
            )
            .all()
        )
        if shared_copies:
            # 文件一旦形成共享工作副本，读取状态就必须以共享事实为准。
            # 即使当前用户最初上传了文件，也不能绕过 TRASHED 状态读取历史原件。
            if not any(copy.status == "ACTIVE" for copy in shared_copies):
                return None
            return self.db.get(Document, document_id)
        return self.repository.get_document_for_user(
            document_id=document_id,
            user_id=current_user.id,
        )

    async def _stream_upload_to_quarantine(self, *, file: UploadFile) -> tuple[Path, int, str]:
        """分块写入受控临时区并计算哈希，避免把整个文件一次性读入内存。"""

        settings = get_settings()
        storage_root = Path(settings.file_storage_root).resolve()
        incoming_dir = storage_root / ".incoming"
        incoming_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = settings.upload_max_file_size_mb * 1024 * 1024
        digest = hashlib.sha256()
        size_bytes = 0
        descriptor, temp_name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=incoming_dir)
        incoming_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    chunk = await file.read(settings.upload_chunk_size_bytes)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > max_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"文件超过当前部署允许的 {settings.upload_max_file_size_mb} MB 资源上限",
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if size_bytes == 0:
                raise HTTPException(status_code=400, detail="不能上传空文件")
            return incoming_path, size_bytes, digest.hexdigest()
        except Exception:
            incoming_path.unlink(missing_ok=True)
            raise

    def _publish_quarantine_file(
        self,
        *,
        document: Document,
        filename: str,
        incoming_path: Path,
    ) -> str:
        """把已完整接收的临时文件原子提交到 Document 私有上传暂存目录。"""

        storage_root = Path(get_settings().file_storage_root)
        relative_path = Path(document.user_id) / document.id / filename
        target_path = storage_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            raise FileExistsError("上传暂存目标已存在")
        os.replace(incoming_path, target_path)
        return relative_path.as_posix()

    @staticmethod
    def _validate_upload_metadata(*, filename: str, content_type: str) -> None:
        """执行基础扩展名和显式危险 MIME 检查，但不得宣称已经完成病毒扫描。"""

        settings = get_settings()
        suffix = Path(filename).suffix.lower()
        if suffix not in set(settings.upload_allowed_extensions):
            raise HTTPException(status_code=415, detail=f"暂不支持上传 {suffix or '无扩展名'} 文件")
        dangerous_mime_types = {
            "application/x-msdownload",
            "application/x-dosexec",
            "application/x-executable",
            "application/x-sh",
        }
        if content_type.lower() in dangerous_mime_types:
            raise HTTPException(status_code=415, detail="上传内容类型存在可执行风险，已拒绝接收")

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
