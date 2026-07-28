"""无标注冷启动分类反馈测试。"""

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace

from app.db.base import Base
from app.db.models import (
    AgentRun,
    ChangeItem,
    ChangeSet,
    ClassificationGraphOutbox,
    Document,
    DocumentCategory,
    DocumentCategoryConfirmationSource,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentVersion,
    ManagedFile,
    ManagedRoot,
    User,
    WorkingCopy,
    WorkingCopyRoot,
)
from app.modules.classification.feedback_schemas import ClassificationFeedbackRequest
from app.modules.classification.feedback_service import ClassificationFeedbackService
from app.modules.classification.clarification_service import (
    ClassificationClarificationService,
)
from app.modules.classification.conversation_decision import (
    ConversationalClassificationDecisionService,
)
from app.modules.classification.graph_outbox import ClassificationGraphOutboxService
from app.modules.file_lifecycle.shared_access import (
    CanonicalWorkingFileError,
    CanonicalWorkingFileResolver,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id


def _feedback_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_suggestion(db):
    """创建带当前共享工作副本的真实分类建议测试数据。"""

    user = User(id="user-feedback", username="feedback-user")
    document = Document(
        id="document-feedback",
        user_id=user.id,
        original_filename="职称材料.docx",
        size_bytes=100,
        sha256="c" * 64,
    )
    agent_run = AgentRun(
        id="11111111-1111-4111-8111-111111111111",
        conversation_id="conversation-feedback",
        message_id="message-feedback",
        user_id=user.id,
    )
    document_version = DocumentVersion(
        id="version-feedback",
        document_id=document.id,
        version_number=1,
        filename=document.original_filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        storage_path="documents/document-feedback/version-feedback.docx",
    )
    shared_workspace_id = get_shared_workspace_id(db)
    managed_root = ManagedRoot(
        id="managed-root-feedback",
        root_key="feedback-root",
        display_name="反馈测试原件",
        container_path="/managed/feedback",
        enabled=True,
    )
    managed_file = ManagedFile(
        id="managed-file-feedback",
        root_id=managed_root.id,
        relative_path=document.original_filename,
        relative_path_hash="a" * 64,
        filename=document.original_filename,
        extension=".docx",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
        status="ACTIVE",
    )
    working_root = WorkingCopyRoot(
        id="working-root-feedback",
        workspace_id=shared_workspace_id,
        managed_root_id=managed_root.id,
        root_key="feedback-root",
        relative_storage_path="shared/feedback",
        status="ACTIVE",
    )
    working_copy = WorkingCopy(
        id="working-copy-feedback",
        working_copy_root_id=working_root.id,
        workspace_id=shared_workspace_id,
        managed_file_id=managed_file.id,
        document_id=document.id,
        current_version_id=document_version.id,
        relative_path=document.original_filename,
        relative_path_hash="b" * 64,
        filename=document.original_filename,
        extension=".docx",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        is_primary_import=True,
        status="ACTIVE",
        sync_status="SYNCED",
    )
    classification_run = DocumentClassificationRun(
        id="classification-run-feedback",
        document_id=document.id,
        agent_run_id=agent_run.id,
        taxonomy_key="school_file_classification",
        taxonomy_version="2026-06-v2",
        classifier_version="taxonomy-graph-semantic-v2",
    )
    suggestion = DocumentCategorySuggestion(
        id="suggestion-feedback",
        classification_run_id=classification_run.id,
        document_id=document.id,
        document_version_id=document_version.id,
        category_id="school.hr.title-review",
        category_name="学校/人事师资/职称",
        category_path_json=["学校", "人事师资", "职称"],
        taxonomy_key="school_file_classification",
        taxonomy_version="2026-06-v2",
        confidence=0.8,
        rank=1,
    )
    db.add_all(
        [
            user,
            document,
            document_version,
            agent_run,
            managed_root,
            managed_file,
            working_root,
            working_copy,
            classification_run,
            suggestion,
        ]
    )
    db.flush()
    return user, suggestion


def test_correction_supersedes_acceptance_and_creates_positive_and_negative_labels():
    """更正必须停用旧反馈，并同时表达原分类负样本和目标正样本。"""

    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        service = ClassificationFeedbackService(db, evaluation_min_samples=2)
        accepted = service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                relation_role="PRIMARY",
                idempotency_key="accept-feedback",
            ),
            current_user=user,
        )
        original_path = db.get(WorkingCopy, "working-copy-feedback").relative_path
        repeated = service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                relation_role="PRIMARY",
                idempotency_key="accept-feedback",
            ),
            current_user=user,
        )
        corrected = service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="CORRECT",
                corrected_category_id="school.hr.appointment-assessment",
            ),
            current_user=user,
        )

        assert accepted.positive_category_ids == ["school.hr.title-review"]
        assert repeated.id == accepted.id
        assert corrected.positive_category_ids == ["school.hr.appointment-assessment"]
        assert corrected.negative_category_ids == ["school.hr.title-review"]
        rows = db.query(DocumentCategoryFeedback).order_by(DocumentCategoryFeedback.created_at).all()
        assert rows[0].is_active is False
        assert rows[1].supersedes_feedback_id == rows[0].id
        relations = (
            db.query(DocumentCategory)
            .order_by(DocumentCategory.created_at.asc())
            .all()
        )
        assert [(item.category_id, item.status) for item in relations] == [
            ("school.hr.title-review", "ENDED"),
            ("school.hr.appointment-assessment", "CONFIRMED"),
        ]
        assert (
            db.query(DocumentCategoryConfirmationSource)
            .filter(
                DocumentCategoryConfirmationSource.status == "ACTIVE"
            )
            .count()
            == 1
        )
        assert db.query(ChangeSet).count() == 1
        assert {
            item.change_type for item in db.query(ChangeItem).all()
        } == {"CATEGORY_ADDED", "CATEGORY_CORRECTED"}
        assert db.query(ClassificationGraphOutbox).count() == 3
        assert db.get(WorkingCopy, "working-copy-feedback").relative_path == original_path
        relations = db.query(DocumentCategory).order_by(DocumentCategory.created_at).all()
        assert [(item.category_id, item.status) for item in relations] == [
            ("school.hr.title-review", "ENDED"),
            ("school.hr.appointment-assessment", "CONFIRMED"),
        ]
        assert all(item.document_id == "document-feedback" for item in relations)
        assert all(item.document_version_id == "version-feedback" for item in relations)
        assert db.query(DocumentCategoryConfirmationSource).count() == 2
        assert db.query(ChangeItem).filter(
            ChangeItem.change_type == "CATEGORY_CORRECTED"
        ).count() == 1
        outbox_rows = db.query(ClassificationGraphOutbox).order_by(
            ClassificationGraphOutbox.state_version
        ).all()
        assert len(outbox_rows) == 3
        versions_by_relation: dict[str, list[int]] = {}
        for item in outbox_rows:
            versions_by_relation.setdefault(item.document_category_id, []).append(
                item.state_version
            )
        assert sorted(versions_by_relation.values()) == [[1], [1, 2]]
        summary = service.summary(current_user=user)
        assert summary.total == 1
        assert summary.corrected == 1
        assert summary.ready_to_freeze_evaluation_set is False
    finally:
        db.close()


