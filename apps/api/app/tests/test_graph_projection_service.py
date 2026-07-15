"""知识图谱投影服务测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    Document,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    ManagedFile,
    ManagedFileSnapshot,
    ManagedRoot,
)
from app.modules.classification.loader import load_default_taxonomy
from app.modules.knowledge_graph.managed_path_profile import (
    ManagedPathProfileRegistry,
    ManagedPathRule,
    ManagedRootClassificationProfile,
)
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
        self.suggested_relations = []
        self.locations = []
        self.weak_path_suggestions = []
        self.deleted_confirmed_sources = []

    def ensure_schema(self):
        """测试仓库无需创建真实 Neo4j schema。"""

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

    def replace_weak_path_suggestions(self, *, relations):
        """记录目录弱分类关系。"""

        self.weak_path_suggestions = list(relations)

    def delete_confirmed_classifications_by_source(self, *, source_type):
        """记录重建前清理的可信关系来源。"""

        self.deleted_confirmed_sources.append(source_type)

    def replace_suggested_classifications(self, *, relations):
        """记录正文分类建议关系。"""

        self.suggested_relations = list(relations)


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

    registry = ManagedPathProfileRegistry(
        profiles={
            "classified_root": ManagedRootClassificationProfile(
                root_key="classified_root",
                version="v1",
                rules=(
                    ManagedPathRule(
                        path_prefix="党办/2026/科学发展观",
                        role="CATEGORY",
                        category_path=("党办", "科学发展观"),
                    ),
                    ManagedPathRule(
                        path_prefix="党办/2026/会议",
                        role="CATEGORY",
                        category_path=("党办", "会议"),
                    ),
                ),
            )
        }
    )
    summary = GraphProjectionService(
        repository=repository,
        profile_registry=registry,
    ).sync_managed_paths(rows)

    folder_paths = {item.relative_path for item in repository.folders}
    assert folder_paths == {"党办", "党办/2026", "党办/2026/科学发展观", "党办/2026/会议"}
    assert any(
        relation.parent_graph_key is not None
        and relation.parent_graph_key.endswith(":党办/2026")
        and relation.child_graph_key.endswith(":党办/2026/科学发展观")
        for relation in repository.folder_relations
    )
    assert len(repository.folder_category_relations) == 2
    assert {
        relation.source_type for relation in repository.folder_category_relations
    } == {"managed_path_category_source"}
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
        assert len(repository.suggested_relations) == 1

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
        assert len(repository.suggested_relations) == 1
    finally:
        db.close()


def test_path_as_category_location_is_weak_signal_not_confirmed_classification(tmp_path):
    """分类来源目录中的文件位置只能产生弱关系，不能自动确认分类。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        root = ManagedRoot(
            id="root-classified",
            root_key="classified_archive",
            display_name="分类档案",
            container_path=str(tmp_path),
            classification_mode="PATH_AS_CATEGORY",
        )
        managed_file = ManagedFile(
            id="managed-file-classified",
            root_id=root.id,
            relative_path="人事处/职称评定/材料.docx",
            category_path="人事处/职称评定",
            filename="材料.docx",
            extension=".docx",
            size_bytes=100,
            fingerprint="f" * 64,
            status="ACTIVE",
        )
        document = Document(
            id="document-managed-classified",
            user_id="user-managed",
            original_filename="材料.docx",
            size_bytes=100,
            sha256="a" * 64,
        )
        snapshot = ManagedFileSnapshot(
            id="snapshot-managed-classified",
            user_id="user-managed",
            managed_file_id=managed_file.id,
            document_id=document.id,
            source_fingerprint=managed_file.fingerprint,
            source_sha256=document.sha256,
            source_size_bytes=100,
            status="ACTIVE",
        )
        db.add_all([root, managed_file, document, snapshot])
        db.flush()

        registry = ManagedPathProfileRegistry(
            profiles={
                root.root_key: ManagedRootClassificationProfile(
                    root_key=root.root_key,
                    version="v1",
                    rules=(
                        ManagedPathRule(
                            path_prefix="人事处/职称评定",
                            role="CATEGORY",
                            category_path=("人事处", "职称评定"),
                        ),
                    ),
                )
            }
        )
        repository = RecordingGraphRepository()
        GraphProjectionService(
            repository=repository,
            profile_registry=registry,
        ).sync_trusted_classifications(db=db)

        assert len(repository.locations) == 1
        assert len(repository.weak_path_suggestions) == 1
        assert repository.confirmed_relations == []
    finally:
        db.close()


def test_corrected_feedback_removes_original_suggestion_relation():
    """用户更正后原建议不能继续作为图谱建议参与后续增强。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        document = Document(
            id="document-corrected",
            user_id="user-corrected",
            original_filename="材料.docx",
            size_bytes=100,
            sha256="c" * 64,
        )
        suggestion = DocumentCategorySuggestion(
            id="suggestion-corrected",
            classification_run_id="classification-run-corrected",
            document_id=document.id,
            category_id="school.hr.title-review",
            category_name="职称评定",
            category_path_json=["学校", "人事师资", "职称评定"],
            taxonomy_key="school_file_classification",
            taxonomy_version="2026-06-v2",
            confidence=0.8,
            status="SUGGESTED",
            evidence_json=[],
            source="rule",
            rank=1,
        )
        feedback = DocumentCategoryFeedback(
            id="feedback-corrected",
            suggestion_id=suggestion.id,
            document_id=document.id,
            user_id="user-corrected",
            action="CORRECTED",
            corrected_category_id="school.admin.annual-plan-summary",
            corrected_category_path_json=["学校", "行政综合管理", "年度计划总结"],
            is_active=True,
        )
        db.add_all([document, suggestion, feedback])
        db.flush()

        repository = RecordingGraphRepository()
        GraphProjectionService(repository=repository).sync_trusted_classifications(db=db)

        assert repository.suggested_relations == []
        assert len(repository.confirmed_relations) == 1
        assert repository.confirmed_relations[0].category_graph_key.endswith(
            ":school.admin.annual-plan-summary"
        )
    finally:
        db.close()
