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
            if path in {"/preview", "/continue/preview", "/refresh/preview",
                        "/prompt/target", "/prompt/preview",
                        "/source-summary/preview", "/source-entries/preview"}:
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
                "funding_usage_continue_preview", "funding_usage_continue_publish",
                "funding_usage_refresh_preview", "funding_usage_refresh_publish",
                "funding_usage_prompt_target", "funding_usage_prompt_preview",
                "funding_usage_prompt_publish",
                "funding_source_summary_preview", "funding_source_summary_publish",
                "funding_source_entries_preview", "funding_source_entries_publish"} <= names
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
        refresh = {"workbook": source, "filename": "余额.xlsx", "request_key": "refresh",
                   "base_document_version_id": source["document_version_id"],
                   "base_sha256": source["source_sha256"], "base_revision": 0}
        await mcp.call_tool("funding_usage_refresh_preview", {"payload": refresh})
        refreshed = await mcp.call_tool("funding_usage_refresh_publish", {"payload": {
            **refresh, "preview_digest": "a" * 64}})
        assert any(isinstance(item, ResourceLink) for item in refreshed.content)
        source_sync = {"workbook": source, "request_key": "source-sync"}
        await mcp.call_tool("funding_source_summary_preview", {"payload": source_sync})
        synced = await mcp.call_tool("funding_source_summary_publish", {"payload": {
            **source_sync, "preview_digest": "a" * 64}})
        assert any(isinstance(item, ResourceLink) for item in synced.content)
        entry_import = {"workbook": source, "filename": "教师成果明细汇总表.xlsx",
                        "request_key": "entries", "confirm_inferred_owner": True}
        await mcp.call_tool("funding_source_entries_preview", {"payload": entry_import})
        imported = await mcp.call_tool("funding_source_entries_publish", {"payload": {
            **entry_import, "preview_digest": "a" * 64}})
        assert any(isinstance(item, ResourceLink) for item in imported.content)
        await mcp.call_tool("funding_usage_prompt_target", {"payload": {"filename": "余额.xlsx"}})
        prompt = {"filename": "余额.xlsx", "request_key": "prompt-one",
                  "base_document_version_id": source["document_version_id"],
                  "base_sha256": source["source_sha256"], "base_revision": 0,
                  "user_request": "给张三新增一笔支出10元",
                  "row_edits": [{"action": "ADD", "name": "张三",
                                 "registration": "新增支出10元", "usage_amount": 10}]}
        await mcp.call_tool("funding_usage_prompt_preview", {"payload": prompt})
        prompted = await mcp.call_tool("funding_usage_prompt_publish", {"payload": {
            **prompt, "preview_digest": "a" * 64}})
        assert any(isinstance(item, ResourceLink) for item in prompted.content)
        rename = {**prompt, "request_key": "rename-one",
                  "user_request": "将张三更正为张山", "row_edits": [],
                  "name_edits": [{"old_name": "张三", "new_name": "张山"}]}
        await mcp.call_tool("funding_usage_prompt_preview", {"payload": rename})
        renamed = await mcp.call_tool("funding_usage_prompt_publish", {"payload": {
            **rename, "preview_digest": "a" * 64}})
        assert any(isinstance(item, ResourceLink) for item in renamed.content)

    asyncio.run(run())
    assert calls[0][0:2] == ("POST", "/preview")
    assert calls[0][2]["request_key"] == "one"
    assert calls[1][1] == "/publish"
    assert calls[2][0] == "download"
    assert calls[3][1] == "/continue/preview"
    assert calls[4][1] == "/continue/publish"
    assert calls[6][1] == "/refresh/preview"
    assert calls[7][1] == "/refresh/publish"
    assert calls[9][1] == "/source-summary/preview"
    assert calls[10][1] == "/source-summary/publish"
    assert calls[12][1] == "/source-entries/preview"
    assert calls[13][1] == "/source-entries/publish"
    assert calls[15][1] == "/prompt/target"
    assert calls[16][1] == "/prompt/preview"
    assert calls[17][1] == "/prompt/publish"
    assert calls[19][1] == "/prompt/preview"
    assert calls[19][2]["name_edits"] == [{"old_name": "张三", "new_name": "张山"}]
    assert calls[20][1] == "/prompt/publish"


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
            assert (await client.funding_usage_request("POST", "/refresh/preview", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/refresh/publish", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/prompt/target", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/prompt/preview", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/prompt/publish", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/source-summary/preview", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/source-summary/publish", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/source-entries/preview", {})) == {"status": "ok"}
            assert (await client.funding_usage_request("POST", "/source-entries/publish", {})) == {"status": "ok"}
            with pytest.raises(ValueError, match="FUNDING_USAGE_ROUTE_INVALID"):
                await client.funding_usage_request("POST", "/other", {})
        finally:
            await client.close()

    asyncio.run(run())
    assert paths == ["/api/funding-usage/continue/preview",
                     "/api/funding-usage/continue/publish",
                     "/api/funding-usage/refresh/preview",
                     "/api/funding-usage/refresh/publish",
                     "/api/funding-usage/prompt/target",
                     "/api/funding-usage/prompt/preview",
                     "/api/funding-usage/prompt/publish",
                     "/api/funding-usage/source-summary/preview",
                     "/api/funding-usage/source-summary/publish",
                     "/api/funding-usage/source-entries/preview",
                     "/api/funding-usage/source-entries/publish"]
