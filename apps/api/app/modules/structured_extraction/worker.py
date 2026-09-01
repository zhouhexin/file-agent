"""STRUCTURED_EXTRACTION 队列 handler 与原 AgentRun 恢复。"""

from __future__ import annotations

import multiprocessing
import queue
from datetime import timezone
from typing import Any

from app.core.database import SessionLocal
from app.core.config import get_settings
from app.core.logging import log_event
from app.db.models import AgentRun, FilesystemJob, StructuredExtractionRun, ToolInvocation, utcnow
from app.modules.managed_files.jobs import FilesystemJobQueue
from app.modules.structured_extraction.autonomous_loop import StructuredExtractionAutonomousLoop
from app.modules.structured_extraction.repository import StructuredExtractionRepository
from app.modules.structured_extraction.service import StructuredExtractionService


class StructuredExtractionTaskTimeoutError(TimeoutError):
    """重型结构化抽取超过任务级总时限。"""


def process_structured_extraction_job_isolated(
    *,
    job_id: str,
    execution_token: str,
    worker_id: str,
    attempt_count: int,
    timeout_seconds: int,
) -> None:
    """在可终止子进程中执行重型任务，避免第三方 SDK 永久占住 Worker。"""

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue(maxsize=1)
    process = context.Process(
        target=_isolated_worker_entry,
        args=(job_id, execution_token, worker_id, attempt_count, result_queue),
        name=f"structured-extraction-{job_id}",
        daemon=False,
    )
    process.start()
    process.join(timeout=max(1, int(timeout_seconds)))
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=2)
        result_queue.close()
        log_event(
            "structured_extraction.timed_out",
            level="ERROR",
            filesystem_job_id=job_id,
            status="FAILED",
            error_code="STRUCTURED_EXTRACTION_TIMEOUT",
            timeout_seconds=int(timeout_seconds),
            message="图片结构化抽取超过任务级总时限",
        )
        raise StructuredExtractionTaskTimeoutError(
            f"图片结构化抽取超过 {int(timeout_seconds)} 秒总时限"
        )
    try:
        result = result_queue.get(timeout=2)
    except queue.Empty as exc:
        raise RuntimeError(
            f"图片结构化抽取隔离进程异常退出（exit_code={process.exitcode}）"
        ) from exc
    finally:
        result_queue.close()
    if not isinstance(result, dict) or result.get("ok") is not True:
        error_code = str((result or {}).get("error_code") or "ISOLATED_PROCESS_FAILED")
        raise RuntimeError(f"图片结构化抽取隔离进程失败：{error_code}")


def _isolated_worker_entry(
    job_id: str,
    execution_token: str,
    worker_id: str,
    attempt_count: int,
    result_queue: Any,
) -> None:
    """子进程入口；只回传状态码，不跨进程传递正文、路径或数据库对象。"""

    db = SessionLocal()
    try:
        job = db.get(FilesystemJob, job_id)
        if (
            job is None
            or job.status != "RUNNING"
            or str(job.execution_token or "") != execution_token
            or str(job.lease_owner or "") != worker_id
            or int(job.attempt_count or 0) != int(attempt_count)
        ):
            raise RuntimeError("STRUCTURED_EXTRACTION_EXECUTION_TOKEN_MISMATCH")
        process_structured_extraction_job(
            db=db,
            job=job,
            expected_execution_token=execution_token,
            expected_worker_id=worker_id,
            expected_attempt_count=attempt_count,
        )
        db.commit()
        result_queue.put({"ok": True})
    except Exception as exc:
        db.rollback()
        result_queue.put({"ok": False, "error_code": exc.__class__.__name__})
    finally:
        db.close()


