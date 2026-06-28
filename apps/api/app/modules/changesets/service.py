"""ChangeSet 生成与查询服务。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentRun, ChangeSet
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

    repository = ChangeSetRepository(db)
    item_count = _count_change_items(document_results)
    status = "FAILED" if item_count == 0 else "COMPLETED"
    changeset = repository.create_or_reset(
        run=run,
        workspace_id=_workspace_id_from_results(document_results),
        summary=f"已处理 {len(document_results)} 个文件，生成 {item_count} 项变更记录。",
        status=status,
    )
    for result in document_results:
        _append_items_for_result(repository=repository, changeset_id=changeset.id, result=result)
    return changeset


def _append_items_for_result(
    *,
    repository: ChangeSetRepository,
    changeset_id: str,
    result: dict[str, Any],
) -> None:
    """按单个文件结果生成 ChangeItem。"""

    document_id = str(result.get("document_id") or "")
    if not document_id:
        return
    if result.get("extraction_status") == "FAILED":
        repository.create_item(
            changeset_id=changeset_id,
            target_type="document",
            target_document_id=document_id,
            change_type="DOCUMENT_PROCESSING_FAILED",
            after_value={
                "filename": result.get("filename") or "",
                "errors": result.get("errors") or [],
            },
            source="extract-document-text",
            evidence={"warnings": result.get("warnings") or []},
            execution_status="FAILED",
        )
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
            source="extract-document-text",
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
            source="extract-document-text",
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
                "taxonomy_key": str(category.get("taxonomy_key") or ""),
                "taxonomy_version": str(category.get("taxonomy_version") or ""),
            },
        )


def _count_change_items(document_results: list[dict[str, Any]]) -> int:
    """统计本次 ChangeSet 会生成的明细数量。"""

    total = 0
    for result in document_results:
        if result.get("extraction_status") == "FAILED":
            total += 1
            continue
        if int(result.get("char_count") or 0) > 0:
            total += 1
        if int(result.get("page_count") or 0) > 0:
            total += 1
        total += len([item for item in result.get("categories", []) if isinstance(item, dict)])
    return total


def _workspace_id_from_results(document_results: list[dict[str, Any]]) -> str | None:
    """从文件结果中提取 workspace_id；当前 document_results 未带该字段时允许为空。"""

    for result in document_results:
        value = result.get("workspace_id")
        if value:
            return str(value)
    return None
