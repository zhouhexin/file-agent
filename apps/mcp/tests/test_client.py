"""MCP 客户端的本地路径边界和后端调用契约测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry


def test_local_root_registry_rejects_traversal_and_symlink_escape(tmp_path) -> None:
    """工具参数不能借上级路径或符号链接读取授权目录之外的文件。"""

    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (root / "escape.txt").symlink_to(outside)
    registry = LocalRootRegistry({"materials": root})

    with pytest.raises(ValueError):
        registry.resolve_file(source_root_ref="materials", source_relative_path="../secret.txt")
    with pytest.raises(ValueError):
        registry.resolve_file(source_root_ref="materials", source_relative_path="escape.txt")
    with pytest.raises(ValueError):
        registry.resolve_file(source_root_ref="unknown", source_relative_path="file.txt")


def test_file_ingest_streams_authorized_file_without_exposing_local_path(tmp_path) -> None:
    """file_ingest 应发送 multipart 字节和 Bearer 认证，但业务响应不含本地绝对路径。"""

    root = tmp_path / "allowed"
    source = root / "待导入" / "材料.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"hello file agent")

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟后端并检查路径、认证和 multipart 载荷。"""

        assert request.url.path == "/api/integrations/v1/ingest-batches/batch-1/items/item-1/content"
        assert request.headers["authorization"] == "Bearer token-value"
        body = await request.aread()
        assert b"hello file agent" in body
        assert str(tmp_path).encode() not in body
        return httpx.Response(
            202,
            json={"accepted": True, "filesystem_job_id": "job-1"},
        )

    async def scenario() -> dict:
        """在独立事件循环中执行一次上传并关闭连接池。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="token-value",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.file_ingest(
                batch_id="batch-1",
                item_id="item-1",
                source_root_ref="materials",
                source_relative_path="待导入/材料.txt",
            )
        finally:
            await client.close()

    result = asyncio.run(scenario())
    assert result == {"accepted": True, "filesystem_job_id": "job-1"}


def test_job_get_returns_structured_error_without_leaking_token(tmp_path) -> None:
    """后端错误只转成业务错误码和消息，不得包含 Authorization 令牌。"""

    root = tmp_path / "allowed"
    root.mkdir()

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟无权查看任务的统一错误 Envelope。"""

        assert request.url.path == "/api/jobs/missing"
        return httpx.Response(
            404,
            content=json.dumps(
                {"error": {"code": "NOT_FOUND", "message": "任务不存在"}},
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    async def scenario() -> None:
        """在独立事件循环中验证错误投影并关闭连接池。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="super-secret-token",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(RuntimeError, match="NOT_FOUND: 任务不存在") as caught:
                await client.job_get(job_id="missing")
            assert "super-secret-token" not in str(caught.value)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_duplicate_decide_forwards_fixed_review_candidate_and_revision(tmp_path) -> None:
    """MCP 必须原样转发结构化选择，不能只发送一段自然语言决定。"""

    root = tmp_path / "allowed"
    root.mkdir()

    async def handler(request: httpx.Request) -> httpx.Response:
        """验证决定请求包含固定候选和修订。"""

        assert request.url.path == "/api/integrations/v1/ingest-items/item-1/duplicate-decision"
        payload = json.loads((await request.aread()).decode("utf-8"))
        assert payload["review_id"] == "review-1"
        assert payload["review_revision"] == 4
        assert payload["group_revision"] is None
        assert payload["group_member_item_ids"] == []
        assert payload["candidate_id"] == "candidate-2"
        assert payload["decision"] == "USE_EXISTING_FILE"
        return httpx.Response(202, json={"reused": False, "item": {"id": "item-1"}})

    async def scenario() -> dict:
        """执行一次结构化决定请求。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="token-value",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.duplicate_decide(
                item_id="item-1",
                review_id="review-1",
                review_revision=4,
                group_revision=None,
                group_member_item_ids=[],
                candidate_id="candidate-2",
                decision="USE_EXISTING_FILE",
                request_id="request-1",
                idempotency_key="decision-1",
            )
        finally:
            await client.close()

    assert asyncio.run(scenario())["reused"] is False


def test_batch_snapshot_restores_all_pending_duplicate_reviews(tmp_path) -> None:
    """刷新后必须从后端逐项恢复所有待确认卡，而非依赖聊天内存。"""

    root = tmp_path / "allowed"
    root.mkdir()

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟一个含两个待确认项和一个成功项的批次。"""

        if request.url.path.endswith("/ingest-batches/batch-1"):
            return httpx.Response(200, json={"id": "batch-1", "status": "PARTIAL"})
        if request.url.path.endswith("/ingest-batches/batch-1/items"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "item-1", "status": "WAITING_DUPLICATE_CONFIRMATION"},
                        {"id": "item-2", "status": "SUCCEEDED"},
                        {"id": "item-3", "status": "WAITING_DUPLICATE_CONFIRMATION"},
                    ],
                    "next_cursor": None,
                },
            )
        item_id = request.url.path.split("/")[-2]
        return httpx.Response(200, json={"item_id": item_id, "review_id": f"review-{item_id}"})

    async def scenario() -> dict:
        """执行一次完整恢复查询。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="token-value",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.batch_snapshot(batch_id="batch-1")
        finally:
            await client.close()

    snapshot = asyncio.run(scenario())
    assert [item["item_id"] for item in snapshot["pending_duplicate_reviews"]] == ["item-1", "item-3"]


def test_extraction_claim_downloads_real_pages_to_controlled_directory(tmp_path) -> None:
    """领取任务必须真实下载页面，不能只向 WorkBuddy 返回一个无法读取的 source_ref。"""

    root = tmp_path / "allowed"
    root.mkdir()

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟领取响应和受租约保护的页面资源。"""

        if request.url.path.endswith("/claim"):
            return httpx.Response(
                200,
                json={
                    "task_id": "task-1",
                    "source_sha256": "a" * 64,
                    "source_version_id": "version-1",
                    "provider_contract_version": "workbuddy-ocr-v1",
                    "lease_token": "lease-token-value-with-length",
                    "lease_expires_at": "2026-09-08T10:00:00Z",
                    "pages": [
                        {
                            "page_number": 1,
                            "content_type": "image/png",
                            "resource_url": "/api/integrations/v1/extraction-tasks/task-1/pages/1",
                        }
                    ],
                },
            )
        assert request.headers["x-extraction-lease-token"] == "lease-token-value-with-length"
        assert request.url.params["worker_id"] == "worker-1"
        return httpx.Response(200, content=b"real-page-bytes", headers={"content-type": "image/png"})

    async def scenario() -> dict:
        """执行领取并关闭连接。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="token-value",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.extraction_claim(
                task_id="task-1",
                worker_id="worker-1",
                output_dir=tmp_path / "pages",
            )
        finally:
            await client.close()

    claim = asyncio.run(scenario())
    page_path = Path(claim["pages"][0]["local_path"])
    assert page_path.read_bytes() == b"real-page-bytes"
    assert str(page_path).startswith(str(tmp_path / "pages"))


def test_ingest_item_action_only_calls_fixed_retry_or_cancel_endpoint(tmp_path) -> None:
    """MCP 条目动作必须使用固定白名单路径并携带幂等标识。"""

    root = tmp_path / "allowed"
    root.mkdir()

    async def handler(request: httpx.Request) -> httpx.Response:
        """验证显式重试请求不携带本地路径或任意动作名。"""

        assert request.url.path == "/api/integrations/v1/ingest-items/item-1/retry"
        payload = json.loads((await request.aread()).decode("utf-8"))
        assert payload == {
            "client_id": "workbuddy-local",
            "request_id": "retry-request",
            "idempotency_key": "retry-event",
            "reason": "用户确认重试",
        }
        return httpx.Response(202, json={"accepted": True, "reused": False})

    async def scenario() -> dict:
        """调用受控动作并验证非法动作在发请求前失败。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="token-value",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(ValueError, match="不支持"):
                await client.ingest_item_action(
                    item_id="item-1",
                    action="delete",
                    request_id="bad",
                    idempotency_key="bad",
                )
            return await client.ingest_item_action(
                item_id="item-1",
                action="retry",
                request_id="retry-request",
                idempotency_key="retry-event",
                reason="用户确认重试",
            )
        finally:
            await client.close()

    assert asyncio.run(scenario()) == {"accepted": True, "reused": False}


def test_conversation_tools_use_read_only_and_controlled_backend_endpoints(tmp_path) -> None:
    """搜索、证据回答和计划确认必须调用各自受控端点，不能由 MCP 直接处理正文或文件。"""

    root = tmp_path / "allowed-conversation"
    root.mkdir()
    requests: list[tuple[str, str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录安全业务请求并返回最小结构化结果。"""

        payload = json.loads((await request.aread()).decode("utf-8")) if request.content else {}
        requests.append((request.method, request.url.path, payload))
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"files": [], "total_returned": 0})
        if request.url.path.endswith("/evidence-answer"):
            return httpx.Response(200, json={"task_result": {"response_type": "evidence_answer"}})
        if request.url.path.endswith("/confirm"):
            return httpx.Response(200, json={"id": "plan-1", "status": "EXECUTED"})
        return httpx.Response(500, json={"error": {"code": "UNEXPECTED", "message": "unexpected"}})

    async def scenario() -> None:
        """依次调用三个外部能力。"""

        client = FileAgentIntegrationClient(
            base_url="http://file-agent.test",
            access_token="token-value",
            roots=LocalRootRegistry({"materials": root}),
            transport=httpx.MockTransport(handler),
        )
        try:
            await client.file_search(conversation_id="wb-conversation", query="奖学金")
            await client.evidence_answer(
                conversation_id="wb-conversation",
                question="截止日期是什么？",
                document_ids=["doc-1"],
            )
            await client.operation_plan_confirm(
                plan_id="plan-1",
                confirmation="确认执行",
            )
        finally:
            await client.close()

    asyncio.run(scenario())
    assert requests == [
        (
            "POST",
            "/api/search",
            {
                "query": "奖学金",
                "conversation_id": "wb-conversation",
                "attachment_document_ids": [],
                "top_k": 10,
            },
        ),
        (
            "POST",
            "/api/conversations/wb-conversation/evidence-answer",
            {"question": "截止日期是什么？", "attachment_document_ids": ["doc-1"]},
        ),
        (
            "POST",
            "/api/operations/plans/plan-1/confirm",
            {"confirmation": "确认执行"},
        ),
    ]
