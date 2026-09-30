"""定向 OCR MCP 领取只暴露冻结的两张区域图像。"""

import asyncio
import hashlib
import json
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink

from file_agent_mcp.scoped_pdf_tools import register_scoped_pdf_tools


def test_claim_downloads_only_two_scoped_regions(tmp_path):
    task_id = str(uuid4())
    calls = []
    images = {"cover_identity": b"\x89PNG\r\n\x1a\ncover",
              "basic_profile": b"\x89PNG\r\n\x1a\nbasic"}

    class FakeHTTP:
        async def post(self, path, json):
            calls.append(("POST", path))
            body = {"task_id": task_id, "lease_token": "x" * 32,
                    "source_sha256": "a" * 64, "scope_digest": "b" * 64,
                    "regions": [{"region_id": "cover_identity", "page_number": 1,
                                 "bbox": [0.3, 0.4, 0.75, 0.5]},
                                {"region_id": "basic_profile", "page_number": 3,
                                 "bbox": [0.12, 0.12, 0.88, 0.56]}]}
            return httpx.Response(200, json=body, request=httpx.Request("POST", "http://test" + path))

        async def get(self, path, *, params=None, headers=None):
            calls.append(("GET", path))
            region = path.rsplit("/", 1)[-1]
            image = images[region]
            return httpx.Response(200, content=image, headers={"X-Region-SHA256": hashlib.sha256(image).hexdigest()},
                                  request=httpx.Request("GET", "http://test" + path))

    class FakeClient:
        def __init__(self):
            self.http = FakeHTTP()

        @staticmethod
        def _business_json(response):
            return response.json()

        async def close(self):
            pass

    async def scenario():
        mcp = FastMCP("scoped-pdf-test")
        register_scoped_pdf_tools(mcp, FakeClient, lambda: tmp_path)
        result = await mcp.call_tool("scoped_pdf_source_claim", {"task_id": task_id, "worker_id": "ocr-worker"})
        assert isinstance(result, CallToolResult)
        assert len([part for part in result.content if isinstance(part, ResourceLink)]) == 2
        assert [region["page_number"] for region in result.structuredContent["regions"]] == [1, 3]
        assert all(region["resource_uri"].startswith("file:") for region in result.structuredContent["regions"])
        assert json.loads(result.content[0].text)["downloaded"] is True
        assert calls == [("POST", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/claim"),
                         ("GET", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/regions/cover_identity"),
                         ("GET", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/regions/basic_profile")]

    asyncio.run(scenario())
