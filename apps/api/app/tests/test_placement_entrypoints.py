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
