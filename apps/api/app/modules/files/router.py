"""文件相关 HTTP 路由。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import get_settings
from app.db.models import DocumentArtifact, User
from app.modules.auth.dependencies import get_current_user
from app.modules.files.schemas import (
    FileDeleteResponse,
    FilePreviewResponse,
    FileUploadResponse,
    SpreadsheetPreviewResponse,
)
from app.modules.files.service import FileUploadService
from app.modules.files.extraction_repository import FileExtractionRepository

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload", response_model=FileUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_file(
    file: UploadFile = File(...),
    conversation_id: str | None = Form(default=None),
    relative_path: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileUploadResponse:
    """上传一个原始文件，并返回可用于消息附件的 document_id。"""

    return await FileUploadService(db).upload(
        file=file,
        current_user=current_user,
        conversation_id=conversation_id,
        relative_path=relative_path,
    )


@router.delete("/{document_id}", response_model=FileDeleteResponse)
def delete_file(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileDeleteResponse:
    """删除尚未进入对话的上传文件。"""

    return FileUploadService(db).delete(document_id=document_id, current_user=current_user)


@router.get("/{document_id}/content", response_class=FileResponse)
def get_file_content(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """返回原始附件内容，供前端点击预览或下载。"""

    return FileUploadService(db).get_content_response(document_id=document_id, current_user=current_user)


@router.get("/{document_id}/artifacts/{artifact_id}", response_class=FileResponse)
def download_document_artifact(
    document_id: str,
    artifact_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """下载当前用户文档的受控派生件，不接受调用方提供本地路径。"""

    extraction_repository = FileExtractionRepository(db, current_user.id)
    document = extraction_repository.get_document_for_current_user(document_id)
    artifact = db.get(DocumentArtifact, artifact_id)
    if document is None or artifact is None or artifact.document_id != document_id:
        raise HTTPException(status_code=404, detail="派生文件不存在或无权访问。")
    lifecycle = extraction_repository.resolve_original_file_for_document(document)
    if not lifecycle.get("ok"):
        error = dict(lifecycle.get("error") or {})
        if error.get("code") == "FILE_TRASHED":
            raise HTTPException(
                status_code=410,
                detail=str(error.get("message") or "文件已删除，请先恢复。"),
            )
        raise HTTPException(status_code=404, detail="派生文件不存在或无权访问。")
    allowed_artifacts = {
        "STRUCTURED_EXTRACTION_CSV": ("text/csv", ".csv"),
        "STRUCTURED_EXTRACTION_XLSX": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xlsx",
        ),
    }
    expected = allowed_artifacts.get(str(artifact.artifact_type or ""))
    if expected is None or not str(artifact.content_type or "").startswith(expected[0]):
        raise HTTPException(status_code=404, detail="派生文件不存在或无权访问。")
    storage_root = Path(get_settings().file_storage_root).resolve()
    artifact_path = (storage_root / artifact.storage_path).resolve()
    try:
        artifact_path.relative_to(storage_root)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="派生文件路径无效。") from exc
    if artifact.storage_backend != "local" or not artifact_path.is_file():
        raise HTTPException(status_code=404, detail="派生文件不存在。")
    suffix = expected[1]
    return FileResponse(
        path=artifact_path,
        media_type=artifact.content_type,
        filename=f"structured-extraction-{document_id}{suffix}",
    )


@router.get("/{document_id}/preview", response_model=FilePreviewResponse)
def preview_file(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FilePreviewResponse:
    """返回已解析正文的安全预览，供聊天文件卡点击查看。"""

    return FileUploadService(db).get_preview(
        document_id=document_id,
        current_user=current_user,
    )


@router.get("/{document_id}/spreadsheet-preview", response_model=SpreadsheetPreviewResponse)
def preview_spreadsheet(
    document_id: str,
    sheet_name: str | None = Query(default=None, max_length=255),
    row_offset: int = Query(default=0, ge=0),
    row_limit: int = Query(default=100, ge=1, le=200),
    column_offset: int = Query(default=0, ge=0),
    column_limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SpreadsheetPreviewResponse:
    """返回已解析 Excel 的结构化分页预览，不执行工作簿公式或外部资源。"""

    return FileUploadService(db).get_spreadsheet_preview(
        document_id=document_id,
        current_user=current_user,
        sheet_name=sheet_name,
        row_offset=row_offset,
        row_limit=row_limit,
        column_offset=column_offset,
        column_limit=column_limit,
    )
