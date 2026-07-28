"""阶段六正式分类图谱 Outbox 回归测试。"""

from types import SimpleNamespace

import pytest

from app.db.models import (
    ClassificationGraphOutbox,
    DocumentCategory,
    GraphProjectionRun,
)
from app.modules.classification.feedback_schemas import ClassificationFeedbackRequest
from app.modules.classification.feedback_service import ClassificationFeedbackService
from app.modules.classification.graph_outbox import ClassificationGraphOutboxService
from app.tests.test_classification_feedback import _feedback_session, _seed_suggestion


class RecordingFormalGraphRepository:
    """记录单条正式分类投影，不连接真实 Neo4j。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.categories = []
        self.versions = []
        self.relations = []
        self.deleted = []

    def upsert_categories(self, *, categories, relations):
        """记录 taxonomy 节点。"""

        if self.fail:
            raise ConnectionError("neo4j unavailable")
        self.categories.extend(categories)

    def upsert_confirmed_classifications(self, *, versions, relations, locations):
        """记录正式分类关系。"""

        self.versions.extend(versions)
        self.relations.extend(relations)

    def delete_confirmed_classification(
        self, *, document_version_id, category_graph_key, source_id
    ):
        """记录已结束关系删除。"""

        self.deleted.append((document_version_id, category_graph_key, source_id))


@pytest.fixture(autouse=True)
def _disable_file_logging(monkeypatch):
    """Outbox 单元测试不依赖项目级 DATABASE_URL 和文件日志目录。"""

    monkeypatch.setattr(
        "app.modules.classification.graph_outbox.log_event",
        lambda *_args, **_kwargs: None,
    )


def _settings():
    """构造启用图谱 Outbox 的最小配置。"""

    return SimpleNamespace(
        graph_projection_worker_enabled=True,
        neo4j_sync_enabled=True,
    )


def test_graph_outbox_projects_formal_relation_idempotently():
    """Outbox 必须使用正式关系 ID 投影当前 DocumentVersion。"""

    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        ClassificationFeedbackService(db).record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                agent_run_id="11111111-1111-4111-8111-111111111111",
            ),
            current_user=user,
        )
        repository = RecordingFormalGraphRepository()
        service = ClassificationGraphOutboxService(
            db,
            settings=_settings(),
            repository=repository,
        )

        outbox_id = service.process_next()
        db.flush()

        relation = db.query(DocumentCategory).one()
        outbox = db.get(ClassificationGraphOutbox, outbox_id)
        assert outbox.status == "COMPLETED"
        assert repository.relations[0].source_id == relation.id
        assert repository.relations[0].source_type == "formal_classification"
        assert repository.versions[0].document_version_id == relation.document_version_id
        assert db.query(GraphProjectionRun).one().status == "COMPLETED"
        assert service.process_next() is None
    finally:
        db.close()


def test_graph_failure_keeps_postgresql_fact_and_retries_outbox():
    """Neo4j 不可用不能撤销 PostgreSQL 正式分类。"""

    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        ClassificationFeedbackService(db).record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                agent_run_id="11111111-1111-4111-8111-111111111111",
            ),
            current_user=user,
        )
        service = ClassificationGraphOutboxService(
            db,
            settings=_settings(),
            repository=RecordingFormalGraphRepository(fail=True),
        )

        outbox_id = service.process_next()
        db.flush()

        outbox = db.get(ClassificationGraphOutbox, outbox_id)
        assert outbox.status == "RETRY"
        assert outbox.error_code == "ConnectionError"
        assert db.query(DocumentCategory).one().status == "CONFIRMED"
        assert db.query(GraphProjectionRun).one().status == "FAILED"
    finally:
        db.close()
