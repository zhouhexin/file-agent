"""正式分类到 Neo4j 的事务 Outbox 消费服务。

PostgreSQL 始终是权威事实源。图数据库不可用时，本服务只记录有限错误并延后重试，
不会回滚已经提交的分类决定，也不会阻塞聊天和文件操作。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.db.models import (
    ClassificationGraphOutbox,
    DocumentCategory,
    DocumentVersion,
    WorkingCopy,
    utcnow,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.classification.schemas import CategoryNode
from app.modules.knowledge_graph.classification_context import get_graph_repository
from app.modules.knowledge_graph.projection_runs import GraphProjectionRunRepository
from app.modules.knowledge_graph.schemas import (
    CategoryProjection,
    ConfirmedClassificationProjection,
    DocumentVersionProjection,
    category_graph_key,
)


class ClassificationGraphOutboxService:
    """领取、校验并投影一条正式分类待办。"""

    def __init__(
        self,
        db: Session,
        *,
        settings: Any,
        repository: Any | None = None,
    ) -> None:
        """保存请求级会话；测试可注入确定性图谱仓库。"""

        self.db = db
        self.settings = settings
        self.repository = repository

    def process_next(self) -> str | None:
        """处理一个可用待办；图谱关闭时保留 PENDING 并立即返回。"""

        if not bool(getattr(self.settings, "graph_projection_worker_enabled", False)):
            return None
        if not bool(getattr(self.settings, "neo4j_sync_enabled", False)):
            return None
        now = utcnow()
        query = (
            self.db.query(ClassificationGraphOutbox)
            .filter(
                ClassificationGraphOutbox.status.in_({"PENDING", "RETRY"}),
                ClassificationGraphOutbox.available_at <= now,
                ClassificationGraphOutbox.attempt_count
                < ClassificationGraphOutbox.max_attempts,
            )
            .order_by(ClassificationGraphOutbox.created_at.asc())
        )
        # PostgreSQL 多 worker 使用 SKIP LOCKED；SQLite 测试不支持该语法。
        if self.db.bind is not None and self.db.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        outbox = query.first()
        if outbox is None:
            return None
        outbox.status = "RUNNING"
        outbox.attempt_count += 1
        outbox.updated_at = now
        self.db.flush()
        self._project(outbox)
        return outbox.id

    def _project(self, outbox: ClassificationGraphOutbox) -> None:
        """比较最新状态后写 Neo4j，并记录独立投影运行。"""

        run_repository = GraphProjectionRunRepository(self.db)
        run = run_repository.create(
            projection_type="PROJECT_CONFIRMED_CLASSIFICATION",
            scope_type="DOCUMENT_CATEGORY",
            scope_id=outbox.document_category_id,
            projection_version="stage6-v1",
        )
        try:
            newer_exists = (
                self.db.query(ClassificationGraphOutbox.id)
                .filter(
                    ClassificationGraphOutbox.document_category_id
                    == outbox.document_category_id,
                    ClassificationGraphOutbox.state_version > outbox.state_version,
                )
                .first()
                is not None
            )
            relation = self.db.get(DocumentCategory, outbox.document_category_id)
            if newer_exists or relation is None:
                outbox.status = "SUPERSEDED"
                outbox.finished_at = utcnow()
                run_repository.complete(
                    run,
                    nodes_written=0,
                    relationships_written=0,
                    items_succeeded=1,
                )
                return
            if (
                relation.working_copy_id != outbox.working_copy_id
                or relation.document_version_id != outbox.document_version_id
                or relation.status != outbox.expected_status
            ):
                outbox.status = "SUPERSEDED"
                outbox.finished_at = utcnow()
                run_repository.complete(
                    run,
                    nodes_written=0,
                    relationships_written=0,
                    items_succeeded=1,
                )
                return

            repository = self.repository or get_graph_repository(self.settings)
            category = _taxonomy_category(relation.category_id)
            graph_key = category_graph_key(
                taxonomy_key=relation.taxonomy_key,
                taxonomy_version=relation.taxonomy_version,
                category_id=relation.category_id,
            )
            if relation.status == "CONFIRMED":
                working_copy = self.db.get(WorkingCopy, relation.working_copy_id)
                version = self.db.get(DocumentVersion, relation.document_version_id)
                if working_copy is None or version is None:
                    raise RuntimeError("正式分类缺少当前工作副本或文档版本")
                repository.upsert_categories(
                    categories=[
                        CategoryProjection(
                            graph_key=graph_key,
                            category_id=relation.category_id,
                            taxonomy_key=relation.taxonomy_key,
                            taxonomy_version=relation.taxonomy_version,
                            name=category.name,
                            path=list(relation.category_path_json or []),
                            description=category.description,
                            aliases=list(category.aliases),
                        )
                    ],
                    relations=[],
                )
                repository.upsert_confirmed_classifications(
                    versions=[
                        DocumentVersionProjection(
                            document_version_id=version.id,
                            document_id=working_copy.document_id,
                            sha256=version.sha256,
                            filename=working_copy.filename,
                            is_active=working_copy.status == "ACTIVE",
                        )
                    ],
                    relations=[
                        ConfirmedClassificationProjection(
                            document_version_id=version.id,
                            category_graph_key=graph_key,
                            source_type="formal_classification",
                            source_id=relation.id,
                            confidence=1.0,
                        )
                    ],
                    locations=[],
                )
                nodes_written = 2
                relationships_written = 1
            else:
                repository.delete_confirmed_classification(
                    document_version_id=relation.document_version_id,
                    category_graph_key=graph_key,
                    source_id=relation.id,
                )
                nodes_written = 0
                relationships_written = 1
            outbox.status = "COMPLETED"
            outbox.error_code = None
            outbox.error_message = None
            outbox.finished_at = utcnow()
            run_repository.complete(
                run,
                nodes_written=nodes_written,
                relationships_written=relationships_written,
                items_succeeded=1,
            )
            log_event(
                "classification.graph_outbox.completed",
                document_id=relation.document_id,
                status="COMPLETED",
                message="正式分类图谱投影完成",
                outbox_id=outbox.id,
            )
        except Exception as exc:
            run_repository.fail(run, error=exc)
            outbox.error_code = exc.__class__.__name__
            # 数据库只保存可公开的有限摘要；连接信息与堆栈只进入服务端 JSONL 日志。
            outbox.error_message = "Neo4j 分类投影暂不可用，系统将按策略重试。"
            if outbox.attempt_count >= outbox.max_attempts:
                outbox.status = "FAILED"
                outbox.finished_at = utcnow()
            else:
                outbox.status = "RETRY"
                delay_seconds = min(300, 2 ** max(1, outbox.attempt_count))
                outbox.available_at = utcnow() + timedelta(seconds=delay_seconds)
            log_event(
                "classification.graph_outbox.failed",
                level="WARNING",
                status=outbox.status,
                error_code=exc.__class__.__name__,
                message="正式分类图谱投影暂不可用，已保留待办重试",
                outbox_id=outbox.id,
            )


def _taxonomy_category(category_id: str) -> CategoryNode:
    """从当前 ACTIVE taxonomy 读取稳定分类节点。"""

    def walk(node: CategoryNode) -> CategoryNode | None:
        if node.id == category_id:
            return node
        for child in node.children:
            found = walk(child)
            if found is not None:
                return found
        return None

    for root in load_default_taxonomy().categories:
        found = walk(root)
        if found is not None:
            return found
    raise RuntimeError("正式分类已不在当前 taxonomy 中")
