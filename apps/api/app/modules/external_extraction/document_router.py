"""WorkBuddy 完整文档提取 v1 路由，独立于旧页图 OCR 契约。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.integrations.router import require_integration_ingest_enabled

from .document_schemas import (
    DocumentExtractionClaimResponse,
    MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES,
    DocumentExtractionSubmitRequest,
    DocumentExtractionSubmitResponse,
    DocumentExtractionTaskResponse,
)
from .schemas import ExternalExtractionClaimRequest, ExternalExtractionRenewRequest
from .document_service import WorkBuddyDocumentExtractionService


def require_document_extraction_enabled() -> None:
    """新建文档任务必须显式开启，关闭时不能悄悄退回服务器解析。"""

    if not get_settings().workbuddy_document_extraction_enabled:
        raise HTTPException(503, detail={"code": "WORKBUDDY_DOCUMENT_DISABLED", "message": "WorkBuddy 文档提取通道当前未启用。"})


router = APIRouter(
    prefix="/api/integrations/v1/document-extraction-tasks",
    tags=["integrations"],
    dependencies=[Depends(require_integration_ingest_enabled), Depends(require_document_extraction_enabled)],
)


@router.get("/{task_id}", response_model=DocumentExtractionTaskResponse)
def get_document_extraction(task_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DocumentExtractionTaskResponse:
    """读取任务状态；正文仍只能通过有效租约的 source 路由读取。"""

    return DocumentExtractionTaskResponse(**WorkBuddyDocumentExtractionService(db).get(task_id=task_id, current_user=current_user))


@router.post("/{task_id}/claim", response_model=DocumentExtractionClaimResponse)
def claim_document_extraction(task_id: str, request: ExternalExtractionClaimRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DocumentExtractionClaimResponse:
    """领取固定原文件并获取短期租约。"""

    result = WorkBuddyDocumentExtractionService(db).claim(task_id=task_id, worker_id=request.worker_id, current_user=current_user)
    db.commit()
    return DocumentExtractionClaimResponse(**result)


@router.post("/{task_id}/renew", response_model=DocumentExtractionTaskResponse)
def renew_document_extraction(task_id: str, request: ExternalExtractionRenewRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DocumentExtractionTaskResponse:
    """续期现有 Worker 的租约。"""

    service = WorkBuddyDocumentExtractionService(db)
    task = service.renew(task_id=task_id, worker_id=request.worker_id, lease_token=request.lease_token, current_user=current_user)
    db.commit()
    db.refresh(task)
    return DocumentExtractionTaskResponse(**service.to_response(task))


@router.get("/{task_id}/source", response_class=FileResponse)
def download_document_source(task_id: str, worker_id: str = Query(min_length=1, max_length=160), x_extraction_lease_token: str = Header(min_length=20, max_length=500), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> FileResponse:
    """下载受控原文件，客户端不得指定或推断服务器路径。"""

    return WorkBuddyDocumentExtractionService(db).source_response(task_id=task_id, worker_id=worker_id, lease_token=x_extraction_lease_token, current_user=current_user)


@router.post("/{task_id}/results", response_model=DocumentExtractionSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_document_extraction(task_id: str, request: Request, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> DocumentExtractionSubmitResponse:
    """验收宿主全文；PARTIAL/FAILED 被保存为诊断但不发布工作副本。"""

    raw_body = await request.body()
    if len(raw_body) > MAX_DOCUMENT_EXTRACTION_PAYLOAD_BYTES:
        raise HTTPException(413, detail={"code": "EXTRACTION_PAYLOAD_TOO_LARGE", "message": "文档提取结果超过请求体限制。"})
    try:
        payload = DocumentExtractionSubmitRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    service = WorkBuddyDocumentExtractionService(db)
    task, next_stage, job_id, reused, ok = service.submit(task_id=task_id, payload=payload, current_user=current_user)
    db.commit()
    db.refresh(task)
    return DocumentExtractionSubmitResponse(task=DocumentExtractionTaskResponse(**service.to_response(task)), accepted=True, ok=ok, reused=reused, next_stage=next_stage, filesystem_job_id=job_id)
