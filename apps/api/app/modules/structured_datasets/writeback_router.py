"""WorkBuddy 完整 XLSX 回传入口；与旧累计表格 API 独立开关。"""

import hashlib
import tempfile
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.core.config import get_settings
from app.core.database import get_db
from app.modules.auth.dependencies import get_current_user
from .runtime import execute_dataset_write
from .task_schemas import TaskPayload
from .writeback import ExcelWritebackService
from .writeback_schemas import WritebackCheckout, WritebackConfirm, WritebackPreview, WritebackPublish, ManualRevisionPublish
from .correction import ExcelCorrectionService
from .correction_schemas import CorrectionCheckout, CorrectionPayload, CorrectionPublish


def require_writeback():
    """新功能显式开启，旧功能不受部署顺序影响。"""
    settings = get_settings()
    if not settings.workbuddy_excel_writeback_enabled or not settings.workbuddy_structured_dataset_demo_enabled:
        raise HTTPException(503, detail={"code": "WRITEBACK_DISABLED",
            "message": "Excel 文件回传依赖结构化数据集能力，两个功能开关都必须启用。"})


router = APIRouter(prefix="/api/excel-writeback", tags=["excel-writeback"],
                   dependencies=[Depends(require_writeback)])


@router.post("/checkout")
def checkout(request: WritebackCheckout, db=Depends(get_db), user=Depends(get_current_user)):
    """精确文件名取得当前版本；文件字节由现有鉴权下载接口提供。"""
    return execute_dataset_write(db=db, user=user, operation="writeback_checkout", payload=request)


