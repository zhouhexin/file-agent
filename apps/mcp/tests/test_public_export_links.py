"""MCP 发布回执的公开链接签发和失败降级，不访问真实服务。"""

import asyncio

import httpx
import pytest

from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry


@pytest.mark.parametrize("route", ["dataset", "manual", "funding"])
@pytest.mark.parametrize("links_ok", [True, False])
def test_published_export_links_are_added_without_hiding_publication_success(route, links_ok):
    """三类发布统一交付只读链接，旧后端不支持签发时仍保留已发布回执。"""
    receipt = {"status": "COMPLETED", "excel_published": True, "dataset_id": "dataset",
               "export_id": "export", "download_url": "/api/structured-datasets/dataset/exports/export/content"}

    def handler(request):
        if request.url.path.endswith("browser-links"):
            assert request.headers["authorization"] == "Bearer test-token"
            return httpx.Response(200, json={
                "preview_url": "/api/structured-datasets/public-exports/signed/preview",
                "download_url": "/api/structured-datasets/public-exports/signed/download",
                "expires_in_seconds": 86400}) if links_ok else httpx.Response(404, json={"detail": "Not found"})
        return httpx.Response(200, json=receipt)

    async def run():
        client = FileAgentIntegrationClient(base_url="http://file-agent.test", access_token="test-token",
            roots=LocalRootRegistry({}), transport=httpx.MockTransport(handler))
        try:
            if route == "dataset":
                result = await client.dataset_request("POST", "/dataset/exports", {})
            elif route == "manual":
                result = await client.excel_writeback_request("POST", "/manual-revisions/publish", {})
            else:
                result = await client.funding_usage_request("POST", "/prompt/add-people", {})
            assert result["status"] == "COMPLETED" and result["excel_published"]
            if links_ok:
                assert result["download_url"] == "http://file-agent.test/api/structured-datasets/public-exports/signed/download"
                assert result["preview_url"].endswith("/signed/preview")
                assert result["authenticated_download_url"].endswith("/export/content")
                assert result["public_link_expires_in_seconds"] == 86400
            else:
                assert result == receipt
        finally:
            await client.close()

    asyncio.run(run())
