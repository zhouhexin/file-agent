"""ChangeSet 生成与查询服务。"""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import AgentRun, ChangeSet, Document
from app.modules.changesets.repository import ChangeSetRepository


def persist_changeset_from_document_results(
    *,
    db: Session,
    run: AgentRun,
    document_results: list[dict[str, Any]],
) -> ChangeSet | None:
    """把 AgentRun 的逐文件结果转换为真实 ChangeSet。

    当前阶段只覆盖解析正文、写入页面、生成分类建议和文件处理失败四类结果。
    """

    if not document_results:
        return None

    start = time.perf_counter()
    valid_document_results = _filter_existing_document_results(db=db, run=run, document_results=document_results)
    if not valid_document_results:
        log_event(
            "changeset.skipped",
            level="WARNING",
            agent_run_id=run.id,
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            status="SKIPPED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_code="NO_EXISTING_DOCUMENT_RESULTS",
            message="ChangeSet 跳过：document_results 中没有存在的 document_id",
        )
        return None

    repository = ChangeSetRepository(db)
    item_count = _count_change_items(valid_document_results)
    status = _changeset_status(valid_document_results=valid_document_results, item_count=item_count)
    try:
        changeset = repository.create_or_reset(
            run=run,
            workspace_id=_workspace_id_from_results(valid_document_results),
            summary=f"已处理 {len(valid_document_results)} 个文件，生成 {item_count} 项变更记录。",
            status=status,
        )
        for result in valid_document_results:
            _append_items_for_result(repository=repository, changeset_id=changeset.id, result=result)
    except Exception as exc:
        log_event(
            "changeset.failed",
            level="ERROR",
            agent_run_id=run.id,
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            status="FAILED",
            duration_ms=int((time.perf_counter() - start) * 1000),
            error_code=exc.__class__.__name__,
            message=str(exc),
        )
        raise
    log_event(
        "changeset.created",
        agent_run_id=run.id,
        user_id=run.user_id,
        conversation_id=run.conversation_id,
        status=status,
        duration_ms=int((time.perf_counter() - start) * 1000),
        message="ChangeSet 创建完成",
        changeset_id=changeset.id,
        item_count=item_count,
    )
    return changeset


