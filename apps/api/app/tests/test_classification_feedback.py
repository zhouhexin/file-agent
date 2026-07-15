"""无标注冷启动分类反馈测试。"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    AgentRun,
    Document,
    DocumentCategoryFeedback,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    User,
)
from app.modules.classification.feedback_schemas import ClassificationFeedbackRequest
from app.modules.classification.feedback_service import ClassificationFeedbackService


def _feedback_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_suggestion(db):
    user = User(id="user-feedback", username="feedback-user")
    document = Document(
        id="document-feedback",
        user_id=user.id,
        original_filename="职称材料.docx",
        size_bytes=100,
        sha256="c" * 64,
    )
    agent_run = AgentRun(
        id="agent-run-feedback",
        conversation_id="conversation-feedback",
        message_id="message-feedback",
        user_id=user.id,
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
        document_version_id=document.id,
        category_id="school.hr.title-review",
        category_name="学校/人事师资/职称",
        category_path_json=["学校", "人事师资", "职称"],
        taxonomy_key="school_file_classification",
        taxonomy_version="2026-06-v2",
        confidence=0.8,
        rank=1,
    )
    db.add_all([user, document, agent_run, classification_run, suggestion])
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
            request=ClassificationFeedbackRequest(action="ACCEPT"),
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
        assert corrected.positive_category_ids == ["school.hr.appointment-assessment"]
        assert corrected.negative_category_ids == ["school.hr.title-review"]
        rows = db.query(DocumentCategoryFeedback).order_by(DocumentCategoryFeedback.created_at).all()
        assert rows[0].is_active is False
        assert rows[1].supersedes_feedback_id == rows[0].id
        summary = service.summary(current_user=user)
        assert summary.total == 1
        assert summary.corrected == 1
        assert summary.ready_to_freeze_evaluation_set is False
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