@router.get("/tasks/{task_id}")
def get_task(task_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """只读恢复回传任务状态。"""
    service = ExcelWritebackService(db, user)
    task = service.owned(str(task_id))
    if task.status == "COMPLETED":
        service.tasks._verify_publication(task)
    return service.receipt(task)


@router.post("/tasks/{task_id}/preview")
def preview(task_id: UUID, request: WritebackPreview, db=Depends(get_db), user=Depends(get_current_user)):
    """冻结候选及产物共同摘要，复核模式等待用户确认。"""
    return execute_dataset_write(db=db, user=user, operation="writeback_preview",
                                 task_id=str(task_id), payload=request)


@router.post("/tasks/{task_id}/confirm")
def confirm(task_id: UUID, request: WritebackConfirm, db=Depends(get_db), user=Depends(get_current_user)):
    """确认用户实际看到的完整候选与 Excel 文件摘要。"""
    return execute_dataset_write(db=db, user=user, operation="writeback_confirm",
                                 task_id=str(task_id), payload=request)


@router.post("/tasks/{task_id}/publish")
def publish(task_id: UUID, result_digest: str = Form(...), file_sha256: str = Form(...),
            payload_json: str = Form(...), file: UploadFile = File(...),
            db=Depends(get_db), user=Depends(get_current_user)):
    """二进制先落隔离临时文件，哈希匹配后才进入受审计的发布边界。"""
    if len(payload_json.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(413, detail={"code": "WRITEBACK_PAYLOAD_TOO_LARGE", "message": "候选清单超过安全上限。"})
    request = WritebackPublish.model_validate({"task_id": task_id, "result_digest": result_digest,
                                               "file_sha256": file_sha256})
    candidate = TaskPayload.model_validate_json(payload_json)
    if not file.filename or Path(file.filename).name != file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(422, detail={"code": "WRITEBACK_XLSX_REQUIRED", "message": "只接受安全文件名的 XLSX 回传。"})
    task = ExcelWritebackService(db, user).owned(str(task_id))
    if file.filename != (task.manifest_json or {}).get("filename"):
        raise HTTPException(422, detail={"code": "WRITEBACK_FILENAME_CHANGED",
            "message": "回传文件名必须与 checkout 的当前工作副本完全一致。"})
    with tempfile.TemporaryDirectory(prefix="file-agent-writeback-") as directory:
        path = Path(directory) / "candidate.xlsx"
        digest = hashlib.sha256()
        size = 0
        with path.open("xb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise HTTPException(413, detail={"code": "WRITEBACK_FILE_TOO_LARGE", "message": "Excel 超过 32 MiB 限制。"})
                digest.update(chunk)
                output.write(chunk)
        if size == 0 or digest.hexdigest() != file_sha256:
            raise HTTPException(422, detail={"code": "WRITEBACK_FILE_CHANGED", "message": "文件为空或与预览哈希不一致。"})
        return execute_dataset_write(db=db, user=user, operation="writeback_publish",
                                     task_id=str(task_id), payload=request,
                                     candidate_payload=candidate, artifact_path=path)


@router.post("/corrections/checkout")
def correction_checkout(request: CorrectionCheckout, db=Depends(get_db), user=Depends(get_current_user)):
    """冻结现有记录与行号；更正不会走新增行的 checkout。"""
    return execute_dataset_write(db=db, user=user, operation="correction_checkout", payload=request)


@router.get("/corrections/tasks/{task_id}")
def correction_get(task_id: UUID, db=Depends(get_db), user=Depends(get_current_user)):
    """恢复已确认更正任务，历史来源无需重新 OCR。"""
    service = ExcelCorrectionService(db, user)
    task = service.owned(str(task_id))
    if task.status == "COMPLETED":
        service.tasks._verify_publication(task)
    return service.receipt(task)


@router.post("/corrections/tasks/{task_id}/preview")
def correction_preview(task_id: UUID, request: WritebackPreview,
                       db=Depends(get_db), user=Depends(get_current_user)):
    """登记候选哈希，始终等待用户确认既有数据的逐格变化。"""
    return execute_dataset_write(db=db, user=user, operation="correction_preview",
                                 task_id=str(task_id), payload=request)


@router.post("/corrections/tasks/{task_id}/confirm")
def correction_confirm(task_id: UUID, request: WritebackConfirm,
                       db=Depends(get_db), user=Depends(get_current_user)):
    """只确认已展示的同一文件和更正摘要。"""
    return execute_dataset_write(db=db, user=user, operation="correction_confirm",
                                 task_id=str(task_id), payload=request)


@router.post("/corrections/tasks/{task_id}/publish")
def correction_publish(task_id: UUID, result_digest: str = Form(...), file_sha256: str = Form(...),
                       payload_json: str = Form(...), file: UploadFile = File(...),
                       db=Depends(get_db), user=Depends(get_current_user)):
    """隔离接收完整 XLSX，再由更正服务校验差异与正式来源。"""
    if len(payload_json.encode("utf-8")) > 2 * 1024 * 1024:
        raise HTTPException(413, detail={"code": "WRITEBACK_PAYLOAD_TOO_LARGE", "message": "更正清单超过安全上限。"})
    request = CorrectionPublish.model_validate({"task_id": task_id,
        "result_digest": result_digest, "file_sha256": file_sha256})
    candidate = CorrectionPayload.model_validate_json(payload_json)
    task = ExcelCorrectionService(db, user).owned(str(task_id))
    if not file.filename or file.filename != (task.manifest_json or {}).get("filename"):
        raise HTTPException(422, detail={"code": "WRITEBACK_FILENAME_CHANGED",
            "message": "回传文件名必须等于当前同名工作副本。"})
    with tempfile.TemporaryDirectory(prefix="file-agent-correction-") as directory:
        path = Path(directory) / "candidate.xlsx"
        digest = hashlib.sha256()
        size = 0
        with path.open("xb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise HTTPException(413, detail={"code": "WRITEBACK_FILE_TOO_LARGE", "message": "Excel 超过 32 MiB。"})
                digest.update(chunk)
                output.write(chunk)
        if size == 0 or digest.hexdigest() != file_sha256:
            raise HTTPException(422, detail={"code": "WRITEBACK_FILE_CHANGED", "message": "文件为空或与预览哈希不一致。"})
        return execute_dataset_write(db=db, user=user, operation="correction_publish",
            task_id=str(task_id), payload=request, candidate_payload=candidate, artifact_path=path)


@router.post("/manual-revisions/publish")
def manual_revision_publish(
    filename: str = Form(...), request_key: str = Form(...),
    base_document_version_id: UUID = Form(...), base_sha256: str = Form(...),
    base_revision: int = Form(...), file_sha256: str = Form(...),
    file: UploadFile = File(...), db=Depends(get_db), user=Depends(get_current_user),
):
    """本轮用户上传补全后的完整 XLSX，原子发布到同一工作副本。"""
    request = ManualRevisionPublish.model_validate({
        "filename": filename, "request_key": request_key,
        "base_document_version_id": base_document_version_id,
        "base_sha256": base_sha256, "base_revision": base_revision,
        "file_sha256": file_sha256,
    })
    if (not file.filename or Path(file.filename).name != file.filename
            or file.filename != request.filename or not file.filename.lower().endswith(".xlsx")):
        raise HTTPException(422, detail={"code": "MANUAL_EXCEL_FILENAME_INVALID",
            "message": "补全文件必须使用目标工作副本的完整 .xlsx 文件名。"})
    with tempfile.TemporaryDirectory(prefix="file-agent-manual-excel-") as directory:
        path = Path(directory) / "candidate.xlsx"
        digest = hashlib.sha256()
        size = 0
        with path.open("xb") as output:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > 32 * 1024 * 1024:
                    raise HTTPException(413, detail={"code": "WRITEBACK_FILE_TOO_LARGE",
                        "message": "Excel 超过 32 MiB 限制。"})
                digest.update(chunk)
                output.write(chunk)
        if size == 0 or digest.hexdigest() != request.file_sha256:
            raise HTTPException(422, detail={"code": "MANUAL_EXCEL_FILE_CHANGED",
                "message": "上传文件为空或哈希不一致。"})
        return execute_dataset_write(db=db, user=user, operation="manual_revision_publish",
                                     payload=request, artifact_path=path)