def _filter_existing_document_results(
    *,
    db: Session,
    run: AgentRun,
    document_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """过滤不存在的 document_id，避免 ChangeItem 写入悬空外键。"""

    document_ids = [str(result.get("document_id") or "") for result in document_results]
    document_ids = [document_id for document_id in document_ids if document_id]
    existing_document_ids = (
        {
            row[0]
            for row in db.query(Document.id)
            .filter(Document.id.in_(document_ids))
            .all()
        }
        if document_ids
        else set()
    )
    valid_results: list[dict[str, Any]] = []
    for result in document_results:
        document_id = str(result.get("document_id") or "")
        if document_id in existing_document_ids:
            valid_results.append(result)
            continue
        if (
            result.get("source_kind") == "managed_file"
            and result.get("extraction_status") == "FAILED"
            and result.get("managed_file_id")
        ):
            valid_results.append(result)
            continue
        log_event(
            "changeset.document_result_skipped",
            level="WARNING",
            agent_run_id=run.id,
            user_id=run.user_id,
            conversation_id=run.conversation_id,
            document_id=document_id,
            status="SKIPPED",
            error_code="DOCUMENT_NOT_FOUND",
            message="跳过不存在的 document_id，未生成 ChangeItem",
        )
    return valid_results


def _append_items_for_result(
    *,
    repository: ChangeSetRepository,
    changeset_id: str,
    result: dict[str, Any],
) -> None:
    """按单个文件结果生成 ChangeItem。"""

    document_id = str(result.get("document_id") or "")
    target_document_id = document_id or None
    is_managed_file = result.get("source_kind") == "managed_file"
    result_source = "managed-file-read-document" if is_managed_file else "extract-document-text"
    managed_file_id = str(result.get("managed_file_id") or "") or None
    snapshot_status = str(result.get("snapshot_status") or "")
    if is_managed_file and snapshot_status in {"CREATED", "REUSED"}:
        repository.create_item(
            changeset_id=changeset_id,
            target_type="managed_file_snapshot",
            target_id=str(result.get("snapshot_id") or managed_file_id or "") or None,
            target_document_id=target_document_id,
            change_type=(
                "MANAGED_FILE_SNAPSHOT_REUSED"
                if snapshot_status == "REUSED"
                else "MANAGED_FILE_SNAPSHOT_CREATED"
            ),
            after_value={
                "filename": result.get("filename") or "",
                "root_key": result.get("root_key") or "",
                "relative_path": result.get("relative_path") or "",
                "source_sha256": result.get("source_sha256") or "",
            },
            source=result_source,
        )

    if result.get("extraction_status") == "FAILED":
        repository.create_item(
            changeset_id=changeset_id,
            target_type="document" if target_document_id else "managed_file",
            target_id=managed_file_id,
            target_document_id=target_document_id,
            change_type="DOCUMENT_PROCESSING_FAILED",
            after_value={
                "filename": result.get("filename") or "",
                "errors": result.get("errors") or [],
                "root_key": result.get("root_key") or "",
                "relative_path": result.get("relative_path") or "",
            },
            source=result_source,
            evidence={"warnings": result.get("warnings") or []},
            execution_status="FAILED",
        )
        return

    if not target_document_id:
        return

    text_change_type = "TEXT_REUSED" if result.get("text_reused") else "TEXT_EXTRACTED"
    pages_change_type = "DOCUMENT_PAGES_REUSED" if result.get("text_reused") else "DOCUMENT_PAGES_CREATED"
    category_change_type = "CATEGORY_SUGGESTION_REUSED" if result.get("classification_reused") else "CATEGORY_SUGGESTED"

    if int(result.get("char_count") or 0) > 0:
        repository.create_item(
            changeset_id=changeset_id,
            target_type="document",
            target_document_id=document_id,
            change_type=text_change_type,
            after_value={
                "filename": result.get("filename") or "",
                "char_count": int(result.get("char_count") or 0),
                "extractor": result.get("extractor") or "",
            },
            source=result_source,
        )

    if int(result.get("page_count") or 0) > 0:
        repository.create_item(
            changeset_id=changeset_id,
            target_type="document_pages",
            target_document_id=document_id,
            change_type=pages_change_type,
            after_value={
                "filename": result.get("filename") or "",
                "page_count": int(result.get("page_count") or 0),
            },
            source=result_source,
        )

    for category in [item for item in result.get("categories", []) if isinstance(item, dict)]:
        repository.create_item(
            changeset_id=changeset_id,
            target_type="document",
            target_document_id=document_id,
            change_type=category_change_type,
            after_value={
                "category_name": str(category.get("name") or "其他"),
                "category_path": list(category.get("category_path") or []),
                "confidence": float(category.get("confidence") or 0),
                "status": str(category.get("status") or "SUGGESTED"),
            },
            source=str(category.get("source") or "rule"),
            confidence=float(category.get("confidence") or 0),
            evidence={
                "evidence": list(category.get("evidence") or []),
                "evidence_items": list(category.get("evidence_items") or []),
                "taxonomy_key": str(category.get("taxonomy_key") or ""),
                "taxonomy_version": str(category.get("taxonomy_version") or ""),
            },
        )


def _count_change_items(document_results: list[dict[str, Any]]) -> int:
    """统计本次 ChangeSet 会生成的明细数量。"""

    total = 0
    for result in document_results:
        if result.get("source_kind") == "managed_file" and result.get("snapshot_status") in {"CREATED", "REUSED"}:
            total += 1
        if result.get("extraction_status") == "FAILED":
            total += 1
            continue
        if int(result.get("char_count") or 0) > 0:
            total += 1
        if int(result.get("page_count") or 0) > 0:
            total += 1
        total += len([item for item in result.get("categories", []) if isinstance(item, dict)])
    return total


def _changeset_status(*, valid_document_results: list[dict[str, Any]], item_count: int) -> str:
    """根据逐文件成功/失败组合确定 ChangeSet 状态。"""

    if item_count == 0:
        return "FAILED"
    failed_count = len(
        [result for result in valid_document_results if result.get("extraction_status") == "FAILED"]
    )
    if failed_count == len(valid_document_results):
        return "FAILED"
    if failed_count > 0:
        return "PARTIAL"
    return "COMPLETED"


def _workspace_id_from_results(document_results: list[dict[str, Any]]) -> str | None:
    """从文件结果中提取 workspace_id；当前 document_results 未带该字段时允许为空。"""

    for result in document_results:
        value = result.get("workspace_id")
        if value:
            return str(value)
    return None
