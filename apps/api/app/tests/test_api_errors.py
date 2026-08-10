"""统一 API 错误 Envelope 回归测试。"""

from __future__ import annotations

from app.tests.helpers import clear_overrides, client_with_database


def test_unknown_route_uses_unified_not_found_envelope():
    """框架级 404 也不能退回 FastAPI 默认 detail 格式。"""

    client, _ = client_with_database()
    response = client.get(
        "/api/not-a-real-route",
        headers={"X-Request-ID": "request-not-found-test"},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "request-not-found-test"
    assert response.json() == {
        "error": {
            "code": "NOT_FOUND",
            "message": "Not Found",
            "request_id": "request-not-found-test",
        }
    }
    clear_overrides()


def test_unhandled_exception_hides_internal_detail_and_keeps_request_id(monkeypatch):
    """未捕获异常不得泄漏内部文本，运维仍可通过 request_id 查找日志。"""

    client, _ = client_with_database()

    def fail_health(_settings):
        """模拟健康检查内部失败，异常内容不得进入 HTTP 响应。"""

        raise RuntimeError("secret internal path /srv/private")

    monkeypatch.setattr("app.main.graph_health", fail_health)
    response = client.get(
        "/api/health",
        headers={"X-Request-ID": "request-internal-test"},
    )

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "request-internal-test"
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "服务器内部错误，请稍后重试。",
            "request_id": "request-internal-test",
        }
    }
    assert "secret" not in response.text
    clear_overrides()
