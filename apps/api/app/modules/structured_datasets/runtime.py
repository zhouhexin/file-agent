"""受控结构化数据集写入运行时，包括异步目标格式准备审计。"""

from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import ChangeItem, ChangeSet, FilesystemJob, ToolInvocation, StructuredDatasetTask, StructuredDataset, StructuredDatasetPublication, utcnow
from app.modules.agent.repository import AgentRunRepository
from app.modules.conversations.repository import ConversationRepository
from app.modules.file_lifecycle.storage import FileLifecycleStorageService

from .schemas import DatasetAppend, DatasetCreate, DatasetCreateFromExcel
from .service import StructuredDatasetService
from .task_schemas import TaskPreview, TaskConfirm, TaskCommit
from .tasks import DatasetTaskService
from .writeback import ExcelWritebackService
from .writeback_schemas import WritebackCheckout, WritebackPreview, WritebackConfirm, WritebackPublish, ManualRevisionPublish
from .correction import ExcelCorrectionService
from .manual_revision import ManualRevisionService
from .correction_schemas import CorrectionCheckout, CorrectionPublish
from .publication import finalize_publication
from app.modules.funding_usage.schemas import (FundingUsagePublish, FundingUsageContinuePublish,
                                               FundingUsageRefreshPublish, FundingSourceSummarySyncPublish)
from app.modules.funding_usage.service import FundingUsageService


class DispatchState(TypedDict):
    """图状态仅保存轻量执行状态。"""

    status: str