def process_structured_extraction_job(
    *,
    db: Any,
    job: FilesystemJob,
    expected_execution_token: str | None = None,
    expected_worker_id: str | None = None,
    expected_attempt_count: int | None = None,
) -> None:
    """执行一个图片结构化抽取任务，并恢复原始 AgentRun。"""

    payload = dict(job.payload_json or {})
    run_id = str(payload.get("structured_extraction_run_id") or "")
    user_id = str(payload.get("user_id") or job.created_by or "")
    if not run_id or not user_id:
        raise ValueError("STRUCTURED_IMAGE_EXTRACTION 缺少运行或用户标识")
    run = db.get(StructuredExtractionRun, run_id)
    if run is None or run.document_id == "":
        raise ValueError("结构化抽取运行不存在")
    service = StructuredExtractionService(
        db=db,
        user_id=user_id,
        conversation_id=str(payload.get("conversation_id") or "") or None,
        agent_run_id=str(payload.get("agent_run_id") or "") or None,
    )
    try:
        output = service.execute_run(run)
        output, clarification = StructuredExtractionAutonomousLoop(db=db).maybe_enhance(
            run=run,
            initial_output=output,
            service=service,
        )
    except Exception as exc:
        service.repository.fail_run(
            run=run,
            code=str(getattr(exc, "code", "") or exc.__class__.__name__),
            message="图片结构化抽取执行失败，请稍后重试或联系管理员。",
        )
        raise
    if expected_execution_token is not None:
        # 完成提交前重新读取任务租约；已超时或已被新 Worker 重领的旧执行不得覆盖终态。
        db.refresh(
            job,
            attribute_names=["status", "execution_token", "lease_owner", "attempt_count"],
        )
        if (
            job.status != "RUNNING"
            or str(job.execution_token or "") != expected_execution_token
            or (
                expected_worker_id is not None
                and str(job.lease_owner or "") != expected_worker_id
            )
            or (
                expected_attempt_count is not None
                and int(job.attempt_count or 0) != int(expected_attempt_count)
            )
        ):
            raise RuntimeError("STRUCTURED_EXTRACTION_EXECUTION_TOKEN_MISMATCH")
    FilesystemJobQueue(db).mark_completed(
        job=job,
        result={
            "structured_extraction_run_id": output.get("structured_extraction_run_id"),
            "status": output.get("status"),
            "record_count": int(output.get("record_count") or 0),
            "field_count": int(output.get("field_count") or 0),
            "review_count": int(output.get("review_count") or 0),
            "quality_band": output.get("quality_band"),
            "original_unchanged": True,
        },
    )
    _resume_agent_run(
        db=db,
        job=job,
        run=run,
        output=output,
        clarification=clarification,
    )


def fail_structured_extraction_agent_run(
    *,
    db: Any,
    job: FilesystemJob,
    error_message: str,
) -> bool:
    """异步任务终态失败时结束原 AgentRun，避免前端永久等待。"""

    if job.job_type != "STRUCTURED_IMAGE_EXTRACTION":
        return False
    payload = dict(job.payload_json or {})
    structured_run_id = str(payload.get("structured_extraction_run_id") or "")
    structured_run = db.get(StructuredExtractionRun, structured_run_id) if structured_run_id else None
    if structured_run is not None and structured_run.status not in {
        "COMPLETED",
        "PARTIAL",
        "NEEDS_REVIEW",
    }:
        structured_run.status = "FAILED"
        structured_run.error_code = structured_run.error_code or (
            "STRUCTURED_EXTRACTION_TIMEOUT"
            if "超时" in error_message
            else "STRUCTURED_EXTRACTION_ASYNC_FAILED"
        )
        structured_run.error_message = structured_run.error_message or error_message[:2000]
        structured_run.updated_at = utcnow()
    agent_run_id = str(payload.get("agent_run_id") or "")
    run = db.get(AgentRun, agent_run_id) if agent_run_id else None
    if run is None:
        return True
    invocation = _structured_invocation(db=db, agent_run_id=run.id)
    if run.status == "FAILED" and invocation is not None and invocation.status == "FAILED":
        return True
    changeset_id = None
    if structured_run is not None:
        failure_code = (
            structured_run.error_code or "STRUCTURED_EXTRACTION_ASYNC_FAILED"
        )
        changeset_id = StructuredExtractionRepository(db).record_failure_changeset(
            run=structured_run,
            agent_run=run,
            error_code=failure_code,
        )
    else:
        failure_code = "STRUCTURED_EXTRACTION_ASYNC_FAILED"
    if invocation is not None:
        invocation.status = "FAILED"
        invocation.output_json = {
            **dict(invocation.output_json or {}),
            "kind": "structured_image_extraction",
            "ok": False,
            "status": "FAILED",
            "changeset_id": changeset_id,
            "error": {
                "code": failure_code,
                "message": error_message,
                "retryable": False,
                "user_action_required": False,
            },
        }
        invocation.changeset_id = changeset_id
        invocation.finished_at = utcnow()
    graph_state = dict(run.graph_state_json or {})
    graph_state.update(
        {
            "status": "FAILED",
            "async_job_ids": [],
            "final_response": error_message,
            "errors": [error_message],
            "changeset_id": changeset_id or graph_state.get("changeset_id"),
        }
    )
    run.status = "FAILED"
    run.final_response = error_message
    run.error_message = error_message
    run.changeset_id = changeset_id or run.changeset_id
    run.graph_state_json = graph_state
    run.updated_at = utcnow()
    return True


