"""共享累计表格 Demo API；复用登录，所有用户均可操作，无 owner 授权流程。"""

from html import escape
from pathlib import Path
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from openpyxl import load_workbook
from app.core.config import get_settings
from app.core.database import get_db
from app.core.security import (TokenDecodeError, create_public_dataset_export_token,
                               decode_public_dataset_export_token)
from app.db.models import StructuredDatasetExport
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


@router.get("/{dataset_id}/exports/{export_id}/browser-links")
def export_browser_links(dataset_id: UUID, export_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """已登录用户为单个已发布导出签发 24 小时只读预览和下载链接。"""
    service = StructuredDatasetService(db, user)
    # 生成能力链接前先验证共享目标、发布状态、文件路径及快照哈希。
    service.download(str(dataset_id), str(export_id))
    token = create_public_dataset_export_token(str(dataset_id), str(export_id))
    prefix = f"/api/structured-datasets/public-exports/{token}"
    return {"preview_url": f"{prefix}/preview", "download_url": f"{prefix}/download",
            "expires_in_seconds": 86400}


def _public_export_scope(token: str) -> dict:
    """解码仅限单个导出文件的短期只读能力链接。"""
    try:
        return decode_public_dataset_export_token(token)
    except TokenDecodeError as exc:
        raise HTTPException(status_code=404, detail="下载链接无效或已过期") from exc


@router.get("/public-exports/{token}/download")
def download_public_export(token: str, db=Depends(get_db)):
    """无需登录下载一个签名令牌绑定的 XLSX 导出快照。"""
    scope = _public_export_scope(token)
    response = StructuredDatasetService(db, None).download(
        scope["dataset_id"], scope["export_id"])
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@router.get("/public-exports/{token}/preview", response_class=HTMLResponse)
def preview_public_export(token: str, db=Depends(get_db)):
    """无需登录预览签名令牌绑定的 XLSX 快照，并提供原文件下载链接。"""
    scope = _public_export_scope(token)
    service = StructuredDatasetService(db, None)
    dataset = service.dataset(scope["dataset_id"])
    # 复用受控下载的发布状态、路径和 SHA-256 校验，预览也不读取未发布候选。
    download_response = service.download(scope["dataset_id"], scope["export_id"])
    export = db.query(StructuredDatasetExport).filter_by(
        id=scope["export_id"], dataset_id=scope["dataset_id"]).one_or_none()
    if export is None:
        raise HTTPException(status_code=404, detail="导出文件不存在")
    workbook = load_workbook(Path(download_response.path), read_only=True, data_only=True)
    download_url = f"/api/structured-datasets/public-exports/{token}/download"
    sections = []
    max_sheets, max_rows, max_columns, max_cells = 20, 300, 40, 12000
    visible_cells = 0
    truncated = len(workbook.worksheets) > max_sheets
    try:
        for sheet_index, sheet in enumerate(workbook.worksheets[:max_sheets]):
            truncated |= (sheet.max_row or 0) > max_rows or (sheet.max_column or 0) > max_columns
            rows = []
            for row_index, row in enumerate(sheet.iter_rows(max_row=max_rows, max_col=max_columns,
                                                              values_only=True)):
                cells = []
                for value in row:
                    visible_cells += 1
                    if visible_cells > max_cells:
                        truncated = True
                        break
                    rendered = "" if value is None else escape(str(value))
                    tag = "th" if row_index == 0 else "td"
                    cells.append(f"<{tag}>{rendered}</{tag}>")
                if cells:
                    rows.append("<tr>" + "".join(cells) + "</tr>")
                if visible_cells > max_cells:
                    break
            sections.append(f"<section><h2>{escape(sheet.title)}</h2><table>{''.join(rows)}</table></section>")
            if visible_cells > max_cells:
                break
    finally:
        workbook.close()
    truncation = "<p>预览内容较多，已截取部分单元格；可下载原文件查看完整内容。</p>" if truncated else ""
    filename = escape(dataset.target_filename or "累计表格.xlsx")
    html = ("<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'><title>{filename}</title>"
            "<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;margin:24px;color:#172033}"
            "section{margin:16px 0;padding:14px;border:1px solid #dfe5ec;border-radius:8px}"
            "table{border-collapse:collapse;display:block;overflow:auto;max-width:100%}"
            "th,td{border:1px solid #dfe5ec;padding:6px 8px;min-width:70px;white-space:pre-wrap;"
            "vertical-align:top}th{background:#eef3f8}</style><main>"
            f"<h1>{filename}</h1><p><a href='{escape(download_url, quote=True)}'>下载原始 Excel</a></p>"
            f"{truncation}{''.join(sections)}</main></html>")
    return HTMLResponse(content=html, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-Robots-Tag": "noindex, nofollow",
    })
