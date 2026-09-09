"""WorkBuddy/MCP 对外集成路由。

路由只做认证、Schema 校验和事务级 Service 调用；它不能直接读本地源目录、写工作副本或绕过
Tool/worker 边界。上传、重复确认、OCR 回写、取消和重试均保存持久化业务状态后异步续跑。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import get_settings
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.ingestion.schemas import (
    IngestBatchCreateRequest,
    IngestBatchResponse,
    IngestBatchResumeResponse,
    IngestContentUploadResponse,
    IngestDuplicateDecisionRequest,
    IngestDuplicateDecisionResponse,
    IngestDuplicateReviewResponse,
    IngestItemsAppendRequest,
    IngestItemsAppendResponse,
    IngestItemsPageResponse,
    IngestItemActionRequest,
    IngestItemActionResponse,
)
from app.modules.ingestion.service import IngestionService
from app.modules.ingestion.duplicate_service import IngestionDuplicateService
from app.modules.external_extraction.schemas import (
    ExternalExtractionClaimRequest,
    ExternalExtractionClaimResponse,
    ExternalExtractionRenewRequest,
    ExternalExtractionSubmitRequest,
    ExternalExtractionSubmitResponse,
    ExternalExtractionTaskResponse,
)
from app.modules.external_extraction.service import ExternalExtractionService


def require_integration_ingest_enabled() -> None:
    """关闭试点开关时拒绝整个外部导入面，不能只依赖 MCP 客户端自律。"""

    if not get_settings().integration_ingest_enabled:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "INTEGRATION_INGEST_DISABLED",
                "message": "WorkBuddy 本地导入通道当前未启用。",
            },
        )


router = APIRouter(
    prefix="/api/integrations/v1",
    tags=["integrations"],
    dependencies=[Depends(require_integration_ingest_enabled)],
)


@router.post(
    "/ingest-batches",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ingest_batch(
    request: IngestBatchCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestBatchResponse:
    """创建由当前用户授权、尚未固定成员范围的导入批次。"""

    return IngestionService(db).create_batch(request=request, current_user=current_user)


@router.post(
    "/ingest-batches/{batch_id}/items",
    response_model=IngestItemsAppendResponse,
)
def append_ingest_items(
    batch_id: str,
    request: IngestItemsAppendRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestItemsAppendResponse:
    """分页追加固定来源快照；相同 client_item_id 只允许完全相同的重试。"""

    return IngestionService(db).append_items(
        batch_id=batch_id,
        request=request,
        current_user=current_user,
    )


@router.post(
    "/ingest-batches/{batch_id}/seal",
    response_model=IngestBatchResponse,
)
def seal_ingest_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestBatchResponse:
    """固定批次清单；seal 后新增成员必须使用新批次或未来显式修订。"""

    return IngestionService(db).seal_batch(batch_id=batch_id, current_user=current_user)


@router.post(
    "/ingest-batches/{batch_id}/resume",
    response_model=IngestBatchResumeResponse,
)
def resume_ingest_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestBatchResumeResponse:
    """恢复原清单尚未传输的成员，不重置业务失败和终态。"""

    return IngestionService(db).resume_batch(batch_id=batch_id, current_user=current_user)


@router.get(
    "/ingest-batches/{batch_id}",
    response_model=IngestBatchResponse,
)
def get_ingest_batch(
    batch_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestBatchResponse:
    """恢复当前用户批次状态、策略修订和逐状态计数。"""

    return IngestionService(db).get_batch(batch_id=batch_id, current_user=current_user)


@router.get(
    "/ingest-batches/{batch_id}/items",
    response_model=IngestItemsPageResponse,
)
def list_ingest_items(
    batch_id: str,
    cursor: str | None = Query(default=None, min_length=1, max_length=36),
    limit: int = Query(default=100, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestItemsPageResponse:
    """分页读取批次逐文件明细，供断线恢复和最终回执展示。"""

    return IngestionService(db).list_items(
        batch_id=batch_id,
        cursor=cursor,
        limit=limit,
        current_user=current_user,
    )


@router.put(
    "/ingest-batches/{batch_id}/items/{item_id}/content",
    response_model=IngestContentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_ingest_item_content(
    batch_id: str,
    item_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestContentUploadResponse:
    """接收一个固定清单项的字节，并自动安排后续查重与整理任务。"""

    return await IngestionService(db).receive_item_content(
        batch_id=batch_id,
        item_id=item_id,
        file=file,
        current_user=current_user,
    )


@router.get(
    "/ingest-items/{item_id}/duplicate-review",
    response_model=IngestDuplicateReviewResponse,
)
def get_ingest_duplicate_review(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestDuplicateReviewResponse:
    """返回绑定条目和修订的重复候选，供 WorkBuddy 恢复选择。"""

    return IngestionDuplicateService(db).get_review(item_id=item_id, current_user=current_user)


@router.post(
    "/ingest-items/{item_id}/duplicate-decision",
    response_model=IngestDuplicateDecisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def decide_ingest_duplicate(
    item_id: str,
    request: IngestDuplicateDecisionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestDuplicateDecisionResponse:
    """提交用户明确的候选和决定；过期修订或幂等冲突必须关闭式拒绝。"""

    return IngestionDuplicateService(db).decide(
        item_id=item_id,
        request=request,
        current_user=current_user,
    )


@router.post(
    "/ingest-items/{item_id}/retry",
    response_model=IngestItemActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_ingest_item(
    item_id: str,
    request: IngestItemActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestItemActionResponse:
    """显式重试失败条目或其失败附带请求，不影响批次其他文件。"""

    return IngestionService(db).retry_item(
        item_id=item_id,
        request=request,
        current_user=current_user,
    )


@router.post(
    "/ingest-items/{item_id}/cancel",
    response_model=IngestItemActionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_ingest_item(
    item_id: str,
    request: IngestItemActionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> IngestItemActionResponse:
    """取消未发布条目；已经发布时仅取消尚未领取的附带请求。"""

    return IngestionService(db).cancel_item(
        item_id=item_id,
        request=request,
        current_user=current_user,
    )


@router.post(
    "/extraction-tasks/{task_id}/claim",
    response_model=ExternalExtractionClaimResponse,
)
def claim_external_extraction(
    task_id: str,
    request: ExternalExtractionClaimRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ExternalExtractionClaimResponse:
    """领取当前用户的外部 OCR 任务，并一次性取得租约 token。"""

    return ExternalExtractionClaimResponse(
        **ExternalExtractionService(db).claim(
            task_id=task_id,
            worker_id=request.worker_id,
            current_user=current_user,
        )
    )


@router.post(
    "/extraction-tasks/{task_id}/renew",
    response_model=ExternalExtractionTaskResponse,
)
def renew_external_extraction(
    task_id: str,
    request: ExternalExtractionRenewRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ExternalExtractionTaskResponse:
    """续期当前 Worker 的有效 OCR 租约。"""

    service = ExternalExtractionService(db)
    task = service.renew(
        task_id=task_id,
        worker_id=request.worker_id,
        lease_token=request.lease_token,
        current_user=current_user,
    )
    return ExternalExtractionTaskResponse(**service.to_response(task))


@router.get(
    "/extraction-tasks/{task_id}/pages/{page_number}",
    response_class=FileResponse,
)
def download_external_extraction_page(
    task_id: str,
    page_number: int,
    worker_id: str = Query(min_length=1, max_length=160),
    x_extraction_lease_token: str = Header(min_length=20, max_length=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    """下载有效租约内的一页真实图片资源。"""

    return ExternalExtractionService(db).page_response(
        task_id=task_id,
        page_number=page_number,
        worker_id=worker_id,
        lease_token=x_extraction_lease_token,
        current_user=current_user,
    )


@router.post(
    "/extraction-tasks/{task_id}/results",
    response_model=ExternalExtractionSubmitResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_external_extraction(
    task_id: str,
    request: ExternalExtractionSubmitRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ExternalExtractionSubmitResponse:
    """验收固定页集合并把 OCR 正文写入正式提取运行。"""

    service = ExternalExtractionService(db)
    task, next_stage, job_id, reused = service.submit(
        task_id=task_id,
        payload=request,
        current_user=current_user,
    )
    return ExternalExtractionSubmitResponse(
        task=ExternalExtractionTaskResponse(**service.to_response(task)),
        accepted=True,
        reused=reused,
        next_stage=next_stage,
        filesystem_job_id=job_id,
    )
