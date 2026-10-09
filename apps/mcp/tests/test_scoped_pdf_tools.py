"""定向 OCR MCP 只暴露冻结区域与服务端逐次签发的单页定位图。"""

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


def test_reverse_probe_downloads_only_server_issued_page_and_submits_result(tmp_path):
    """定位工具只给模型一张服务端签发的页图，提交不接受自造图像。"""
    task_id = str(uuid4())
    page_image = b"\x89PNG\r\n\x1a\nreview-page"
    page_sha = hashlib.sha256(page_image).hexdigest()
    calls = []

    class FakeHTTP:
        async def get(self, path, *, params=None, headers=None):
            calls.append(("GET", path))
            if path.endswith("/review-probe/image"):
                return httpx.Response(200, content=page_image,
                    headers={"X-Region-SHA256": page_sha},
                    request=httpx.Request("GET", "http://test" + path))
            return httpx.Response(200, json={"task_id": task_id, "page_number": 24,
                "image_sha256": page_sha, "review_discovery": {"status": "DISCOVERING"}},
                request=httpx.Request("GET", "http://test" + path))

        async def post(self, path, json=None):
            calls.append(("POST", path))
            if path.endswith("/fixed25-upgrade"):
                return httpx.Response(200, json={"task_id": task_id,
                    "provider_contract_version": "workbuddy-scoped-pdf-v4",
                    "review_discovery": {"status": "FOUND", "selected_page": 25}},
                    request=httpx.Request("POST", "http://test" + path))
            if path.endswith("/review-discovery-upgrade"):
                return httpx.Response(200, json={"task_id": task_id,
                    "provider_contract_version": "workbuddy-scoped-pdf-v3"},
                    request=httpx.Request("POST", "http://test" + path))
            if path.endswith("/review-region/reject"):
                return httpx.Response(200, json={"task_id": task_id,
                    "review_discovery": {"status": "DISCOVERING", "next_page": 23}},
                    request=httpx.Request("POST", "http://test" + path))
            return httpx.Response(200, json={"task_id": task_id,
                "review_discovery": {"status": "FOUND", "selected_page": 24}},
                request=httpx.Request("POST", "http://test" + path))

    class FakeClient:
        def __init__(self):
            self.http = FakeHTTP()

        @staticmethod
        def _business_json(response):
            return response.json()

        async def close(self):
            pass

    async def scenario():
        mcp = FastMCP("scoped-pdf-review-test")
        register_scoped_pdf_tools(mcp, FakeClient, lambda: tmp_path)
        result = await mcp.call_tool("scoped_pdf_review_probe", {
            "task_id": task_id, "worker_id": "ocr-worker", "lease_token": "x" * 32})
        assert isinstance(result, CallToolResult)
        assert result.structuredContent["page_number"] == 24
        assert len([part for part in result.content if isinstance(part, ResourceLink)]) == 1
        assert calls == [("GET", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-probe"),
                         ("GET", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-probe/image")]
        submitted = await mcp.call_tool("scoped_pdf_review_probe_submit", {
            "task_id": task_id, "payload": {"worker_id": "ocr-worker", "lease_token": "x" * 32,
                "submission_key": "probe-24", "page_number": 24, "image_sha256": page_sha,
                "text": "任现职以来的考核情况", "provider_name": "real-ocr",
                "section_confirmed": True, "heading_bbox": [0.2, 0.4, 0.65, 0.44]}})
        assert submitted[1]["review_discovery"]["selected_page"] == 24
        assert calls[-1] == ("POST", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-probe/results")
        upgraded = await mcp.call_tool("scoped_pdf_review_discovery_upgrade", {"task_id": task_id})
        assert upgraded[1]["provider_contract_version"] == "workbuddy-scoped-pdf-v3"
        assert calls[-1] == ("POST", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-discovery-upgrade")
        fixed = await mcp.call_tool("scoped_pdf_fixed25_upgrade", {"task_id": task_id})
        assert fixed[1]["provider_contract_version"] == "workbuddy-scoped-pdf-v4"
        assert fixed[1]["review_discovery"]["selected_page"] == 25
        assert calls[-1] == ("POST", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/fixed25-upgrade")
        rejected = await mcp.call_tool("scoped_pdf_review_region_reject", {"task_id": task_id,
            "payload": {"worker_id": "ocr-worker", "lease_token": "x" * 32,
                "submission_key": "reject-24", "image_sha256": page_sha,
                "reason": "HEADING_ABSENT"}})
        assert rejected[1]["review_discovery"]["next_page"] == 23
        assert calls[-1] == ("POST", f"/api/integrations/v1/scoped-pdf-sources/{task_id}/review-region/reject")

    asyncio.run(scenario())