def test_repeated_decision_is_idempotent_and_keeps_single_formal_source():
    """相同 AgentRun 重复提交不能重复创建反馈、来源或 ChangeSet。"""

    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        request = ClassificationFeedbackRequest(
            action="ACCEPT",
            relation_role="PRIMARY",
            agent_run_id="11111111-1111-4111-8111-111111111111",
            idempotency_key="same-click",
        )
        service = ClassificationFeedbackService(db)
        first = service.record(
            suggestion_id=suggestion.id,
            request=request,
            current_user=user,
        )
        second = service.record(
            suggestion_id=suggestion.id,
            request=request,
            current_user=user,
        )

        assert first.id == second.id
        assert db.query(DocumentCategoryFeedback).count() == 1
        assert db.query(DocumentCategory).count() == 1
        assert db.query(DocumentCategoryConfirmationSource).count() == 1
        assert db.query(ChangeSet).count() == 1
        assert db.query(ClassificationGraphOutbox).count() == 1
    finally:
        db.close()


def test_second_user_rejection_does_not_remove_first_users_confirmation():
    """共享分类有多个确认来源时，一个用户撤回不能结束其他用户的事实。"""

    db = _feedback_session()
    try:
        first_user, suggestion = _seed_suggestion(db)
        second_user = User(id="user-feedback-2", username="feedback-user-2")
        second_run = AgentRun(
            id="22222222-2222-4222-8222-222222222222",
            conversation_id="conversation-feedback-2",
            message_id="message-feedback-2",
            user_id=second_user.id,
        )
        db.add_all([second_user, second_run])
        db.flush()
        service = ClassificationFeedbackService(db)
        service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                agent_run_id="11111111-1111-4111-8111-111111111111",
            ),
            current_user=first_user,
        )
        service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                agent_run_id=second_run.id,
            ),
            current_user=second_user,
        )
        service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="REJECT",
                agent_run_id=second_run.id,
            ),
            current_user=second_user,
        )

        relation = db.query(DocumentCategory).one()
        assert relation.status == "CONFIRMED"
        active_sources = (
            db.query(DocumentCategoryConfirmationSource)
            .filter(DocumentCategoryConfirmationSource.status == "ACTIVE")
            .all()
        )
        assert [item.user_id for item in active_sources] == [first_user.id]
    finally:
        db.close()


