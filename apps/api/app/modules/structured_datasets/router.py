"""共享累计表格 Demo API；复用登录，所有用户均可操作，无 owner 授权流程。"""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.config import get_settings
from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from .schemas import DatasetCreate, DatasetAppend, DatasetQuery
from .service import StructuredDatasetService
from .runtime import execute_dataset_write


def require_demo():
    """新入口独立开关，不改变原有插件及 OCR 的路由行为。"""
    if not get_settings().workbuddy_structured_dataset_demo_enabled:
        raise HTTPException(503, detail={"code": "DATASET_DEMO_DISABLED", "message": "累计表格 Demo 尚未启用。"})


router = APIRouter(prefix="/api/structured-datasets", tags=["structured-datasets"], dependencies=[Depends(require_demo)])


@router.get("")
def list_datasets(db=Depends(get_db), user=Depends(get_current_user)):
    """列出共享数据集供继续追加。"""
    return StructuredDatasetService(db, user).list_datasets()


@router.post("")
def create_dataset(request: DatasetCreate, db=Depends(get_db), user=Depends(get_current_user)):
    """首次固定 schema，不要求用户授权确认。"""
    return execute_dataset_write(db=db, user=user, operation="create", payload=request)


@router.get("/sources/{document_id}")
def get_source(
    document_id: UUID,
    document_version_id: UUID | None = Query(default=None),
    extraction_run_id: UUID | None = Query(default=None),
    content_origin: Literal["WORKBUDDY"] | None = Query(default=None),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """返回固定工作副本版本的正式正文区段；GET 不触发解析或 OCR。"""

    return StructuredDatasetService(db, user).source(
        str(document_id),
        version_id=str(document_version_id) if document_version_id else None,
        extraction_run_id=str(extraction_run_id) if extraction_run_id else None,
        content_origin=content_origin,
    )


@router.get("/{dataset_id}")
def get_dataset(dataset_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """读取共享表格 schema 和累计状态。"""
    return StructuredDatasetService(db, user).get(str(dataset_id))


@router.post("/{dataset_id}/records")
def append_records(dataset_id: UUID, request: DatasetAppend, db=Depends(get_db), user=Depends(get_current_user)):
    """从固定工作副本版本幂等追加单图记录。"""
    return execute_dataset_write(db=db, user=user, operation="append", dataset_id=str(dataset_id), payload=request)


@router.post("/{dataset_id}/query")
def query_dataset(dataset_id: UUID, request: DatasetQuery, db=Depends(get_db), user=Depends(get_current_user)):
    """通过固定字段比较和确定性汇总查询。"""
    return StructuredDatasetService(db, user).query(str(dataset_id), request)


@router.post("/{dataset_id}/exports")
def export_dataset(dataset_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """生成或复用当前 revision 的 XLSX 快照。"""
    return execute_dataset_write(db=db, user=user, operation="export", dataset_id=str(dataset_id))


@router.get("/{dataset_id}/exports/{export_id}/content")
def download_export(dataset_id: UUID, export_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """下载独立聚合快照。"""
    return StructuredDatasetService(db, user).download(str(dataset_id), str(export_id))