def execute_dataset_write(*, db, user, operation, payload=None, dataset_id=None, task_id=None,
                          artifact_path=None, candidate_payload=None):
    """经固定 schema、白名单 dispatch 和审计边界执行数据集写操作。"""

    schemas = {
        "create": DatasetCreate,
        "create_from_excel": DatasetCreateFromExcel,
        "append": DatasetAppend,
        "export": None,
        "task_preview": TaskPreview,
        "task_confirm": TaskConfirm,
        "task_commit": TaskCommit,
        "writeback_checkout": WritebackCheckout,
        "writeback_preview": WritebackPreview,
        "writeback_confirm": WritebackConfirm,
        "writeback_publish": WritebackPublish,
        "correction_checkout": CorrectionCheckout,
        "correction_preview": WritebackPreview,
        "correction_confirm": WritebackConfirm,
        "correction_publish": CorrectionPublish,
        "manual_revision_publish": ManualRevisionPublish,
        "funding_usage_publish": FundingUsagePublish,
        "funding_usage_continue_publish": FundingUsageContinuePublish,
        "funding_usage_refresh_publish": FundingUsageRefreshPublish,
        "funding_source_summary_publish": FundingSourceSummarySyncPublish,
    }
    if operation not in schemas:
        raise ValueError("未知数据集写操作")
    if schemas[operation] is not None:
        payload = schemas[operation].model_validate(payload)

    conversation_repo = ConversationRepository(db)
    conversation_id = str(uuid5(NAMESPACE_URL, f"file-agent:dataset-audit:{user.id}"))
    try:
        with db.begin_nested():
            conversation = conversation_repo.ensure_conversation(conversation_id, user.id)
    except IntegrityError:
        conversation = conversation_repo.ensure_conversation(conversation_id, user.id)
    conversation.workspace_id = user.default_workspace_id
    message = conversation_repo.create_user_message(
        conversation_id, user.id, f"共享表格：{operation}", attachments=[]
    )
    message.role = "SYSTEM_AUDIT"
    run = AgentRunRepository(db).create_run(conversation_id, message.id, user.id)
    run.intent = "STRUCTURED_DATASET"
    run.status = "RUNNING_TOOL"
    tool_name = f"structured_dataset_{operation}"
    input_summary = {"dataset_id": dataset_id, "task_id": task_id}
    if operation == "append":
        input_summary.update(
            document_id=str(payload.document_id),
            document_version_id=str(payload.document_version_id),
            record_count=len(payload.records),
        )
    elif operation == "create_from_excel":
        input_summary.update(
            document_id=str(payload.document_id),
            document_version_id=(str(payload.document_version_id) if payload.document_version_id else None),
            extraction_run_id=(str(payload.extraction_run_id) if payload.extraction_run_id else None),
            sheet_name=payload.sheet_name,
            legacy_target_policy=payload.legacy_target_policy,
        )
    invocation = ToolInvocation(
        agent_run_id=run.id,
        tool_name=tool_name,
        input_json=input_summary,
        output_json={},
        status="RUNNING",
    )
    db.add(invocation)
    db.flush()
    service = StructuredDatasetService(db, user, run)
    tasks = DatasetTaskService(db, user, run)
    writeback = ExcelWritebackService(db, user, run)
    correction = ExcelCorrectionService(db, user, run)
    manual_revision = ManualRevisionService(db, user, run)
    funding_usage = FundingUsageService(db, user, run)
    result = {}
    visible_cleanup = {}
    publication_id = None
    error = None

    def dispatch(_state: DispatchState):
        nonlocal result
        handlers = {
            "create": lambda: service.create(payload),
            "create_from_excel": lambda: service.create_from_excel(payload),
            "append": lambda: service.append(dataset_id, payload),
            "export": lambda: service.export(dataset_id),
            "task_preview": lambda: tasks.preview(payload),
            "task_confirm": lambda: tasks.confirm(task_id, payload),
            "task_commit": lambda: tasks.commit(task_id, payload),
            "writeback_checkout": lambda: writeback.checkout(payload),
            "writeback_preview": lambda: writeback.preview(task_id, payload),
            "writeback_confirm": lambda: writeback.confirm(task_id, payload),
            "writeback_publish": lambda: writeback.publish(
                task_id, result_digest=payload.result_digest, file_sha256=payload.file_sha256,
                payload=candidate_payload, file_path=artifact_path),
            "correction_checkout": lambda: correction.checkout(payload),
            "correction_preview": lambda: correction.preview(task_id, payload),
            "correction_confirm": lambda: correction.confirm(task_id, payload),
            "correction_publish": lambda: correction.publish(
                task_id, result_digest=payload.result_digest, file_sha256=payload.file_sha256,
                payload=candidate_payload, file_path=artifact_path),
            "manual_revision_publish": lambda: manual_revision.publish(payload, artifact_path),
            "funding_usage_publish": lambda: funding_usage.publish(payload),
            "funding_usage_continue_publish": lambda: funding_usage.continue_publish(payload),
            "funding_usage_refresh_publish": lambda: funding_usage.refresh_publish(payload),
            "funding_source_summary_publish": lambda: funding_usage.source_summary_publish(payload),
        }
        result = handlers[operation]()
        return {"status": "COMPLETED"}

    graph = StateGraph(DispatchState)
    graph.add_node("tool-dispatch", dispatch)
    graph.add_edge(START, "tool-dispatch")
    graph.add_edge("tool-dispatch", END)
    try:
        with db.begin_nested():
            graph.compile().invoke({"status": "RUNNING_TOOL"})
            publication_id = result.pop("_publication_id", None)
            visible_cleanup = dict(result.pop("_visible_cleanup", {}) or {})
            safe_keys = (
                "dataset_id", "revision", "record_count", "image_count", "appended_revision",
                "review_field_count", "record_count_added", "export_id", "working_copy_id",
                "document_id", "document_version_id", "version_number", "filename", "sheet_name",
                "reused", "original_unchanged", "kind", "status", "filesystem_job_id",
                "source_filename", "target_filename", "source_document_id",
                "source_document_version_id", "converted_from_legacy", "conversion_artifact_id",
                "task_id", "excel_published", "task_record_count", "task_cell_count", "checkmark_count",
                "base_sha256", "base_revision", "download_url", "labels", "layout", "review_mode",
                "record_id", "row_number", "corrected_record_id", "corrected_cell_count", "mode",
                "manual_changed_cell_count",
                "person_count",
                "added_record_count",
                "changed_count", "review_count",
            )
            safe_result = {key: result[key] for key in safe_keys if key in result}
            is_job = result.get("kind") == "filesystem_job"
            is_async = is_job or bool(publication_id)
            changeset_id = result.get("changeset_id")
            if not changeset_id:
                changeset = ChangeSet(
                    agent_run_id=run.id,
                    conversation_id=conversation_id,
                    user_id=user.id,
                    status="PENDING" if is_async else "COMPLETED",
                    summary=(
                        "旧版 Excel 目标表格式提升已进入异步队列。"
                        if is_async else f"共享表格 {operation} 已完成。"
                    ),
                )
                db.add(changeset)
                db.flush()
                changeset_id = changeset.id
                if is_job:
                    target_type = "FILESYSTEM_JOB"
                    target_id = result.get("filesystem_job_id")
                    change_type = "STRUCTURED_DATASET_TARGET_PREPARATION_QUEUED"
                else:
                    target_type = "DATASET_TASK" if operation.startswith(("task_", "writeback_", "correction_", "manual_revision_")) else "DATASET_EXPORT" if operation == "export" else "DATASET"
                    target_id = result.get("task_id", result.get("export_id", result.get("dataset_id")))
                    change_type = (
                        ("EXPORT_REUSED" if result.get("reused") else "EXPORT_CREATED")
                        if operation == "export"
                        else "STRUCTURED_DATASET_REUSED"
                        if result.get("reused")
                        else f"STRUCTURED_DATASET_{operation.upper()}"
                    )
                db.add(ChangeItem(
                    changeset_id=changeset.id,
                    target_type=target_type,
                    target_id=target_id,
                    change_type=change_type,
                    after_value_json=safe_result,
                    source="workbuddy-excel-writeback" if operation.startswith(("writeback_", "correction_", "manual_revision_")) else "workbuddy-dataset-demo",
                    execution_status="PENDING" if is_async else "COMPLETED",
                ))
                if operation in {"export", "writeback_publish", "correction_publish", "manual_revision_publish", "funding_usage_publish", "funding_usage_continue_publish", "funding_usage_refresh_publish", "funding_source_summary_publish"} and result.get("working_copy_id"):
                    db.add(ChangeItem(
                        changeset_id=changeset.id,
                        target_type="working_copy",
                        target_id=result["working_copy_id"],
                        target_document_id=result.get("document_id"),
                        change_type=("DOCUMENT_VERSION_REUSED" if result.get("reused") else "DOCUMENT_VERSION_CREATED"),
                        after_value_json={
                            "working_copy_id": result["working_copy_id"],
                            "document_version_id": result.get("document_version_id"),
                            "version_number": result.get("version_number"),
                            "dataset_revision": result.get("revision"),
                        },
                        source="workbuddy-excel-writeback" if operation in {"writeback_publish", "correction_publish", "manual_revision_publish"} else "workbuddy-dataset-demo",
                        confidence=1.0,
                        evidence_json={},
                        execution_status="COMPLETED",
                    ))
            run.changeset_id = changeset_id
            invocation.changeset_id = changeset_id
            invocation.output_json = safe_result
            if is_job:
                job = db.get(FilesystemJob, result["filesystem_job_id"])
                if job is not None:
                    job.payload_json = {
                        **dict(job.payload_json or {}),
                        "agent_run_id": run.id,
                        "tool_invocation_id": invocation.id,
                        "changeset_id": changeset_id,
                    }
    except HTTPException as exc:
        error = exc
    except IntegrityError:
        error = HTTPException(
            409,
            detail={"code": "DATASET_CONCURRENT_WRITE", "message": "并发写入冲突，请使用原提交键重试。"},
        )
    except Exception as exc:
        log_event(
            "structured_dataset.write.exception",
            level="ERROR",
            agent_run_id=run.id,
            user_id=user.id,
            tool_name=tool_name,
            status="FAILED",
            error_code="DATASET_WRITE_FAILED",
            exception_type=type(exc).__name__,
        )
        error = HTTPException(
            500,
            detail={"code": "DATASET_WRITE_FAILED", "message": "本次操作失败，已保存记录不受影响，可重试。"},
        )
    waiting = not error and (result.get("kind") == "filesystem_job" or bool(publication_id))
    run.status = "FAILED" if error else "PUBLISHING" if publication_id else "WAITING_FOR_ASYNC_JOB" if waiting else "COMPLETED"
    invocation.status = "FAILED" if error else "RUNNING" if waiting else "COMPLETED"
    run.graph_state_json = {"status": run.status}
    run.final_response = (
        "表格操作失败。"
        if error
        else "旧版 Excel 正在异步提升为 XLSX，原文件尚未修改。"
        if waiting
        else "表格候选已登记，等待用户确认。"
        if result.get("status") == "WAITING_CONFIRMATION"
        else "表格任务尚未发布，请按任务回执继续处理。"
        if operation.startswith("task_") and result.get("status") != "COMPLETED"
        else "共享表格操作已完成。"
    )
    if error:
        invocation.output_json = {"error": error.detail}
        run.error_message = (
            str(error.detail.get("code", "DATASET_WRITE_FAILED"))
            if isinstance(error.detail, dict) else "DATASET_WRITE_FAILED"
        )
        if operation in {"writeback_publish", "correction_publish", "manual_revision_publish"} and task_id:
            task = db.query(StructuredDatasetTask).filter_by(id=str(task_id), user_id=user.id).with_for_update().one_or_none()
            if task is not None and task.request_key.startswith(("writeback:", "correction:")):
                detail = error.detail if isinstance(error.detail, dict) else {}
                task.error_json = {key: detail[key] for key in ("code", "message", "cell", "field_label") if key in detail}
        if operation == "task_commit" and task_id:
            # 保存点只回滚当前阶段；已提交的 append 进度保留，下一次继续同一任务。
            task = db.query(StructuredDatasetTask).filter_by(id=str(task_id), user_id=user.id).with_for_update().one_or_none()
            if task is not None:
                detail = error.detail if isinstance(error.detail, dict) else {}
                task.error_json = {key: detail[key] for key in (
                    "code", "message", "record_index", "field_key", "field_label", "column"
                ) if key in detail}
                task.error_json.setdefault("code", run.error_message)
                # 尚未追加任何记录时失败可释放占用，让用户修正并重新预览；已追加的发布失败仍保留恢复锁。
                progress = task.progress_json or {}
                if not progress.get("revisions"):
                    if progress.get("dataset_id"):
                        target = db.query(StructuredDataset).filter_by(id=progress["dataset_id"]).with_for_update().one_or_none()
                        if target and target.active_task_id == task.id:
                            target.active_task_id = None
                    if run.error_message.startswith(("APPROVAL_", "EVIDENCE_", "REQUIREMENT_", "SOURCE_ONLY_")) or run.error_message in {
                        "FIELD_TITLE_CHANGED", "CHECKMARK_TEXT_FIELD_REQUIRED", "DATASET_RECORDS_NEED_REVIEW",
                    }:
                        # 已确认草稿不可在原任务内静默修补；须重新提取、预览并确认新摘要。
                        task.status = "NEEDS_INPUT"
    invocation.finished_at = None if waiting else utcnow()
    database_bind = db.get_bind()
    try:
        db.commit()
    except Exception as commit_error:
        if not publication_id or error:
            raise
        # 提交返回失败不等于事务未提交；放弃故障会话后用新会话读取真实状态。
        db.close()
        with Session(bind=database_bind) as recovery_db:
            publication = recovery_db.get(StructuredDatasetPublication, publication_id)
            if publication is None:
                log_event("structured_dataset.publication.commit_unconfirmed", level="ERROR",
                          status="FAILED", error_code=type(commit_error).__name__)
                raise HTTPException(503, detail={
                    "code": "DATASET_COMMIT_UNCONFIRMED",
                    "message": "数据库提交结果未确认，未改动同名可见文件；请用原任务或请求键查询状态后重试。",
                }) from commit_error
            recovered = finalize_publication(recovery_db, publication_id)
            result = {**result, **(publication.result_json or {}),
                      "status": "COMPLETED" if recovered else "PUBLISHING",
                      "excel_published": recovered, "saved_to_server": recovered}
            return result
    if not error and publication_id:
        published = finalize_publication(db, publication_id)
        if published:
            published_row = db.get(StructuredDatasetPublication, publication_id)
            archived_dataset_id = (published_row.result_json or {}).get("archived_dataset_id") if published_row else None
            if archived_dataset_id:
                result = {**result, "archived_dataset_id": archived_dataset_id}
            if operation == "writeback_publish":
                result = ExcelWritebackService(db, user).receipt(ExcelWritebackService(db, user).owned(task_id))
            elif operation == "correction_publish":
                result = ExcelCorrectionService(db, user).receipt(ExcelCorrectionService(db, user).owned(task_id))
            elif operation == "manual_revision_publish":
                task = db.get(StructuredDatasetTask, result["task_id"])
                result = ManualRevisionService(db, user).receipt(task)
            elif operation == "task_commit":
                result = DatasetTaskService(db, user).receipt(DatasetTaskService(db, user).owned(task_id))
            elif operation in {"funding_usage_publish", "funding_usage_continue_publish", "funding_usage_refresh_publish", "funding_source_summary_publish"}:
                result = {**result, "status": "COMPLETED", "excel_published": True,
                          "saved_to_server": True}
            else:
                result = {**result, "excel_published": True}
        else:
            result = {**result, "status": "PUBLISHING", "excel_published": False,
                      "saved_to_server": False,
                      "publication_id": publication_id}
    if not error and visible_cleanup and not publication_id:
        try:
            FileLifecycleStorageService().retire_dataset_legacy_current_file(**visible_cleanup)
        except Exception as exc:
            log_event(
                "structured_dataset.visible_cleanup_failed",
                level="ERROR",
                agent_run_id=run.id,
                user_id=user.id,
                tool_name=tool_name,
                status="NEEDS_REPAIR",
                error_code=type(exc).__name__,
                message="累计目标表已发布，但旧可见入口清理失败。",
            )
    log_event(
        "structured_dataset.write",
        agent_run_id=run.id,
        user_id=user.id,
        tool_name=tool_name,
        status=run.status,
        error_code=run.error_message,
    )
    if error:
        raise error
    return result