def test_no_feedback_does_not_count_as_positive_sample():
    """仅生成分类建议不能增加反馈样本数量。"""

    db = _feedback_session()
    try:
        user, _suggestion = _seed_suggestion(db)
        summary = ClassificationFeedbackService(db).summary(current_user=user)
        assert summary.total == 0
        assert summary.accepted == 0
    finally:
        db.close()


def test_shared_file_acceptance_reuses_relation_and_keeps_each_user_source():
    """不同用户确认同一共享分类时复用关系，但确认来源必须逐用户保留。"""

    db = _feedback_session()
    try:
        first_user, suggestion = _seed_suggestion(db)
        second_user = User(id="user-feedback-2", username="feedback-user-2")
        second_run = AgentRun(
            id="22222222-2222-4222-8222-222222222222",
            conversation_id="conversation-feedback-2",
            message_id="message-feedback-2",
            user_id=second_user.id,
        )
        db.add_all([second_user, second_run])
        db.flush()
        service = ClassificationFeedbackService(db)
        service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                relation_role="PRIMARY",
            ),
            current_user=first_user,
        )
        service.record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                relation_role="PRIMARY",
                agent_run_id=second_run.id,
            ),
            current_user=second_user,
        )

        assert db.query(DocumentCategory).filter(
            DocumentCategory.status == "CONFIRMED"
        ).count() == 1
        sources = db.query(DocumentCategoryConfirmationSource).filter(
            DocumentCategoryConfirmationSource.status == "ACTIVE"
        ).all()
        assert {item.user_id for item in sources} == {
            first_user.id,
            second_user.id,
        }
    finally:
        db.close()


def test_classification_graph_outbox_projects_formal_relation(monkeypatch):
    """图谱 Outbox 成功后标记完成，PostgreSQL 正式关系仍是事实源。"""

    class FakeGraphRepository:
        """记录投影调用的确定性假仓库。"""

        def __init__(self):
            self.categories = []
            self.versions = []
            self.relations = []

        def upsert_categories(self, *, categories, relations):
            self.categories.extend(categories)

        def upsert_confirmed_classifications(self, *, versions, relations, locations):
            self.versions.extend(versions)
            self.relations.extend(relations)

        def delete_confirmed_classification(self, **_kwargs):
            raise AssertionError("本测试不应删除活动正式关系")

    monkeypatch.setattr(
        "app.modules.classification.graph_outbox.log_event",
        lambda *_args, **_kwargs: None,
    )
    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        ClassificationFeedbackService(db).record(
            suggestion_id=suggestion.id,
            request=ClassificationFeedbackRequest(
                action="ACCEPT",
                relation_role="PRIMARY",
            ),
            current_user=user,
        )
        repository = FakeGraphRepository()
        outbox_id = ClassificationGraphOutboxService(
            db,
            settings=SimpleNamespace(
                graph_projection_worker_enabled=True,
                neo4j_sync_enabled=True,
            ),
            repository=repository,
        ).process_next()

        assert outbox_id
        outbox = db.get(ClassificationGraphOutbox, outbox_id)
        assert outbox.status == "COMPLETED"
        assert repository.versions[0].document_version_id == "version-feedback"
        assert repository.relations[0].source_type == "formal_classification"
    finally:
        db.close()


