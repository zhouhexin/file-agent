"""分类建议持久化服务。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.modules.classification.repository import ClassificationRepository


def persist_document_results_classifications(
    *,
    db: Session,
    agent_run_id: str,
    document_results: list[dict[str, Any]],
) -> None:
    """把 AgentRun 的 document_results 分类建议落库。

    这里保存的是 SUGGESTED 分类建议，不写正式 document_categories。
    """

    repository = ClassificationRepository(db)
    repository.delete_by_agent_run(agent_run_id)
    for result in document_results:
        document_id = str(result.get("document_id") or "")
        if not document_id:
            continue
        categories = [item for item in result.get("categories", []) if isinstance(item, dict)]
        status = "FAILED" if result.get("extraction_status") == "FAILED" else "COMPLETED"
        error_message = _first_error_message(result)
        taxonomy_key = _first_category_value(categories, "taxonomy_key")
        taxonomy_version = _first_category_value(categories, "taxonomy_version")
        classification_run = repository.create_run(
            agent_run_id=agent_run_id,
            document_id=document_id,
            taxonomy_key=taxonomy_key,
            taxonomy_version=taxonomy_version,
            status=status,
            error_message=error_message,
        )
        for rank, category in enumerate(categories, start=1):
            repository.create_suggestion(
                classification_run_id=classification_run.id,
                document_id=document_id,
                category=category,
                rank=rank,
            )


def _first_category_value(categories: list[dict[str, Any]], key: str) -> str:
    """从分类建议中提取 taxonomy 元数据。"""

    for category in categories:
        value = category.get(key)
        if value:
            return str(value)
    return ""


def _first_error_message(result: dict[str, Any]) -> str | None:
    """从文件级结果中取第一条错误信息。"""

    errors = result.get("errors") or []
    if not errors:
        return None
    first_error = errors[0]
    if isinstance(first_error, dict):
        return str(first_error.get("message") or "")
    return str(first_error)
