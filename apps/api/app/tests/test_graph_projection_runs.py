"""图谱投影运行持久化测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import GraphProjectionRun
from app.modules.knowledge_graph.projection_service import GraphProjectionService
from app.tests.test_graph_projection_service import RecordingGraphRepository


def test_full_projection_persists_completed_run():
    """成功投影必须留下可查询的完成记录和数量。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        summary = GraphProjectionService(repository=RecordingGraphRepository()).sync_all(db=db)
        run = db.query(GraphProjectionRun).one()

        assert run.status == "COMPLETED"
        assert run.projection_type == "FULL"
        assert run.nodes_written >= summary.category_count
        assert run.finished_at is not None
    finally:
        db.close()
