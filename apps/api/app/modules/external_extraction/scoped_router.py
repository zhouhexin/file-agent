"""WorkBuddy B–I 定向 PDF OCR 的受控 API。"""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.integrations.router import require_integration_ingest_enabled
from .document_router import require_document_extraction_enabled
from .schemas import ExternalExtractionClaimRequest, ExternalExtractionRenewRequest
from .scoped_schemas import ScopedSubmit
from .scoped_service import ScopedPdfExtractionService


router = APIRouter(prefix="/api/integrations/v1/scoped-pdf-sources", tags=["integrations"],
    dependencies=[Depends(require_integration_ingest_enabled), Depends(require_document_extraction_enabled)])


@router.get("/{task_id}")
def get(task_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    return ScopedPdfExtractionService(db).get(task_id=task_id, user=user)


@router.post("/{task_id}/claim")
def claim(task_id: str, request: ExternalExtractionClaimRequest,
          user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    result = ScopedPdfExtractionService(db).claim(task_id=task_id, worker_id=request.worker_id, user=user)
    db.commit()
    return result


@router.get("/{task_id}/regions/{region_id}")
def region(task_id: str, region_id: str, worker_id: str = Query(min_length=1, max_length=160),
           x_extraction_lease_token: str = Header(min_length=20, max_length=500),
           user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    result = ScopedPdfExtractionService(db).region(task_id=task_id, region_id=region_id,
        worker_id=worker_id, lease_token=x_extraction_lease_token, user=user)
    return Response(content=result, media_type="image/png", headers={
        "X-Region-SHA256": hashlib.sha256(result).hexdigest(), "Cache-Control": "no-store"})


@router.post("/{task_id}/renew")
def renew(task_id: str, request: ExternalExtractionRenewRequest,
          user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    result = ScopedPdfExtractionService(db).renew(task_id=task_id, worker_id=request.worker_id,
        lease_token=request.lease_token, user=user)
    db.commit()
    return result


@router.post("/{task_id}/results")
def submit(task_id: str, request: ScopedSubmit,
           user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    try:
        result = ScopedPdfExtractionService(db).submit(task_id=task_id, payload=request, user=user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
