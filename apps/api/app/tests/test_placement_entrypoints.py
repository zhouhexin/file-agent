"""D5/T16、T26、T30 分类落位 HTTP 入口的授权与状态测试。"""

from __future__ import annotations

from app.db.models import (
    ClassificationPlacementOperation,
    OperationConfirmation,
    OperationPlan,
)
from app.tests.helpers import client_with_database
from app.tests.test_file_lifecycle import _auth, _configure, _drain, _upload


def _command_from_public_metadata(client, headers) -> dict[str, object]:
    """只使用公开元数据和 taxonomy 选项构造一次稳定的主分类更正命令。"""

    working_copy = client.get("/api/working-copies", headers=headers).json()[0]
    taxonomy = client.get("/api/classification/taxonomy/options", headers=headers).json()
    return {
        "working_copy_id": working_copy["id"],
        "action": "SET_PRIMARY",
        "expected_revision": working_copy["revision"],
        "expected_document_version_id": working_copy["current_version_id"],
        "target_category_id": "college.finance",
        "taxonomy_version": taxonomy["taxonomy_version"],
        "idempotency_key": "placement-http-1",
    }


def _path_payload_from_public_metadata(client, headers) -> tuple[str, dict[str, object]]:
    """专用路径只接收目标和并发事实，工作副本 ID 必须来自 URL。"""

    command = _command_from_public_metadata(client, headers)
    working_copy_id = str(command.pop("working_copy_id"))
    command.pop("action")
    return working_copy_id, command


def test_http_set_primary_uses_direct_authorization_and_returns_real_status(monkeypatch, tmp_path):
    """明确 API 提交只受理一次，随后由 worker 完成，不生成假确认记录。"""

    _configure(monkeypatch, tmp_path)
    client, session_factory = client_with_database()
    owner_headers = _auth(client, "placement-http-owner")
    _upload(client, owner_headers, "财务归档材料.txt", b"finance archive material")
    _drain(session_factory)

    submission_response = client.post(
        "/api/classification/placements",
        headers=owner_headers,
        json=_command_from_public_metadata(client, owner_headers),
    )

    assert submission_response.status_code == 202
    submission = submission_response.json()
    assert submission["status"] == "PREPARED"
    assert submission["placement_status"] == "PENDING"
    assert submission["requires_confirmation"] is False
    assert submission["created"] is True

    db = session_factory()
    try:
        operation = db.get(ClassificationPlacementOperation, submission["operation_id"])
        assert operation is not None
        assert operation.state == "PREPARED"
        assert db.get(OperationPlan, operation.operation_plan_id).status == "AUTHORIZED"
        assert db.query(OperationConfirmation).count() == 0
    finally:
        db.close()

    pending = client.get(
        f"/api/classification/placements/{submission['operation_id']}",
        headers=owner_headers,
    )
    assert pending.status_code == 200
    assert pending.json()["status"] == "PREPARED"
    assert pending.json()["result"] == {}

    _drain(session_factory)

    completed = client.get(
        f"/api/classification/placements/{submission['operation_id']}",
        headers=owner_headers,
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "COMMITTED"
    assert completed.json()["placement_status"] == "IN_SYNC"
    assert completed.json()["result"]["original_unchanged"] is True
    assert str(tmp_path) not in completed.text

    other_headers = _auth(client, "placement-http-other")
    hidden = client.get(
        f"/api/classification/placements/{submission['operation_id']}",
        headers=other_headers,
    )
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "PLACEMENT_NOT_FOUND"


def test_path_bound_set_primary_reuses_placement_coordinator(monkeypatch, tmp_path):
    """文档约定的专用路径必须同样异步受理，且正文不能替换 URL 中的副本。"""

    _configure(monkeypatch, tmp_path)
    client, session_factory = client_with_database()
    headers = _auth(client, "placement-http-path")
    _upload(client, headers, "专用路径归档材料.txt", b"path-bound placement")
    _drain(session_factory)
    working_copy_id, payload = _path_payload_from_public_metadata(client, headers)

    submission = client.post(
        f"/api/classification/working-copies/{working_copy_id}/primary-category",
        headers=headers,
        json=payload,
    )

    assert submission.status_code == 202
    body = submission.json()
    assert body["working_copy_id"] == working_copy_id
    assert body["status"] == "PREPARED"
    assert body["file_position_changed"] is None
    assert body["requires_confirmation"] is False

    status_response = client.get(
        f"/api/classification/placement-operations/{body['operation_id']}",
        headers=headers,
    )
    assert status_response.status_code == 200
    assert status_response.json()["operation_id"] == body["operation_id"]

    injected = dict(payload)
    injected["working_copy_id"] = "00000000-0000-4000-8000-000000000000"
    rejected = client.post(
        f"/api/classification/working-copies/{working_copy_id}/primary-category",
        headers=headers,
        json=injected,
    )
    assert rejected.status_code == 422

    db = session_factory()
    try:
        assert db.query(ClassificationPlacementOperation).count() == 1
        assert db.query(OperationConfirmation).count() == 0
    finally:
        db.close()


def test_integration_path_uses_workbuddy_client_identity(monkeypatch, tmp_path):
    """外部连接器路径应复用协调器，但审计来源必须与普通 API 可区分。"""

    _configure(monkeypatch, tmp_path)
    client, session_factory = client_with_database()
    headers = _auth(client, "placement-http-integration")
    _upload(client, headers, "连接器归档材料.txt", b"integration placement")
    _drain(session_factory)
    working_copy_id, payload = _path_payload_from_public_metadata(client, headers)

    submission = client.post(
        f"/api/integrations/v1/working-copies/{working_copy_id}/primary-category",
        headers=headers,
        json=payload,
    )

    assert submission.status_code == 202
    operation_id = submission.json()["operation_id"]
    status_response = client.get(
        f"/api/integrations/v1/placement-operations/{operation_id}",
        headers=headers,
    )
    assert status_response.status_code == 200
    db = session_factory()
    try:
        operation = db.get(ClassificationPlacementOperation, operation_id)
        assert operation.client_id == "workbuddy-mcp"
        assert operation.authorization_snapshot_json["source_event_ref"].startswith(
            "integration:workbuddy-mcp:"
        )
    finally:
        db.close()


def test_http_placement_rejects_client_authorization_injection_before_writes(monkeypatch, tmp_path):
    """公开入口拒绝权限注入字段，且在参数校验阶段不创建审计或任务。"""

    _configure(monkeypatch, tmp_path)
    client, session_factory = client_with_database()
    headers = _auth(client, "placement-http-injection")
    _upload(client, headers, "待归档材料.txt", b"pending placement")
    _drain(session_factory)
    payload = _command_from_public_metadata(client, headers)
    payload.update({"skip_confirmation": True, "actor_user_id": "forged-user"})

    response = client.post("/api/classification/placements", headers=headers, json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    db = session_factory()
    try:
        assert db.query(ClassificationPlacementOperation).count() == 0
        assert db.query(OperationPlan).count() == 0
    finally:
        db.close()
