"""受管目录异步 worker。

该模块负责消费 filesystem_jobs 中的只读扫描任务，
避免聊天请求线程直接遍历服务器目录。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.core.logging import log_context, log_event, new_request_id
from app.db.models import AgentRun, FilesystemJob, ToolInvocation, utcnow
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.changesets.service import persist_changeset_from_document_results
from app.modules.classification.classifier_service import DocumentClassificationService
from app.modules.classification.result_builder import (
    build_document_results_from_extraction_results,
    format_document_results_response,
)
from app.modules.classification.service import persist_document_results_classifications
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.managed_files.repository import FilesystemJobRepository, ManagedFileRepository
from app.modules.managed_files.scanner import ManagedFileScanner


def process_next_filesystem_job(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    worker_id: str = "filesystem-worker",
) -> str | None:
    """处理一个待执行文件系统任务，没有任务时返回 None。"""

    db = session_factory()
    try:
        queue = FilesystemJobQueue(db)
        job = queue.claim_next(worker_id=worker_id)
        if job is None:
            db.commit()
            return None

        job_id = str(job.id)
        with log_context(request_id=new_request_id()):
            log_event(
                "filesystem.worker.started",
                agent_run_id=job_id,
                job_id=job_id,
                status=job.status,
                message="文件系统任务开始执行",
            )
            try:
                _process_job(db=db, job=job)
                db.commit()
                log_event(
                    "filesystem.worker.completed",
                    agent_run_id=job_id,
                    job_id=job_id,
                    status=job.status,
                    message="文件系统任务执行完成",
                )
            except Exception as exc:
                db.rollback()
                failed_db = session_factory()
                try:
                    failed_job = failed_db.get(FilesystemJob, job_id)
                    if failed_job is not None:
                        public_error = _public_job_error_message(job=failed_job, error=exc)
                        FilesystemJobQueue(failed_db).mark_failed(
                            job=failed_job,
                            error_message=public_error,
                        )
                        _mark_agent_run_failed_for_job(
                            db=failed_db,
                            job=failed_job,
                            error_message=public_error,
                        )
                        failed_db.commit()
                    log_event(
                        "filesystem.worker.failed",
                        level="ERROR",
                        agent_run_id=job_id,
                        job_id=job_id,
                        status="FAILED",
                        error_code=exc.__class__.__name__,
                        message="文件系统任务执行失败，详细堆栈仅保留在服务端异常日志中",
                    )
                finally:
                    failed_db.close()
                raise
        return job_id
    finally:
        db.close()


def run_filesystem_worker(
    *,
    session_factory: Callable[[], Session] = SessionLocal,
    worker_id: str = "filesystem-worker",
    poll_seconds: float = 3.0,
) -> None:
    """持续轮询数据库任务队列。"""

    while True:
        processed = process_next_filesystem_job(session_factory=session_factory, worker_id=worker_id)
        if processed is None:
            time.sleep(poll_seconds)


def _process_job(*, db: Session, job: FilesystemJob) -> None:
    """按任务类型执行具体处理逻辑。"""

    if job.job_type == "CLASSIFY_MANAGED_FILES":
        _process_managed_file_classification_job(db=db, job=job)
        return
    if job.job_type != "SCAN_MANAGED_ROOT":
        raise ValueError(f"Unsupported filesystem job type: {job.job_type}")
    if not job.root_id:
        raise ValueError("SCAN_MANAGED_ROOT 缺少 root_id")
    root = ManagedFileRepository(db).get_root(job.root_id)
    if root is None or not root.enabled:
        raise ValueError("Managed root not found")
    scan_run = ManagedFileScanner(db).scan_root(root, job_id=job.id)
    FilesystemJobQueue(db).mark_completed(
        job=job,
        result={
            "scan_run_id": scan_run.id,
            "files_discovered": scan_run.files_discovered,
            "files_updated": scan_run.files_updated,
            "files_missing": scan_run.files_missing,
            "errors": scan_run.errors,
        },
    )


def _mark_agent_run_failed_for_job(
    *,
    db: Session,
    job: FilesystemJob,
    error_message: str,
) -> None:
    """异步分类任务整体失败时同步结束原 AgentRun，避免前端永久等待。"""

    if job.job_type != "CLASSIFY_MANAGED_FILES":
        return
    agent_run_id = str((job.payload_json or {}).get("agent_run_id") or "")
    run = db.get(AgentRun, agent_run_id) if agent_run_id else None
    if run is None:
        return
    run.status = "FAILED"
    run.error_message = error_message
    run.final_response = f"受管文件后台分类失败：{error_message}"
    graph_state = dict(run.graph_state_json or {})
    graph_state.update(
        {
            "status": "FAILED",
            "final_response": run.final_response,
            "errors": [error_message],
        }
    )
    run.graph_state_json = graph_state
    run.updated_at = utcnow()
    invocation = (
        db.query(ToolInvocation)
        .filter(ToolInvocation.agent_run_id == run.id)
        .filter(ToolInvocation.tool_name == "classify-managed-files")
        .order_by(ToolInvocation.created_at.desc())
        .first()
    )
    if invocation is not None:
        invocation.status = "FAILED"
        invocation.output_json = {
            **dict(invocation.output_json or {}),
            "status": "FAILED",
            "job_status": "FAILED",
            "error": {"code": "ASYNC_CLASSIFICATION_FAILED", "message": error_message},
        }
        invocation.finished_at = utcnow()


def _process_managed_file_classification_job(*, db: Session, job: FilesystemJob) -> None:
    """按逻辑范围处理大批量受管文件，并把结果回写原 AgentRun。"""

    payload = dict(job.payload_json or {})
    user_id = str(payload.get("user_id") or job.created_by or "")
    agent_run_id = str(payload.get("agent_run_id") or "")
    if not user_id or not agent_run_id:
        raise ValueError("CLASSIFY_MANAGED_FILES 缺少 user_id 或 agent_run_id")
    run = db.get(AgentRun, agent_run_id)
    if run is None or run.user_id != user_id:
        raise ValueError("CLASSIFY_MANAGED_FILES 对应的 AgentRun 不存在")

    rows = _load_managed_classification_rows(db=db, payload=payload)
    job.progress_total = len(rows)
    job.progress_current = 0
    db.flush()
    registry = ToolRegistry(db=db, user_id=user_id)
    classification_service = DocumentClassificationService(db=db)
    document_results: list[dict] = []
    for managed_file, root in rows:
        try:
            with db.begin_nested():
                invocation = registry.invoke(
                    "managed-file-read-document",
                    {
                        "root_key": root.root_key,
                        "relative_path": managed_file.relative_path,
                        "force_reprocess": bool(payload.get("force_reprocess", False)),
                        "scan_before_read": False,
                    },
                )
                extraction_result = dict(invocation.output_json)
                extraction_result["source"] = "classify-managed-files"
                extraction_result["classification_force_reprocess"] = bool(
                    payload.get("force_reprocess", False)
                )
                item_results = build_document_results_from_extraction_results(
                    extraction_results=[extraction_result],
                    context_documents=[],
                    classification_service=classification_service,
                    include_categories=True,
                )
        except Exception as exc:
            item_results = [
                {
                    "document_id": "",
                    "filename": managed_file.filename,
                    "extraction_status": "FAILED",
                    "source_kind": "managed_file",
                    "source": "classify-managed-files",
                    "managed_file_id": managed_file.id,
                    "root_key": root.root_key,
                    "relative_path": managed_file.relative_path,
                    "categories": [],
                    "warnings": [],
                    "errors": [
                        {
                            "code": exc.__class__.__name__,
                            "message": "受管文件分类失败，请稍后重试或联系管理员。",
                        }
                    ],
                }
            ]
            FilesystemJobRepository(db).create_event(
                job_id=job.id,
                level="ERROR",
                message="单个受管文件分类失败",
                details={
                    "root_key": root.root_key,
                    "relative_path": managed_file.relative_path,
                    "error_code": exc.__class__.__name__,
                },
            )
        document_results.extend(item_results)
        job.progress_current += 1
        job.updated_at = utcnow()
        db.flush()

    persist_document_results_classifications(
        db=db,
        agent_run_id=run.id,
        document_results=document_results,
    )
    changeset = persist_changeset_from_document_results(
        db=db,
        run=run,
        document_results=document_results,
    )
    completed_count = len(
        [item for item in document_results if item.get("extraction_status") == "COMPLETED"]
    )
    failed_count = len(document_results) - completed_count
    run.status = "FAILED" if completed_count == 0 and failed_count else "COMPLETED"
    run.final_response = format_document_results_response(document_results)
    graph_state = dict(run.graph_state_json or {})
    graph_state.update(
        {
            "status": run.status,
            "document_results": document_results,
            "result_summary": {
                **dict(graph_state.get("result_summary") or {}),
                "document_results": document_results,
            },
            "final_response": run.final_response,
            "changeset_id": changeset.id if changeset is not None else None,
        }
    )
    run.graph_state_json = graph_state
    run.changeset_id = changeset.id if changeset is not None else None
    run.updated_at = utcnow()
    invocation = (
        db.query(ToolInvocation)
        .filter(ToolInvocation.agent_run_id == run.id)
        .filter(ToolInvocation.tool_name == "classify-managed-files")
        .order_by(ToolInvocation.created_at.desc())
        .first()
    )
    if invocation is not None:
        invocation.output_json = {
            **dict(invocation.output_json or {}),
            "status": "COMPLETED" if completed_count else "FAILED",
            "job_status": "COMPLETED",
            "matched_count": len(document_results),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "changeset_id": run.changeset_id,
        }
        invocation.status = "COMPLETED" if completed_count else "FAILED"
        invocation.changeset_id = run.changeset_id
        invocation.finished_at = utcnow()
    FilesystemJobQueue(db).mark_completed(
        job=job,
        result={
            "agent_run_id": run.id,
            "matched_count": len(document_results),
            "completed_count": completed_count,
            "failed_count": failed_count,
            "changeset_id": run.changeset_id,
        },
    )


def _load_managed_classification_rows(*, db: Session, payload: dict) -> list[tuple]:
    """分页读取异步分类范围内的活动受管文件，排除隐藏项和缺失项。"""

    repository = ManagedFileRepository(db)
    rows: list[tuple] = []
    offset = 0
    page_size = get_settings().managed_file_classification_batch_size
    while True:
        page = repository.list_files(
            root_key=payload.get("root_key"),
            root_keys=(
                list(payload.get("configured_root_keys") or [])
                if payload.get("root_key") is None
                else None
            ),
            path_prefix=payload.get("path_prefix"),
            extension=payload.get("extension"),
            filename_contains=payload.get("filename_contains"),
            status="ACTIVE",
            limit=page_size,
            offset=offset,
        )
        rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    if payload.get("recursive", True) is False and payload.get("path_prefix"):
        prefix_depth = str(payload["path_prefix"]).strip("/").count("/") + 1
        rows = [
            row
            for row in rows
            if str(row[0].relative_path).count("/") == prefix_depth
        ]
    return rows


def _public_job_error_message(*, job: FilesystemJob, error: Exception) -> str:
    """普通用户可查询的分类 Job 不返回底层路径、连接信息或异常细节。"""

    if job.job_type == "CLASSIFY_MANAGED_FILES":
        return "受管文件后台分类失败，请稍后重试或联系管理员。"
    return str(error)[:2000] or "文件系统任务执行失败。"


def main() -> None:
    """worker 命令行入口。"""

    worker_id = os.getenv("FILESYSTEM_WORKER_ID", "filesystem-worker")
    poll_seconds = float(os.getenv("FILESYSTEM_WORKER_POLL_SECONDS", "3"))
    run_filesystem_worker(worker_id=worker_id, poll_seconds=poll_seconds)


if __name__ == "__main__":
    main()
