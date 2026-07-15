"""知识图谱投影服务测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import Document, DocumentCategoryFeedback, DocumentCategorySuggestion
from app.modules.classification.loader import load_default_taxonomy
from app.modules.knowledge_graph.projection_service import GraphProjectionService


class RecordingGraphRepository:
    """只记录写入参数的测试仓库。"""

    def __init__(self) -> None:
        self.categories = []
        self.category_relations = []
        self.roots = []
        self.folders = []
        self.folder_relations = []
        self.folder_category_relations = []
        self.versions = []
        self.confirmed_relations = []
        self.locations = []

    def upsert_categories(self, *, categories, relations):
        """记录分类节点和父子关系。"""

        self.categories.extend(categories)
        self.category_relations.extend(relations)

    def upsert_managed_hierarchy(self, *, roots, folders, relations, folder_category_relations):
        """记录受管目录层级和目录分类映射。"""

        self.roots.extend(roots)
        self.folders.extend(folders)
        self.folder_relations.extend(relations)
        self.folder_category_relations.extend(folder_category_relations)

    def upsert_confirmed_classifications(self, *, versions, relations, locations):
        """记录可信分类和目录归属。"""

        self.versions.extend(versions)
        self.confirmed_relations.extend(relations)
        self.locations.extend(locations)


def test_taxonomy_projection_preserves_stable_ids_and_parent_relations():
    """taxonomy 投影必须使用稳定分类 ID，并保留完整父子关系。"""

    repository = RecordingGraphRepository()
    summary = GraphProjectionService(repository=repository).sync_taxonomy(load_default_taxonomy())

    category_by_id = {item.category_id: item for item in repository.categories}
    assert category_by_id["school.hr.title-review"].graph_key == (
        "school_file_classification:2026-06-v2:school.hr.title-review"
    )
    assert any(
        relation.parent_graph_key.endswith(":school.hr")
        and relation.child_graph_key.endswith(":school.hr.title-review")
        for relation in repository.category_relations
    )
    assert summary.category_count == len(repository.categories)
    assert summary.relation_count == len(repository.category_relations)


def test_managed_path_projection_builds_all_parent_folders():
    """只提供叶子分类路径时，也必须补齐多级目录和父子关系。"""

    repository = RecordingGraphRepository()
    rows = [
        ("classified_root", "已分类目录", "党办/2026/科学发展观", 3),
        ("classified_root", "已分类目录", "党办/2026/会议", 2),
    ]

    summary = GraphProjectionService(repository=repository).sync_managed_paths(rows)

    folder_paths = {item.relative_path for item in repository.folders}
    assert folder_paths == {"党办", "党办/2026", "党办/2026/科学发展观", "党办/2026/会议"}
    assert any(
        relation.parent_graph_key is not None
        and relation.parent_graph_key.endswith(":党办/2026")
        and relation.child_graph_key.endswith(":党办/2026/科学发展观")
        for relation in repository.folder_relations
    )
    assert len(repository.folder_category_relations) == len(repository.folders)
    assert summary.folder_count == 4


def test_only_explicitly_confirmed_suggestion_becomes_trusted_graph_relation():
    """普通建议不得自强化，只有明确确认反馈才能写入可信图谱关系。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()
    try:
        document = Document(
            id="document-confirmed",
            user_id="user-confirmed",
            original_filename="职称材料.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=100,
            sha256="a" * 64,
        )
        suggestion = DocumentCategorySuggestion(
            id="suggestion-confirmed",
            classification_run_id="classification-run",
            document_id=document.id,
            category_name="学校/人事师资/职称",
            category_path_json=["学校", "人事师资", "职称"],
            taxonomy_key="school_file_classification",
            taxonomy_version="2026-06-v2",
            confidence=0.8,
            status="SUGGESTED",
            evidence_json=[],
            source="rule",
            rank=1,
        )
        db.add_all([document, suggestion])
        db.flush()

        repository = RecordingGraphRepository()
        service = GraphProjectionService(repository=repository)
        service.sync_trusted_classifications(db=db)
        assert repository.confirmed_relations == []

        db.add(
            DocumentCategoryFeedback(
                id="feedback-confirmed",
                suggestion_id=suggestion.id,
                document_id=document.id,
                user_id="user-confirmed",
                action="CONFIRMED",
            )
        )
        db.flush()

        service.sync_trusted_classifications(db=db)

        assert repository.confirmed_relations[-1].document_version_id == document.id
        assert repository.confirmed_relations[-1].category_graph_key.endswith(":school.hr.title-review")
        assert repository.confirmed_relations[-1].source_type == "user_feedback"
    finally:
        db.close()
