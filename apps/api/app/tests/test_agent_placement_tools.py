"""D5 分类落位 Agent Tool 的授权、受理与只读状态测试。"""

from __future__ import annotations

from app.db.models import (
    AgentRun,
    ClassificationPlacementOperation,
    Conversation,
    Message,
    OperationConfirmation,
    OperationPlan,
    User,
)
from app.modules.agent.tool_registry import ToolRegistry
from app.tests.helpers import client_with_database
from app.tests.test_file_lifecycle import _auth, _configure, _drain, _upload


def test_placement_submit_and_status_tools_use_real_agent_message(monkeypatch, tmp_path):
    """submit 只接受真实消息授权，status 仅读取持久化进度。"""

    _configure(monkeypatch, tmp_path)
    client, session_factory = client_with_database()
    headers = _auth(client, "placement-tool-owner")
    _upload(client, headers, "Tool主分类材料.txt", b"tool placement material")
    _drain(session_factory)
    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    taxonomy = client.get("/api/classification/taxonomy/options", headers=headers).json()

    db = session_factory()
    try:
        user = db.query(User).filter(User.username == "placement-tool-owner").one()
        user_id = user.id
        conversation = Conversation(
            id="12121212-1212-4212-8212-121212121212",
            user_id=user.id,
            title="分类落位 Tool 测试",
        )
        message = Message(
            id="13131313-1313-4313-8313-131313131313",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="把这份文件的主分类改为财务",
            attachments_json=[],
        )
        run = AgentRun(
            id="14141414-1414-4414-8414-141414141414",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
        )
        db.add_all([conversation, message, run])
        db.commit()
        submitted = ToolRegistry(db=db, user_id=user.id).invoke(
            "working-copy-placement-submit",
            {
                "action": "SET_PRIMARY",
                "working_copy_id": working_copy["id"],
                "expected_revision": working_copy["revision"],
                "expected_document_version_id": working_copy["current_version_id"],
                "target_category_id": "college.finance",
                "taxonomy_version": taxonomy["taxonomy_version"],
                "idempotency_key": "placement-tool-submit-1",
                "conversation_id": conversation.id,
                "agent_run_id": run.id,
            },
        )
        assert submitted.status == "COMPLETED"
        assert submitted.output_json["status"] == "PREPARED"
        assert submitted.output_json.get("file_position_changed") is None
        assert submitted.placement_operation_id
        operation = db.get(ClassificationPlacementOperation, submitted.placement_operation_id)
        assert operation is not None
        assert db.get(OperationPlan, operation.operation_plan_id).status == "AUTHORIZED"
        assert db.query(OperationConfirmation).count() == 0
        db.commit()
    finally:
        db.close()

    _drain(session_factory)

    db = session_factory()
    try:
        observed = ToolRegistry(db=db, user_id=user_id).invoke(
            "working-copy-placement-status",
            {"operation_id": submitted.placement_operation_id},
        )
        assert observed.status == "COMPLETED"
        assert observed.output_json["status"] == "COMMITTED"
        assert observed.output_json["placement_status"] == "IN_SYNC"
        assert observed.output_json["file_position_changed"] is True
    finally:
        db.close()
