"""管理员 AgentRun 中文诊断接口测试。

这些测试保护权限边界和可读运维投影：普通用户不能读取诊断，管理员无需解析原始 JSON
即可看到任务状态、后台依赖和建议动作。
"""

from __future__ import annotations

from app.core import config
from app.core.logging import log_event
from app.db.models import AgentRun, Conversation, FilesystemJob, Message, User
from app.tests.helpers import clear_overrides, client_with_database


def _register_and_login(client, *, username: str) -> str:
    """注册并登录测试用户，返回 Bearer token。"""

    client.post(
        "/api/auth/register",
        json={
            "username": username,
            "password": "password123",
            "display_name": username,
        },
    )
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def test_agent_diagnostics_requires_ops_or_admin():
    """普通 user 即使知道 AgentRun ID，也不能访问内部运维时间线。"""

    client, _session_factory = client_with_database()
    token = _register_and_login(client, username="diagnostic-user")

    response = client.get(
        "/api/admin/agent-runs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    clear_overrides()


def test_admin_reads_chinese_agent_run_diagnostics(
    monkeypatch,
    tmp_path,
):
    """管理员应看到等待后台任务的中文结论和 worker 处置建议。"""

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    config.get_settings.cache_clear()
    client, session_factory = client_with_database()
    _register_and_login(client, username="diagnostic-admin")
    db = session_factory()
    try:
        admin = db.query(User).filter(User.username == "diagnostic-admin").one()
        admin.role = "admin"
        conversation = Conversation(
            id="conv-diagnostic",
            user_id=admin.id,
            title="诊断测试",
        )
        message = Message(
            id="msg-diagnostic",
            conversation_id=conversation.id,
            user_id=admin.id,
            role="user",
            content="查找未来五年规划文件",
        )
        job = FilesystemJob(
            id="job-diagnostic",
            job_type="ANALYZE_DOCUMENT_VERSION",
            queue_name="ANALYZE",
            status="PENDING",
            created_by=admin.id,
        )
        run = AgentRun(
            id="run-diagnostic",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=admin.id,
            intent="SEARCH_FILES",
            status="WAITING_FOR_ASYNC_JOB",
            planner_mode="llm",
            graph_state_json={
                "async_job_ids": [job.id],
                "result_summary": {
                    "filesystem_job": {"source": "search-readiness"}
                },
            },
        )
        db.add_all([conversation, message, job, run])
        db.commit()
        log_event(
            "retrieval.waiting_run.resume_waiting",
            agent_run_id=run.id,
            user_id=admin.id,
            conversation_id=conversation.id,
            status="WAITING_FOR_ASYNC_JOB",
            event_title="等待文件索引",
            stage="ASYNC_JOB",
            operator_message=(
                "目标文件 /srv/private/未来五年规划.docx 尚未完成索引，"
                "系统正在等待后台处理。"
            ),
            recommended_action="确认 worker 已启动。",
            filesystem_job_id=job.id,
        )
    finally:
        db.close()

    # 角色修改后重新登录，确保 JWT 采用数据库中的最新角色。
    login = client.post(
        "/api/auth/login",
        json={"username": "diagnostic-admin", "password": "password123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    list_response = client.get("/api/admin/agent-runs", headers=headers)
    detail_response = client.get(
        "/api/admin/agent-runs/run-diagnostic/diagnostics",
        headers=headers,
    )

    assert list_response.status_code == 200
    assert list_response.json()[0]["id"] == "run-diagnostic"
    assert detail_response.status_code == 200
    payload = detail_response.json()
    assert "等待文件解析或索引" in payload["summary"]
    assert any("worker" in item for item in payload["recommended_actions"])
    assert any(
        event["event_title"] == "等待文件索引"
        and "后台处理" in event["operator_message"]
        for event in payload["events"]
    )
    assert "/srv/private" not in str(payload)
    assert all("exception_traceback" not in event for event in payload["events"])
    clear_overrides()
