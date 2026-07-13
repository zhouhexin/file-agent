"""ChangeSet 持久化仓库。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AgentRun, ChangeItem, ChangeSet, ToolInvocation, utcnow


class ChangeSetRepository:
    """封装 ChangeSet 和 ChangeItem 的数据库读写。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def get_by_agent_run(self, agent_run_id: str) -> ChangeSet | None:
        """查询某次 AgentRun 已有关联的 ChangeSet。"""

        return self.db.query(ChangeSet).filter(ChangeSet.agent_run_id == agent_run_id).one_or_none()

    def create_or_reset(
        self,
        *,
        run: AgentRun,
        workspace_id: str | None,
        summary: str,
        status: str,
    ) -> ChangeSet:
        """创建或重置 ChangeSet，保证重复持久化同一 AgentRun 时不产生重复明细。"""

        changeset = self.get_by_agent_run(run.id)
        if changeset is None:
            changeset = ChangeSet(
                workspace_id=workspace_id,
                conversation_id=run.conversation_id,
                agent_run_id=run.id,
                user_id=run.user_id,
            )
            self.db.add(changeset)
            self.db.flush()
        changeset.workspace_id = workspace_id
        changeset.summary = summary
        changeset.status = status
        changeset.updated_at = utcnow()
        self.db.query(ChangeItem).filter(ChangeItem.changeset_id == changeset.id).delete(synchronize_session=False)
        run.changeset_id = changeset.id
        (
            self.db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == run.id)
            .update({ToolInvocation.changeset_id: changeset.id}, synchronize_session=False)
        )
        self.db.flush()
        return changeset

    def create_item(
        self,
        *,
        changeset_id: str,
        target_type: str,
        target_document_id: str | None,
        change_type: str,
        after_value: dict[str, Any],
        source: str,
        before_value: dict[str, Any] | None = None,
        confidence: float = 0,
        evidence: dict[str, Any] | None = None,
        execution_status: str = "COMPLETED",
        target_id: str | None = None,
    ) -> ChangeItem:
        """写入一条文件级 ChangeItem。"""

        item = ChangeItem(
            changeset_id=changeset_id,
            target_type=target_type,
            target_id=target_id,
            target_document_id=target_document_id,
            change_type=change_type,
            before_value_json=before_value or {},
            after_value_json=after_value,
            source=source,
            confidence=confidence,
            evidence_json=evidence or {},
            execution_status=execution_status,
        )
        self.db.add(item)
        self.db.flush()
        return item

    def get_owned_changeset(self, *, changeset_id: str, user_id: str) -> ChangeSet | None:
        """按用户边界查询 ChangeSet，避免跨用户读取审计结果。"""

        return (
            self.db.query(ChangeSet)
            .filter(ChangeSet.id == changeset_id, ChangeSet.user_id == user_id)
            .one_or_none()
        )

    def list_items(self, changeset_id: str) -> list[ChangeItem]:
        """按创建顺序列出 ChangeItem 明细。"""

        return (
            self.db.query(ChangeItem)
            .filter(ChangeItem.changeset_id == changeset_id)
            .order_by(ChangeItem.created_at.asc())
            .all()
        )
