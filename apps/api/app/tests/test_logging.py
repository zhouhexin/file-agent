"""轻量结构化日志测试。"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

from app.core import config
from app.core.logging import cleanup_old_logs
from app.tests.helpers import clear_overrides, client_with_database


def test_api_request_writes_jsonl_log_with_request_id(monkeypatch, tmp_path):
    """API 请求必须生成 request_id 响应头，并写入 JSONL 请求日志。"""

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    client, _ = client_with_database()

    response = client.get("/api/health", headers={"X-Request-ID": "req-test-001"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-test-001"
    log_lines = _read_jsonl_logs(tmp_path)
    completed = [item for item in log_lines if item["event"] == "api.request.completed"]
    assert completed
    assert completed[-1]["request_id"] == "req-test-001"
    assert completed[-1]["status"] == "COMPLETED"
    assert completed[-1]["duration_ms"] >= 0
    clear_overrides()
    config.get_settings.cache_clear()


def test_cleanup_old_logs_removes_files_older_than_retention(monkeypatch, tmp_path):
    """日志清理必须删除超过保留天数的本地日志文件。"""

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_RETENTION_DAYS", "7")
    config.get_settings.cache_clear()
    old_log = tmp_path / "file-agent-old.log"
    fresh_log = tmp_path / "file-agent-fresh.log"
    old_log.write_text("{}\n", encoding="utf-8")
    fresh_log.write_text("{}\n", encoding="utf-8")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    fresh_timestamp = time.time()
    os.utime(old_log, (old_timestamp, old_timestamp))
    os.utime(fresh_log, (fresh_timestamp, fresh_timestamp))

    cleanup_old_logs()

    assert not old_log.exists()
    assert fresh_log.exists()
    config.get_settings.cache_clear()


def test_message_processing_writes_agent_tool_file_classification_and_changeset_logs(monkeypatch, tmp_path):
    """消息处理链路必须写入 Agent、Tool、文件解析、分类和 ChangeSet 日志。"""

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("FILE_STORAGE_ROOT", str(tmp_path / "storage"))
    config.get_settings.cache_clear()
    client, _ = client_with_database()
    auth_header = _auth_header(client)

    upload_response = client.post(
        "/api/files/upload",
        headers=auth_header,
        files={"file": ("log.txt", b"zhicheng zuzhi", "text/plain")},
    )
    document_id = upload_response.json()["document_id"]

    response = client.post(
        "/api/conversations/log-conv/messages",
        headers=auth_header,
        json={
            "content": "帮我读取并分类这批文件",
            "attachments": [{"document_id": document_id}],
        },
    )

    assert response.status_code == 200
    events = {item["event"] for item in _read_jsonl_logs(tmp_path / "logs")}
    assert "agent.run.completed" in events
    assert "agent.node.completed" in events
    assert "tool.invoke.completed" in events
    assert "file.extract.completed" in events
    assert "classification.completed" in events
    assert "changeset.created" in events
    clear_overrides()
    config.get_settings.cache_clear()


def test_file_search_writes_end_to_end_node_diagnostics(monkeypatch, tmp_path):
    """文件检索必须记录从 Planner 到就绪检查的完整节点，不写入查询正文。"""

    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TWO_STAGE_RETRIEVAL_ENABLED", "true")
    config.get_settings.cache_clear()
    client, _ = client_with_database()
    auth_header = _auth_header(client)

    response = client.post(
        "/api/conversations/search-log-conv/messages",
        headers=auth_header,
        json={
            "content": "帮我找2025年计算机学院的工作总结",
            "attachments": [],
        },
    )

    assert response.status_code == 200
    records = _read_jsonl_logs(tmp_path / "logs")
    events = {item["event"] for item in records}
    assert {
        "retrieval.planner.file_search_plan",
        "retrieval.request.started",
        "retrieval.attachment_scope.canonicalized",
        "retrieval.trash_lookup.completed",
        "retrieval.route.selected",
        "retrieval.query.parsed",
        "retrieval.scope.resolved",
        "retrieval.strategy.selected",
        "retrieval.phrase_strategy.started",
        "retrieval.phrase_strategy.phrase_completed",
        "retrieval.phrase_strategy.completed",
        "retrieval.stage1.completed",
        "retrieval.chunk_fallback.completed",
        "retrieval.stage2.skipped",
        "retrieval.evidence.skipped",
        "retrieval.search.completed",
        "retrieval.strategy.intersection_completed",
        "retrieval.active_scope.completed",
        "retrieval.readiness.started",
        "retrieval.readiness.candidates_resolved",
        "retrieval.readiness.completed",
        "retrieval.request.completed",
    }.issubset(events)
    retrieval_records = [
        item for item in records if str(item.get("event") or "").startswith("retrieval.")
    ]
    assert retrieval_records
    assert all(
        "帮我找2025年计算机学院的工作总结"
        not in json.dumps(item, ensure_ascii=False)
        for item in retrieval_records
    )
    clear_overrides()
    config.get_settings.cache_clear()


def _auth_header(client) -> dict[str, str]:
    """注册并登录日志测试用户。"""

    client.post(
        "/api/auth/register",
        json={"username": "log-user", "password": "password123", "display_name": "log-user"},
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": "log-user", "password": "password123"},
    )
    return {"Authorization": f"Bearer {login_response.json()['access_token']}"}


def _read_jsonl_logs(log_dir) -> list[dict]:
    """读取测试目录中的全部 JSONL 日志。"""

    items: list[dict] = []
    for path in sorted(log_dir.glob("file-agent-*.log")):
        for line in path.read_text(encoding="utf-8").splitlines():
            items.append(json.loads(line))
    return items
