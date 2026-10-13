"""成果资助 MCP 工具只调用固定预览/发布 API，正式成功后提供下载资源。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink

from file_agent_mcp.funding_usage_tools import register_funding_usage_tools
from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry


def test_funding_usage_tools_route_and_download(tmp_path):
    calls = []

    class FakeClient:
        async def funding_usage_request(self, method, path, payload):
            calls.append((method, path, payload))
            if path in {"/preview", "/continue/preview"}:
                return {"preview_digest": "a" * 64, "person_count": 1}
            return {"status": "COMPLETED", "excel_published": True,
                    "dataset_id": "dataset", "export_id": "export",
                    "filename": "余额.xlsx", "version_number": 1}

        async def download_dataset_export(self, **kwargs):
            calls.append(("download", kwargs))
            return {"resource_uri": (tmp_path / "余额.xlsx").as_uri(), "size_bytes": 123}

        async def close(self):
            pass

    mcp = FastMCP("funding-test")
    register_funding_usage_tools(mcp, FakeClient, lambda: tmp_path)
    source = {"document_id": str(uuid4()), "document_version_id": str(uuid4()),
              "source_sha256": "b" * 64}
    payload = {"image": {**source, "extraction_run_id": str(uuid4())},
               "workbook": source, "filename": "余额.xlsx", "request_key": "one",
               "rows": [{"page_number": 1, "name": "张三",
                         "name_ref": {"quote": "张三", "start": 0, "end": 2},
                         "no_usage": True}]}

    async def run():
        names = {item.name for item in await mcp.list_tools()}
        assert {"funding_usage_preview", "funding_usage_publish",
                "funding_usage_continue_preview", "funding_usage_continue_publish"} <= names
        await mcp.call_tool("funding_usage_preview", {"payload": payload})
        result = await mcp.call_tool("funding_usage_publish", {"payload": {
            **payload, "preview_digest": "a" * 64}})
        assert isinstance(result, CallToolResult)
        assert any(isinstance(item, ResourceLink) for item in result.content)
        continuing = {key: value for key, value in payload.items() if key != "workbook"}
        continuing.update(base_document_version_id=source["document_version_id"],
                          base_sha256=source["source_sha256"], base_revision=0)
        await mcp.call_tool("funding_usage_continue_preview", {"payload": continuing})
        added = await mcp.call_tool("funding_usage_continue_publish", {"payload": {
            **continuing, "preview_digest": "a" * 64}})
        assert any(isinstance(item, ResourceLink) for item in added.content)

    asyncio.run(run())
    assert calls[0][0:2] == ("POST", "/preview")
    assert calls[0][2]["request_key"] == "one"
    assert calls[1][1] == "/publish"
    assert calls[2][0] == "download"
    assert calls[3][1] == "/continue/preview"
    assert calls[4][1] == "/continue/publish"


def test_funding_usage_client_allows_only_fixed_continue_routes():
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": "ok"})

    async def run():
        client = FileAgentIntegrationClient(base_url="http://file-agent.test",
            access_token="test-token", roots=LocalRootRegistry({}),
            transport=httpx.MockTransport(handler))
        try:
            assert (await client.funding_usage_request("POST", "/continue/preview", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/continue/publish", {})) == {"status": "ok"}
            with pytest.raises(ValueError, match="FUNDING_USAGE_ROUTE_INVALID"):
                await client.funding_usage_request("POST", "/other", {})
        finally:
            await client.close()

    asyncio.run(run())
    assert paths == ["/api/funding-usage/continue/preview",
                     "/api/funding-usage/continue/publish"]