def reconcile_waiting_structured_extraction_runs(
    *,
    session_factory: Any = SessionLocal,
) -> int:
    """按 Job 真实终态补偿等待中的结构化 AgentRun，并收敛历史超时任务。"""

    db = session_factory()
    changed = 0
    try:
        waiting_runs = (
            db.query(AgentRun)
            .filter(AgentRun.status == "WAITING_FOR_ASYNC_JOB")
            .all()
        )
        timeout_seconds = int(get_settings().structured_extraction_task_timeout_seconds)
        for agent_run in waiting_runs:
            invocation = _structured_invocation(db=db, agent_run_id=agent_run.id)
            if invocation is None:
                continue
            graph_state = dict(agent_run.graph_state_json or {})
            job_ids = [str(value) for value in graph_state.get("async_job_ids", []) if str(value)]
            jobs = (
                db.query(FilesystemJob)
                .filter(
                    FilesystemJob.id.in_(job_ids),
                    FilesystemJob.job_type == "STRUCTURED_IMAGE_EXTRACTION",
                )
                .all()
                if job_ids
                else []
            )
            if not jobs:
                continue
            for job in jobs:
                if job.status in {"PENDING", "RUNNING"} and _structured_job_timed_out(
                    job=job,
                    timeout_seconds=timeout_seconds,
                ):
                    message = "图片结构化抽取处理超时，原始文件未修改；请缩小范围后重试或联系管理员。"
                    FilesystemJobQueue(db).mark_failed(
                        job=job,
                        error_message=message,
                        event_details={"error_code": "STRUCTURED_EXTRACTION_TIMEOUT"},
                    )
                    fail_structured_extraction_agent_run(
                        db=db,
                        job=job,
                        error_message=message,
                    )
                    changed += 1
                    break
                if job.status == "FAILED":
                    fail_structured_extraction_agent_run(
                        db=db,
                        job=job,
                        error_message=str(job.error_message or "图片结构化抽取失败。"),
                    )
                    changed += 1
                    break
                if job.status == "COMPLETED":
                    payload = dict(job.payload_json or {})
                    structured_run = db.get(
                        StructuredExtractionRun,
                        str(payload.get("structured_extraction_run_id") or ""),
                    )
                    if structured_run is None:
                        continue
                    output = dict(invocation.output_json or {})
                    if output.get("kind") != "structured_image_extraction" or output.get("ok") is not True:
                        service = StructuredExtractionService(
                            db=db,
                            user_id=str(payload.get("user_id") or job.created_by or ""),
                            conversation_id=str(payload.get("conversation_id") or "") or None,
                            agent_run_id=str(agent_run.id),
                        )
                        output = service._tool_output(  # noqa: SLF001 - 一致性补偿复用同一输出契约
                            run=structured_run,
                            result=service.result_for_run(structured_run),
                            reused=True,
                        )
                    _resume_agent_run(
                        db=db,
                        job=job,
                        run=structured_run,
                        output=output,
                        clarification=None,
                    )
                    changed += 1
                    break
        db.commit()
        return changed
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _structured_job_timed_out(*, job: FilesystemJob, timeout_seconds: int) -> bool:
    """以首次开始时间为总时限，不允许心跳无限延长业务任务。"""

    started_at = job.started_at or job.created_at
    if started_at is None:
        return False
    now = utcnow()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (now - started_at).total_seconds() >= max(1, timeout_seconds)


