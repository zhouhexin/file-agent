"""成果资助使用登记的只读预览与受控发布接口。"""

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import get_settings
from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from app.modules.structured_datasets.runtime import execute_dataset_write

from .schemas import (FundingUsageRequest, FundingUsagePublish,
                      FundingUsageContinue, FundingUsageContinuePublish)
from .service import FundingUsageService


def require_funding_usage():
    settings = get_settings()
    if not settings.workbuddy_excel_writeback_enabled or not settings.workbuddy_structured_dataset_demo_enabled:
        raise HTTPException(503, detail={"code": "FUNDING_USAGE_DISABLED",
            "message": "成果资助 Excel 生成依赖已启用的 WorkBuddy 表格能力。"})


router = APIRouter(prefix="/api/funding-usage", tags=["funding-usage"],
                   dependencies=[Depends(require_funding_usage)])


@router.post("/preview")
def preview(request: FundingUsageRequest, db=Depends(get_db), user=Depends(get_current_user)):
    return FundingUsageService(db, user).preview(request)


@router.post("/publish")
def publish(request: FundingUsagePublish, db=Depends(get_db), user=Depends(get_current_user)):
    return execute_dataset_write(db=db, user=user, operation="funding_usage_publish", payload=request)


@router.post("/continue/preview")
def continue_preview(request: FundingUsageContinue, db=Depends(get_db), user=Depends(get_current_user)):
    return FundingUsageService(db, user).continue_preview(request)


@router.post("/continue/publish")
def continue_publish(request: FundingUsageContinuePublish, db=Depends(get_db), user=Depends(get_current_user)):
    return execute_dataset_write(db=db, user=user, operation="funding_usage_continue_publish", payload=request)
