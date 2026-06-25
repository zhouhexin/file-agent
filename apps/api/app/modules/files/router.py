"""文件相关 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.files.schemas import FileDeleteResponse, FileUploadResponse
from app.modules.files.service import FileUploadService

router = APIRouter(prefix="/api/files", tags=["files"])


@router.post("/upload", response_model=FileUploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileUploadResponse:
    """上传一个原始文件，并返回可用于消息附件的 document_id。"""

    return await FileUploadService(db).upload(file=file, current_user=current_user)


@router.delete("/{document_id}", response_model=FileDeleteResponse)
def delete_file(
    document_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileDeleteResponse:
    """删除尚未进入对话的上传文件。"""

    return FileUploadService(db).delete(document_id=document_id, current_user=current_user)
