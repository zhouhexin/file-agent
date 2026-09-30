"""共享累计表格 Demo API；复用登录，所有用户均可操作，无 owner 授权流程。"""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from app.core.config import get_settings
from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from .schemas import DatasetCreate, DatasetCreateFromExcel, DatasetAppend, DatasetQuery
from .service import StructuredDatasetService
from .runtime import execute_dataset_write
from .task_schemas import TaskPreview, TaskConfirm, TaskCommit
from .tasks import DatasetTaskService


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


@router.post("/from-excel")
def create_dataset_from_excel(
    request: DatasetCreateFromExcel,
    response: Response,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """把本轮已入库的 XLSX 工作副本注册为新的目标表。"""

    result = execute_dataset_write(
        db=db,
        user=user,
        operation="create_from_excel",
        payload=request,
    )
    if result.get("kind") == "filesystem_job":
        response.status_code = 202
    return result


@router.get("/targets/by-filename")
def resolve_dataset_target(
    filename: str = Query(min_length=1, max_length=240),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """按完整文件名解析唯一活动累计 Excel，不执行模糊选择。"""

    return StructuredDatasetService(db, user).resolve_target(filename)


@router.get("/sources/{document_id}")
def get_source(
    document_id: UUID,
    document_version_id: UUID | None = Query(default=None),
    extraction_run_id: UUID | None = Query(default=None),
    content_origin: Literal["WORKBUDDY", "WORKBUDDY_SCOPED"] | None = Query(default=None),
    mode: Literal["FULL", "INDEX"] = Query(default="FULL"),
    unit_orders: list[int] | None = Query(default=None),
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """按固定版本返回正文或无正文索引；定向读取不触发解析或 OCR。"""

    return StructuredDatasetService(db, user).source(
        str(document_id),
        version_id=str(document_version_id) if document_version_id else None,
        extraction_run_id=str(extraction_run_id) if extraction_run_id else None,
        content_origin=content_origin,
        mode=mode,
        unit_orders=unit_orders,
    )


@router.post("/tasks/preview")
def preview_task(request: TaskPreview, db=Depends(get_db), user=Depends(get_current_user)):
    """登记无正文预览，冻结全部列与目标，不创建 Excel。"""
    return execute_dataset_write(db=db, user=user, operation="task_preview", payload=request)


@router.get("/tasks/capabilities")
def task_capabilities(user=Depends(get_current_user)):
    """在宿主 OCR 之前公开当前服务端可校验的专用契约，不返回任何文件内容。"""
    return {"contract_version": "sealed-item-template-v1", "source_fact_column_range": "J:P",
            "source_only_fragments": True, "hr_annual_excellence_seal_block": True,
            "identity_column_range": "B:I", "review_modes": ["REVIEW_REQUIRED", "DIRECT_SAVE"],
            "full_row_write_scope": "A:AA", "ocr_uncertain_markers": True,
            "full_row_pdf_source_kind": "WORKBUDDY_DOCUMENT"}


@router.get("/tasks/{task_id}")
def get_task(task_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """只读恢复当前用户的任务阶段，不因轮询产生写入。"""
    service = DatasetTaskService(db, user)
    task = service.owned(str(task_id))
    if task.status == "COMPLETED":
        service._verify_publication(task)
    return service.receipt(task)


@router.post("/tasks/{task_id}/confirm")
def confirm_task(task_id: UUID, request: TaskConfirm, db=Depends(get_db), user=Depends(get_current_user)):
    """用户确认完整表格预览，正文及 Excel 保存由后续固定任务推进。"""
    return execute_dataset_write(db=db, user=user, operation="task_confirm", task_id=str(task_id), payload=request)


@router.post("/tasks/{task_id}/commit")
def commit_task(task_id: UUID, request: TaskCommit, db=Depends(get_db), user=Depends(get_current_user)):
    """短参数恢复已确认任务，不能跳过确认或返回虚假 Excel 完成。"""
    return execute_dataset_write(db=db, user=user, operation="task_commit", task_id=str(task_id), payload=request)


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
