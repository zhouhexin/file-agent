"""图谱投影运行的 PostgreSQL 审计仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import GraphProjectionRun, utcnow


class GraphProjectionRunRepository:
    """创建并推进可查询的图谱投影运行。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        projection_type: str,
        scope_type: str = "ALL",
        scope_id: str | None = None,
        projection_version: str = "graph-v2",
    ) -> GraphProjectionRun:
        """创建 RUNNING 记录。"""

        run = GraphProjectionRun(
            projection_type=projection_type,
            scope_type=scope_type,
            scope_id=scope_id,
            projection_version=projection_version,
            status="RUNNING",
        )
        self.db.add(run)
        self.db.flush()
        return run

    def complete(
        self,
        run: GraphProjectionRun,
        *,
        nodes_written: int,
        relationships_written: int,
        items_succeeded: int,
        items_failed: int = 0,
    ) -> GraphProjectionRun:
        """记录成功数量并结束运行。"""

        run.status = "COMPLETED" if items_failed == 0 else "PARTIAL"
        run.nodes_written = nodes_written
        run.relationships_written = relationships_written
        run.items_succeeded = items_succeeded
        run.items_failed = items_failed
        run.finished_at = utcnow()
        self.db.flush()
        return run

    def fail(self, run: GraphProjectionRun, *, error: Exception) -> GraphProjectionRun:
        """记录结构化错误，不保存正文或连接凭据。"""

        run.status = "FAILED"
        run.items_failed = max(1, run.items_failed)
        run.error_code = error.__class__.__name__
        run.error_message = str(error)[:2000]
        run.finished_at = utcnow()
        self.db.flush()
        return run