def _resume_agent_run(
    *,
    db: Any,
    job: FilesystemJob,
    run: StructuredExtractionRun,
    output: dict[str, Any],
    clarification: str | None,
) -> None:
    """以同一消息和 ToolInvocation 写回最终事实，不创建重复聊天消息。"""

    agent_run = db.get(AgentRun, run.agent_run_id) if run.agent_run_id else None
    if agent_run is None:
        return
    invocation = _structured_invocation(db=db, agent_run_id=agent_run.id)
    if invocation is not None:
        invocation.output_json = output
        invocation.status = str(output.get("status") or "COMPLETED")
        invocation.changeset_id = output.get("changeset_id")
        invocation.finished_at = utcnow()
    graph_state = dict(agent_run.graph_state_json or {})
    graph_state["async_job_ids"] = [
        str(value)
        for value in graph_state.get("async_job_ids", [])
        if str(value) != str(job.id)
    ]
    graph_output = _structured_graph_summary(output)
    graph_state["tool_results"] = _replace_pending_result(
        list(graph_state.get("tool_results") or []),
        run_id=run.id,
        output=graph_output,
    )
    result_summary = dict(graph_state.get("result_summary") or {})
    result_summary["structured_extraction"] = graph_output
    result_summary.pop("filesystem_job", None)
    graph_state["result_summary"] = result_summary
    if clarification:
        final_response = clarification
        status = "NEEDS_REVIEW"
    else:
        final_response = (
            f"已从图片中提取 {int(output.get('record_count') or 0)} 条记录，"
            f"{int(output.get('review_count') or 0)} 个字段需要复核；原始文件未修改。"
        )
        status = "COMPLETED" if output.get("status") == "COMPLETED" else "NEEDS_REVIEW"
    graph_state.update(
        {
            "status": status,
            "final_response": final_response,
            # 一致性补偿重试成功后必须清除旧失败快照，避免 UI 继续展示已恢复的错误。
            "errors": [],
            "changeset_id": output.get("changeset_id") or graph_state.get("changeset_id"),
        }
    )
    agent_run.status = status
    agent_run.final_response = final_response
    agent_run.error_message = None
    agent_run.changeset_id = output.get("changeset_id") or agent_run.changeset_id
    agent_run.graph_state_json = graph_state
    agent_run.updated_at = utcnow()
    log_event(
        "structured_extraction.agent_resumed",
        agent_run_id=agent_run.id,
        filesystem_job_id=job.id,
        document_id=run.document_id,
        status=status,
        message="结构化抽取结果已恢复到原 AgentRun",
    )


def _structured_invocation(*, db: Any, agent_run_id: str) -> ToolInvocation | None:
    """读取初始结构化抽取 ToolInvocation。"""

    return (
        db.query(ToolInvocation)
        .filter(
            ToolInvocation.agent_run_id == agent_run_id,
            ToolInvocation.tool_name == "extract-image-structured-data",
        )
        .order_by(ToolInvocation.created_at.asc())
        .first()
    )


def _replace_pending_result(
    results: list[dict[str, Any]],
    *,
    run_id: str,
    output: dict[str, Any],
) -> list[dict[str, Any]]:
    """替换同一抽取运行的等待结果，保持其他 Tool 事实不变。"""

    replaced = False
    updated: list[dict[str, Any]] = []
    for item in results:
        if str(item.get("structured_extraction_run_id") or "") == run_id:
            updated.append(output)
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(output)
    return updated


def _structured_graph_summary(output: dict[str, Any]) -> dict[str, Any]:
    """Graph State 只保存轻量业务摘要，不复制字段值、OCR 文本或 bbox。"""

    return {
        "kind": "structured_image_extraction",
        "ok": output.get("ok") is True,
        "status": str(output.get("status") or "FAILED"),
        "document_id": str(output.get("document_id") or ""),
        "structured_extraction_run_id": str(
            output.get("structured_extraction_run_id") or ""
        ),
        "schema_mode": str(output.get("schema_mode") or ""),
        "record_mode": str(output.get("record_mode") or ""),
        "presentation": str(output.get("presentation") or ""),
        "record_count": int(output.get("record_count") or 0),
        "field_count": int(output.get("field_count") or 0),
        "review_count": int(output.get("review_count") or 0),
        "missing_required_field_count": int(
            output.get("missing_required_field_count") or 0
        ),
        "quality_band": str(output.get("quality_band") or "LOW"),
        "original_unchanged": True,
        "changeset_id": output.get("changeset_id"),
    }
