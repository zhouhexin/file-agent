"""WorkBuddy 文档任务 MCP 的发现、下载、续租和强类型回传测试。"""

from __future__ import annotations

import asyncio
import hashlib

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink

from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry
from file_agent_mcp.document_extraction_tools import register_document_extraction_tools


TASK_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"


def test_document_tools_are_additive_and_strict(tmp_path) -> None:
    """四个工具独立注册，submit 的嵌套对象拒绝未知字段。"""

    mcp = FastMCP("document-extraction-test")
    register_document_extraction_tools(mcp, lambda: None, lambda: tmp_path)
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "document_extraction_get", "document_extraction_claim",
        "document_extraction_renew", "document_extraction_submit",
    }
    schemas = {tool.name: tool.inputSchema for tool in tools}
    submit = schemas["document_extraction_submit"]
    assert submit["$defs"]["ExtractionUnit"]["additionalProperties"] is False
    assert submit["$defs"]["ExtractionBlock"]["additionalProperties"] is False


def test_claim_downloads_and_verifies_fixed_document(tmp_path) -> None:
    """claim 必须完成认证下载、大小和 SHA-256 校验后才返回资源。"""

    content = b"fixed-docx-content"
    sha256 = hashlib.sha256(content).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={
                "task_id": TASK_ID, "provider_contract_version": "workbuddy-document-v1",
                "source_version_id": VERSION_ID, "source_sha256": sha256,
                "lease_token": "lease-token-value-123456789", "lease_expires_at": "2026-09-20T12:00:00Z",
                "source": {"filename": "登记.docx", "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           "size_bytes": len(content), "source_kind": "WORD", "page_count": None,
                           "resource_url": f"/api/integrations/v1/document-extraction-tasks/{TASK_ID}/source"},
                "limits": {"max_text_chars": 100000, "max_units": 500, "max_blocks": 2000, "max_payload_bytes": 4194304},
            })
        assert request.headers["x-extraction-lease-token"] == "lease-token-value-123456789"
        assert request.url.params["worker_id"] == "worker-one"
        return httpx.Response(200, content=content, headers={
            "Content-Length": str(len(content)),
            "Content-Disposition": 'attachment; filename="register.docx"',
            "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        })

    async def scenario() -> None:
        client = FileAgentIntegrationClient(base_url="http://file-agent.test", access_token="token",
            roots=LocalRootRegistry({}), transport=httpx.MockTransport(handler))
        try:
            result = await client.document_extraction_claim(
                task_id=TASK_ID, worker_id="worker-one", output_dir=tmp_path
            )
            assert result["source"]["sha256"] == sha256
            assert result["source"]["size_bytes"] == len(content)
            assert len(list(tmp_path.rglob("register.docx"))) == 1
            assert not list(tmp_path.rglob("*.part"))
        finally:
            await client.close()

    asyncio.run(scenario())


def test_claim_tool_returns_resource_and_submit_forwards_schema(tmp_path) -> None:
    """宿主获得 ResourceLink，回传载荷保持固定版本和真实 provider。"""

    calls: list[object] = []

    class FakeClient:
        async def document_extraction_claim(self, **kwargs):
            return {"task_id": TASK_ID, "lease_token": "lease", "source_sha256": "a" * 64,
                    "source_version_id": VERSION_ID, "source": {"filename": "a.docx", "resource_uri": "file:///tmp/a.docx",
                    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "size_bytes": 10, "sha256": "a" * 64}}

        async def document_extraction_submit(self, **kwargs):
            calls.append(kwargs)
            return {"accepted": True, "ok": True}

        async def close(self):
            calls.append("closed")

    async def scenario() -> None:
        mcp = FastMCP("document-extraction-test")
        register_document_extraction_tools(mcp, FakeClient, lambda: tmp_path)
        claimed = await mcp._tool_manager.call_tool("document_extraction_claim", {"task_id": TASK_ID, "worker_id": "worker"})
        assert isinstance(claimed, CallToolResult)
        assert any(isinstance(item, ResourceLink) for item in claimed.content)
        await mcp.call_tool("document_extraction_submit", {
            "task_id": TASK_ID, "worker_id": "worker", "lease_token": "x" * 20,
            "submission_key": "submit-one", "source_sha256": "a" * 64,
            "source_version_id": VERSION_ID, "provider_name": "workbuddy-docx-reader",
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "page_number": None,
                       "order": 0, "status": "READ", "text": "正文", "blocks": []}],
        })
        payload = next(item for item in calls if isinstance(item, dict))["payload"]
        assert payload["provider_name"] == "workbuddy-docx-reader"
        assert payload["units"][0]["page_number"] is None

    asyncio.run(scenario())
