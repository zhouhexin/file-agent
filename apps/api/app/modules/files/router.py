"""文件相关 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.files.schemas import FileUploadResponse
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
