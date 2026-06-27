"""分类建议持久化仓库。

本仓库只保存分类建议和反馈，不负责正式 document_categories 关系。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import DocumentCategorySuggestion, DocumentClassificationRun


class ClassificationRepository:
    """封装分类运行和分类建议的数据库写入。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def delete_by_agent_run(self, agent_run_id: str) -> None:
        """删除某次 AgentRun 已有分类建议，保证重复写入时幂等。"""

        runs = (
            self.db.query(DocumentClassificationRun)
            .filter(DocumentClassificationRun.agent_run_id == agent_run_id)
            .all()
        )
        run_ids = [run.id for run in runs]
        if run_ids:
            (
                self.db.query(DocumentCategorySuggestion)
                .filter(DocumentCategorySuggestion.classification_run_id.in_(run_ids))
                .delete(synchronize_session=False)
            )
        for run in runs:
            self.db.delete(run)
        self.db.flush()

    def create_run(
        self,
        *,
        agent_run_id: str,
        document_id: str,
        taxonomy_key: str,
        taxonomy_version: str,
        status: str,
        error_message: str | None = None,
    ) -> DocumentClassificationRun:
        """创建一个文件在本次 AgentRun 中的分类运行记录。"""

        run = DocumentClassificationRun(
            agent_run_id=agent_run_id,
            document_id=document_id,
            taxonomy_key=taxonomy_key,
            taxonomy_version=taxonomy_version,
            classifier_version="taxonomy-rule-v1",
            source="rule",
            status=status,
            error_message=error_message,
        )
        self.db.add(run)
        self.db.flush()
        return run

    def create_suggestion(
        self,
        *,
        classification_run_id: str,
        document_id: str,
        category: dict[str, Any],
        rank: int,
    ) -> DocumentCategorySuggestion:
        """创建一条 SUGGESTED 分类建议。"""

        suggestion = DocumentCategorySuggestion(
            classification_run_id=classification_run_id,
            document_id=document_id,
            category_name=str(category.get("name") or "其他"),
            category_path_json=list(category.get("category_path") or [category.get("name") or "其他"]),
            taxonomy_key=str(category.get("taxonomy_key") or ""),
            taxonomy_version=str(category.get("taxonomy_version") or ""),
            confidence=float(category.get("confidence") or 0),
            status=str(category.get("status") or "SUGGESTED"),
            evidence_json=list(category.get("evidence") or []),
            source="rule",
            rank=rank,
        )
        self.db.add(suggestion)
        self.db.flush()
        return suggestion