def test_multiple_classification_suggestions_require_signed_selection():
    """多个建议不能默认接受第一项，选择卡只公开文件名、标签和签发 ID。"""

    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        second = DocumentCategorySuggestion(
            id="suggestion-feedback-2",
            classification_run_id=suggestion.classification_run_id,
            document_id=suggestion.document_id,
            document_version_id=suggestion.document_version_id,
            category_id="school.hr.appointment-assessment",
            category_name="学校/人事师资/考核聘任",
            category_path_json=["学校", "人事师资", "考核聘任"],
            taxonomy_key=suggestion.taxonomy_key,
            taxonomy_version=suggestion.taxonomy_version,
            confidence=0.7,
            rank=2,
        )
        db.add(second)
        db.flush()

        result = ConversationalClassificationDecisionService(
            db, user.id
        ).execute(
            action="ACCEPT",
            message="这个分类是对的",
            document_ids=[suggestion.document_id],
            conversation_id="conversation-feedback",
            agent_run_id="11111111-1111-4111-8111-111111111111",
        )

        assert result["kind"] == "classification_clarification"
        public = result["classification_clarification"]
        assert len(public["options"]) == 2
        assert all(
            set(option) == {"id", "filename", "category_label"}
            for option in public["options"]
        )
        selected = ClassificationClarificationService(db).resolve(
            clarification_id=public["id"],
            user_id=user.id,
            option_id=public["options"][1]["id"],
        )
        response = ClassificationFeedbackService(db).record(
            suggestion_id=selected.suggestion_id,
            request=ClassificationFeedbackRequest(
                action=selected.action,
                relation_role=selected.relation_role,
                agent_run_id=selected.agent_run_id,
                idempotency_key=f"{public['id']}:{selected.option_id}",
            ),
            current_user=user,
        )
        ClassificationClarificationService(db).mark_resolved(
            clarification_id=public["id"],
            user_id=user.id,
            option_id=selected.option_id,
            feedback_id=response.id,
        )

        relation = db.query(DocumentCategory).one()
        assert relation.category_id == "school.hr.appointment-assessment"
        assert db.get(WorkingCopy, "working-copy-feedback").relative_path == "职称材料.docx"
    finally:
        db.close()


def test_unknown_taxonomy_target_rolls_back_formal_classification_facts():
    """不在 ACTIVE taxonomy 的目标不能产生反馈、正式关系或图谱待办。"""

    db = _feedback_session()
    try:
        user, suggestion = _seed_suggestion(db)
        with pytest.raises(HTTPException) as raised:
            ClassificationFeedbackService(db).record(
                suggestion_id=suggestion.id,
                request=ClassificationFeedbackRequest(
                    action="CORRECT",
                    corrected_category_id="free.generated.path",
                ),
                current_user=user,
            )

        assert raised.value.status_code == 422
        assert db.query(DocumentCategoryFeedback).count() == 0
        assert db.query(DocumentCategory).count() == 0
        assert db.query(ClassificationGraphOutbox).count() == 0
    finally:
        db.close()


def test_canonical_identity_rejects_changed_version_and_ambiguous_copies():
    """身份解析不能按哈希合并旧版本或多个工作副本。"""

    db = _feedback_session()
    try:
        _user, suggestion = _seed_suggestion(db)
        resolver = CanonicalWorkingFileResolver(db)
        with pytest.raises(CanonicalWorkingFileError) as changed:
            resolver.resolve_document(
                document_id=suggestion.document_id,
                document_version_id="different-version",
            )
        assert changed.value.code == "DOCUMENT_VERSION_CHANGED"

        original = db.get(WorkingCopy, "working-copy-feedback")
        duplicate = WorkingCopy(
            id="working-copy-feedback-2",
            working_copy_root_id=original.working_copy_root_id,
            workspace_id=original.workspace_id,
            managed_file_id=original.managed_file_id,
            document_id=original.document_id,
            current_version_id=original.current_version_id,
            relative_path="副本/职称材料.docx",
            relative_path_hash="d" * 64,
            filename=original.filename,
            extension=original.extension,
            size_bytes=original.size_bytes,
            content_sha256=original.content_sha256,
            imported_source_sha256=original.imported_source_sha256,
            is_primary_import=False,
            status="ACTIVE",
            sync_status="SYNCED",
        )
        db.add(duplicate)
        db.flush()
        with pytest.raises(CanonicalWorkingFileError) as ambiguous:
            resolver.resolve_document(document_id=suggestion.document_id)
        assert ambiguous.value.code == "WORKING_COPY_AMBIGUOUS"
    finally:
        db.close()
