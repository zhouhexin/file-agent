"""D3 分类落位命令、授权快照和幂等占用测试。"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.db.models import (
    ClassificationPlacementOperation,
    Conversation,
    Document,
    DocumentVersion,
    ManagedFile,
    ManagedRoot,
    Message,
    OperationConfirmation,
    OperationPlan,
    User,
    WorkingCopy,
    WorkingCopyRoot,
    Workspace,
)
from app.modules.classification.placement_authorization import (
    PlacementAuthorizationError,
    PlacementAuthorizationService,
)
from app.modules.classification.placement_repository import (
    ClassificationPlacementRepository,
    PlacementOperationDraft,
    PlacementRepositoryError,
)
from app.modules.classification.placement_schemas import PlacementCommand


USER_ID = "11111111-1111-4111-8111-111111111111"
WORKSPACE_ID = "22222222-2222-4222-8222-222222222222"
CONVERSATION_ID = "33333333-3333-4333-8333-333333333333"
WORKING_COPY_ID = "44444444-4444-4444-8444-444444444444"
DOCUMENT_ID = "55555555-5555-4555-8555-555555555555"
VERSION_ID = "66666666-6666-4666-8666-666666666666"


def _session():
    """创建启用完整模型约束的隔离 SQLite 会话。"""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed(db):
    """创建一个活动共享工作副本及两条真实用户消息。"""

    user = User(id=USER_ID, username="placement-user")
    workspace = Workspace(
        id=WORKSPACE_ID,
        name="共享工作区",
        workspace_type="SYSTEM_SHARED",
        system_key="placement-test",
    )
    conversation = Conversation(
        id=CONVERSATION_ID,
        user_id=user.id,
        workspace_id=workspace.id,
        title="分类更正",
    )
    explicit_message = Message(
        id="77777777-7777-4777-8777-777777777777",
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content="把这个个文件的主分类改为学院财务管理。",
    )
    suggestion_message = Message(
        id="88888888-8888-4888-8888-888888888888",
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content="只建议一下怎么分类，先不要执行。",
    )
    document = Document(
        id=DOCUMENT_ID,
        user_id=user.id,
        workspace_id=workspace.id,
        original_filename="财务材料.docx",
        size_bytes=32,
        sha256="a" * 64,
    )
    version = DocumentVersion(
        id=VERSION_ID,
        document_id=document.id,
        version_number=1,
        filename=document.original_filename,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=document.size_bytes,
        sha256=document.sha256,
        storage_path="documents/finance.docx",
    )
    managed_root = ManagedRoot(
        id="99999999-9999-4999-8999-999999999999",
        root_key="placement-source",
        display_name="测试原件",
        container_path="/managed/source",
    )
    managed_file = ManagedFile(
        id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        root_id=managed_root.id,
        relative_path=document.original_filename,
        relative_path_hash="b" * 64,
        filename=document.original_filename,
        extension=".docx",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
    )
    working_root = WorkingCopyRoot(
        id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        workspace_id=workspace.id,
        managed_root_id=managed_root.id,
        root_key="placement-working",
        relative_storage_path="shared/working",
        status="ACTIVE",
    )
    working_copy = WorkingCopy(
        id=WORKING_COPY_ID,
        working_copy_root_id=working_root.id,
        workspace_id=workspace.id,
        managed_file_id=managed_file.id,
        document_id=document.id,
        current_version_id=version.id,
        relative_path="其他/财务材料.docx",
        relative_path_hash="c" * 64,
        filename=document.original_filename,
        extension=".docx",
        size_bytes=document.size_bytes,
        content_sha256=document.sha256,
        imported_source_sha256=document.sha256,
        status="ACTIVE",
    )
    db.add_all(
        [
            user,
            workspace,
            conversation,
            explicit_message,
            suggestion_message,
            document,
            version,
            managed_root,
            managed_file,
            working_root,
            working_copy,
        ]
    )
    db.flush()
    return user, working_copy, explicit_message, suggestion_message


def _command(*, key: str = "submit-1", category_id: str = "college.finance"):
    """构造合法、稳定版本绑定的 SET_PRIMARY 命令。"""

    return PlacementCommand(
        working_copy_id=UUID(WORKING_COPY_ID),
        action="SET_PRIMARY",
        expected_revision=1,
        expected_document_version_id=UUID(VERSION_ID),
        target_category_id=category_id,
        taxonomy_version="2026-09-v10",
        idempotency_key=key,
    )


def _draft(working_copy, *, category_id: str = "college.finance"):
    """构造已通过 taxonomy 和路径解析的冻结草稿。"""

    return PlacementOperationDraft(
        working_copy=working_copy,
        source_sha256=working_copy.content_sha256,
        source_identity={"document_version_id": VERSION_ID},
        before_primary_relation_id=None,
        target_relative_path="学院/财务管理/财务材料.docx",
        target_category_id=category_id,
        taxonomy_key="unified_school_file_classification",
        taxonomy_digest="d" * 64,
        policy_version="workdata-v1",
        target_filename=working_copy.filename,
        decision_snapshot={"classification_outcome": "CLASSIFIED"},
    )


def test_t14_schema_rejects_client_authorization_injection_without_writes():
    """T14：客户端注入授权/actor 字段返回校验失败，且数据库没有副作用。"""

    db = _session()
    try:
        payload = _command().model_dump(mode="json")
        payload.update({"skip_confirmation": True, "actor_user_id": USER_ID})
        with pytest.raises(ValidationError):
            PlacementCommand.model_validate(payload)
        assert db.query(OperationPlan).count() == 0
        assert db.query(ClassificationPlacementOperation).count() == 0
    finally:
        db.close()


def test_t15_only_structured_or_explicit_persisted_message_authorizes():
    """T15：专用提交与明确原始消息可授权；只建议消息不能由 LLM 字符串补成授权。"""

    db = _session()
    try:
        user, _working_copy, explicit, suggestion = _seed(db)
        service = PlacementAuthorizationService(db)
        structured = service.authorize_structured_request(
            command=_command(),
            current_user=user,
            workspace_id=WORKSPACE_ID,
            client_id="web",
            request_id="request-structured",
            source_event_ref="http:request-structured",
        )
        chat = service.authorize_chat_message(
            command=_command(key="chat-1"),
            current_user=user,
            message_id=explicit.id,
            workspace_id=WORKSPACE_ID,
            client_id="chat",
            request_id="request-chat",
        )
        assert structured.authorization_mode == "EXPLICIT_REQUEST"
        assert chat.source_event_ref == f"message:{explicit.id}"
        with pytest.raises(PlacementAuthorizationError) as exc_info:
            service.authorize_chat_message(
                command=_command(key="chat-2"),
                current_user=user,
                message_id=suggestion.id,
                workspace_id=WORKSPACE_ID,
                client_id="chat",
                request_id="request-suggestion",
            )
        assert exc_info.value.code == "EXPLICIT_EXECUTION_NOT_REQUESTED"
    finally:
        db.close()


def test_t16_direct_request_uses_authorized_plan_without_fake_confirmation():
    """T16：直接更正由授权计划执行，不插入 OperationConfirmation。"""

    db = _session()
    try:
        user, working_copy, _explicit, _suggestion = _seed(db)
        command = _command()
        authorization_service = PlacementAuthorizationService(db)
        context = authorization_service.authorize_structured_request(
            command=command,
            current_user=user,
            workspace_id=WORKSPACE_ID,
            client_id="api",
            request_id="request-direct",
            source_event_ref="http:request-direct",
        )
        plan = OperationPlan(
            workspace_id=WORKSPACE_ID,
            conversation_id=None,
            user_id=user.id,
            operation_type="CLASSIFICATION_PLACEMENT",
            status="AUTHORIZED",
            authorization_mode="EXPLICIT_REQUEST",
            authorization_context_json=context.model_dump(mode="json"),
            plan_json={},
        )
        db.add(plan)
        db.flush()
        operation, created = ClassificationPlacementRepository(db).prepare(
            command=command,
            authorization=context,
            draft=_draft(working_copy),
            operation_plan_id=plan.id,
        )
        authorization_service.authorize_execution(
            plan=plan,
            operation=operation,
            context=context,
        )
        assert created is True
        assert db.query(OperationConfirmation).count() == 0
        assert operation.authorization_snapshot_json["source_event_ref"] == "http:request-direct"
    finally:
        db.close()


def test_t17_idempotency_conflict_and_active_copy_occupancy():
    """T17：同键同参返回原操作，同键异参冲突，同副本第二个活动写被拒绝。"""

    db = _session()
    try:
        user, working_copy, _explicit, _suggestion = _seed(db)
        auth_service = PlacementAuthorizationService(db)
        command = _command()
        context = auth_service.authorize_structured_request(
            command=command,
            current_user=user,
            workspace_id=WORKSPACE_ID,
            client_id="mcp",
            request_id="request-1",
            source_event_ref="mcp:request-1",
        )
        repository = ClassificationPlacementRepository(db)
        first, created = repository.prepare(
            command=command,
            authorization=context,
            draft=_draft(working_copy),
        )
        repeated, repeated_created = repository.prepare(
            command=command,
            authorization=context,
            draft=_draft(working_copy),
        )
        assert created is True
        assert repeated_created is False
        assert repeated.id == first.id

        changed = _command(category_id="school.finance")
        changed_context = auth_service.authorize_structured_request(
            command=changed,
            current_user=user,
            workspace_id=WORKSPACE_ID,
            client_id="mcp",
            request_id="request-2",
            source_event_ref="mcp:request-2",
        )
        with pytest.raises(PlacementRepositoryError) as idempotency_error:
            repository.prepare(
                command=changed,
                authorization=changed_context,
                draft=_draft(working_copy, category_id="school.finance"),
            )
        assert idempotency_error.value.code == "IDEMPOTENCY_CONFLICT"

        second = _command(key="submit-2")
        second_context = auth_service.authorize_structured_request(
            command=second,
            current_user=user,
            workspace_id=WORKSPACE_ID,
            client_id="mcp",
            request_id="request-3",
            source_event_ref="mcp:request-3",
        )
        with pytest.raises(PlacementRepositoryError) as active_error:
            repository.prepare(
                command=second,
                authorization=second_context,
                draft=_draft(working_copy),
            )
        assert active_error.value.code == "PLACEMENT_IN_PROGRESS"
    finally:
        db.close()


def test_t18_delete_overwrite_rename_and_source_path_cannot_borrow_move_authority():
    """T18：窄范围命令不能夹带删除、覆盖、独立改名或任意源路径。"""

    base = _command().model_dump(mode="json")
    for injected in (
        {**base, "action": "DELETE"},
        {**base, "source_path": "C:/private/source.docx"},
        {**base, "target_filename": "new-name.docx"},
        {**base, "overwrite": True},
    ):
        with pytest.raises(ValidationError):
            PlacementCommand.model_validate(injected)
