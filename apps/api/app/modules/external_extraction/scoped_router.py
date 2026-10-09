"""WorkBuddy B–I 与 L 定向 PDF OCR、逐页定位和提交的受控 API。"""

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
from .scoped_schemas import ReviewProbeSubmit, ReviewRegionReject, ScopedSubmit
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


@router.post("/{task_id}/review-discovery-upgrade")
def review_discovery_upgrade(task_id: str, user: User = Depends(get_current_user),
                             db: Session = Depends(get_db)) -> dict:
    """将尚未完成的旧版固定末页任务重新签发为倒序找页任务。"""
    try:
        result = ScopedPdfExtractionService(db).upgrade_review_discovery(task_id=task_id, user=user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


@router.post("/{task_id}/fixed25-upgrade")
def fixed25_upgrade(task_id: str, user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> dict:
    """将未完成的 v2/v3 任务重签为固定第 25 页契约。"""
    try:
        result = ScopedPdfExtractionService(db).upgrade_fixed_25(task_id=task_id, user=user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


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


@router.get("/{task_id}/review-probe")
def review_probe(task_id: str, worker_id: str = Query(min_length=1, max_length=160),
                 x_extraction_lease_token: str = Header(min_length=20, max_length=500),
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """仅返回当前租约签发的倒序候选页与哈希，不接受调用方指定页码。"""
    return ScopedPdfExtractionService(db).probe_info(task_id=task_id, worker_id=worker_id,
        lease_token=x_extraction_lease_token, user=user)


@router.get("/{task_id}/review-probe/image")
def review_probe_image(task_id: str, worker_id: str = Query(min_length=1, max_length=160),
                       x_extraction_lease_token: str = Header(min_length=20, max_length=500),
                       user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> Response:
    """在当前租约内只渲染服务端选定的一页定位图。"""
    result = ScopedPdfExtractionService(db).probe_image(task_id=task_id, worker_id=worker_id,
        lease_token=x_extraction_lease_token, user=user)
    return Response(content=result, media_type="image/png", headers={
        "X-Region-SHA256": hashlib.sha256(result).hexdigest(), "Cache-Control": "no-store"})


@router.post("/{task_id}/review-probe/results")
def review_probe_submit(task_id: str, request: ReviewProbeSubmit,
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """校验候选页哈希、顺序及 OCR 栏目位置后推进发现进度。"""
    try:
        result = ScopedPdfExtractionService(db).probe_submit(task_id=task_id, payload=request, user=user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


@router.post("/{task_id}/review-region/reject")
def review_region_reject(task_id: str, request: ReviewRegionReject,
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """客户端核对栏目图不含正文时，v4 留空 L，旧 v3 才查前一页。"""
    try:
        result = ScopedPdfExtractionService(db).reject_review_region(
            task_id=task_id, payload=request, user=user)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


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
