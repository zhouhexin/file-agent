"""OperationPlan 最小闭环测试。

这些测试只验证通用计划、查询和确认的安全边界；真实文件重命名由独立测试覆盖。
"""

from app.db.models import OperationConfirmation, OperationPlan
from app.tests.helpers import clear_overrides, client_with_database


def _register_and_login(client, username: str) -> tuple[str, str]:
    """注册并登录测试用户，返回用户 id 和 access token。"""

    register_response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "password123", "display_name": username},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return register_response.json()["id"], login_response.json()["access_token"]


def _auth_header(token: str) -> dict[str, str]:
    """构造认证请求头。"""

    return {"Authorization": f"Bearer {token}"}


def _create_plan(client, token: str, conversation_id: str = "op-conv"):
    """创建一个尚未接入真实执行器的高风险移动计划。"""

    return client.post(
        "/api/operations/plans",
        headers=_auth_header(token),
        json={
            "conversation_id": conversation_id,
            "operation_type": "MOVE_FILES",
            "risk_level": "medium",
            "reason": "生成标准化文件名建议",
            "items": [
                {
                    "document_id": "document-1",
                    "before": {"filename": "旧文件名.pdf"},
                    "after": {"filename": "新文件名.pdf"},
                }
            ],
        },
    )


def test_create_operation_plan_persists_waiting_confirmation():
    """创建高风险计划时只落库等待确认，不执行真实动作。"""

    client, SessionLocal = client_with_database()
    user_id, token = _register_and_login(client, "operation-owner")

    response = _create_plan(client, token)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "WAITING_CONFIRMATION"
    assert data["operation_type"] == "MOVE_FILES"
    assert data["requires_confirmation"] is True
    assert data["risk_level"] == "medium"
    assert data["items"][0]["execution_status"] == "PLANNED"

    db = SessionLocal()
    try:
        plan = db.query(OperationPlan).one()
        assert plan.user_id == user_id
        assert plan.status == "WAITING_CONFIRMATION"
        assert plan.plan_json["items"][0]["after"]["filename"] == "新文件名.pdf"
        assert db.query(OperationConfirmation).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_direct_rename_plan_creation_is_rejected():
    """受管和上传附件重命名计划都必须由受控建议 Tool 生成，不能提交任意 before/after。"""

    client, _ = client_with_database()
    _, token = _register_and_login(client, "operation-direct-rename")
    for operation_type in ["RENAME_FILES", "RENAME_UPLOADED_FILES"]:
        response = client.post(
            "/api/operations/plans",
            headers=_auth_header(token),
            json={
                "conversation_id": "direct-rename-conversation",
                "operation_type": operation_type,
                "items": [{
                    "document_id": "document-1",
                    "before": {"filename": "旧文件名.pdf"},
                    "after": {"filename": "新文件名.pdf"},
                }],
            },
        )
        assert response.status_code == 400
    clear_overrides()


def test_get_operation_plan_returns_owned_plan():
    """当前用户可以查询自己创建的 OperationPlan。"""

    client, _ = client_with_database()
    _, token = _register_and_login(client, "operation-reader")
    create_response = _create_plan(client, token)
    plan_id = create_response.json()["id"]

    response = client.get(f"/api/operations/plans/{plan_id}", headers=_auth_header(token))

    assert response.status_code == 200
    assert response.json()["id"] == plan_id
    assert response.json()["status"] == "WAITING_CONFIRMATION"
    clear_overrides()


def test_other_user_cannot_get_or_confirm_operation_plan():
    """用户不能读取或确认其他用户的 OperationPlan。"""

    client, _ = client_with_database()
    _, owner_token = _register_and_login(client, "operation-private-owner")
    _, other_token = _register_and_login(client, "operation-private-other")
    plan_id = _create_plan(client, owner_token).json()["id"]

    get_response = client.get(f"/api/operations/plans/{plan_id}", headers=_auth_header(other_token))
    confirm_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(other_token),
        json={"confirmation": "确认执行"},
    )

    assert get_response.status_code == 404
    assert confirm_response.status_code == 404
    clear_overrides()


def test_confirm_operation_plan_rejects_operation_without_executor():
    """没有受控执行器的高风险计划必须拒绝确认，不能伪造 EXECUTED 状态。"""

    client, SessionLocal = client_with_database()
    _, token = _register_and_login(client, "operation-confirmer")
    plan_id = _create_plan(client, token).json()["id"]

    response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Operation type does not have a controlled executor"

    db = SessionLocal()
    try:
        plan = db.get(OperationPlan, plan_id)
        assert plan is not None
        assert plan.status == "WAITING_CONFIRMATION"
        assert plan.confirmed_at is None
        assert plan.executed_at is None
        assert db.query(OperationConfirmation).count() == 0
    finally:
        db.close()
        clear_overrides()


def test_confirm_operation_plan_rejects_repeated_confirmation():
    """没有执行器的计划无论确认多少次都必须保持待确认，不能产生确认记录。"""

    client, _ = client_with_database()
    _, token = _register_and_login(client, "operation-repeat")
    plan_id = _create_plan(client, token).json()["id"]

    first_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )
    second_response = client.post(
        f"/api/operations/plans/{plan_id}/confirm",
        headers=_auth_header(token),
        json={"confirmation": "确认执行"},
    )

    assert first_response.status_code == 409
    assert second_response.status_code == 409
    clear_overrides()
