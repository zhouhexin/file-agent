"""WorkBuddy 文档任务 MCP 的发现、下载、续租和强类型回传测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink

from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry
from file_agent_mcp.document_extraction_tools import register_document_extraction_tools


TASK_ID = "11111111-1111-4111-8111-111111111111"
VERSION_ID = "22222222-2222-4222-8222-222222222222"


def test_document_tools_are_additive_and_strict(tmp_path) -> None:
    """文档工具独立注册，preview/submit 的嵌套对象拒绝未知字段。"""

    mcp = FastMCP("document-extraction-test")
    register_document_extraction_tools(mcp, lambda: None, lambda: tmp_path)
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "document_extraction_get", "document_extraction_resume", "document_extraction_claim",
        "document_extraction_renew", "document_extraction_preview",
        "document_extraction_confirm", "document_extraction_submit",
        "document_extraction_submit_cached",
    }
    schemas = {tool.name: tool.inputSchema for tool in tools}
    submit = schemas["document_extraction_submit"]
    assert submit["$defs"]["ExtractionUnit"]["additionalProperties"] is False
    assert submit["$defs"]["ExtractionBlock"]["additionalProperties"] is False
    assert schemas["document_extraction_preview"]["$defs"]["ExtractionUnit"]["additionalProperties"] is False
    claim = next(tool for tool in tools if tool.name == "document_extraction_claim")
    assert claim.outputSchema is not None
    assert set(claim.outputSchema["required"]) >= {
        "lease_token", "source_version_id", "source_sha256", "source",
        "extraction_save_policy", "downloaded",
    }
    assert set(claim.outputSchema["$defs"]["DocumentExtractionClaimSource"]["required"]) >= {
        "resource_url", "resource_uri",
    }


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
        async def document_extraction_renew(self, **kwargs):
            return {"lease_token": kwargs["lease_token"]}

        async def document_extraction_claim(self, **kwargs):
            return {"task_id": TASK_ID, "provider_contract_version": "workbuddy-document-v1",
                    "lease_token": "lease", "lease_expires_at": "2026-09-20T12:00:00Z",
                    "source_sha256": "a" * 64, "source_version_id": VERSION_ID,
                    "extraction_save_policy": "CONFIRM_BEFORE_SAVE", "confirmation_required": True,
                    "confirmation": {}, "limits": {},
                    "source": {"filename": "a.docx", "resource_uri": "file:///tmp/a.docx",
                    "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    "size_bytes": 10, "source_kind": "WORD", "page_count": None,
                    "resource_url": f"/api/integrations/v1/document-extraction-tasks/{TASK_ID}/source",
                    "sha256": "a" * 64}}

        async def document_extraction_submit(self, **kwargs):
            calls.append(("submit", kwargs))
            return {"accepted": True, "ok": True}

        async def document_extraction_prepare_confirmation(self, **kwargs):
            calls.append(("preview", kwargs))
            return {"id": "confirmation-one", "result_digest": kwargs["payload"]["result_digest"],
                    "status": "WAITING_CONFIRMATION"}

        async def document_extraction_confirm(self, **kwargs):
            calls.append(("confirm", kwargs))
            return {"id": kwargs["confirmation_id"], "result_digest": kwargs["result_digest"],
                    "status": "CONFIRMED"}

        async def close(self):
            calls.append("closed")

    async def scenario() -> None:
        mcp = FastMCP("document-extraction-test")
        register_document_extraction_tools(mcp, FakeClient, lambda: tmp_path)
        # 走 FastMCP 对外调用路径，以便真正执行 outputSchema 校验；直接调用
        # _tool_manager 且 convert_result=False 会漏掉本次线上错误。
        claimed = await mcp.call_tool("document_extraction_claim", {"task_id": TASK_ID, "worker_id": "worker"})
        assert isinstance(claimed, CallToolResult)
        assert any(isinstance(item, ResourceLink) for item in claimed.content)
        assert claimed.structuredContent["lease_token"] == "lease"
        assert claimed.structuredContent["source_version_id"] == VERSION_ID
        assert claimed.structuredContent["source_sha256"] == "a" * 64
        assert claimed.structuredContent["source"]["filename"] == "a.docx"
        assert claimed.structuredContent["source"]["resource_url"].endswith(f"/{TASK_ID}/source")
        assert claimed.structuredContent["source"]["resource_uri"] == "file:///tmp/a.docx"
        assert claimed.structuredContent["downloaded"] is True
        text_payload = json.loads(next(item.text for item in claimed.content if item.type == "text"))
        assert text_payload == claimed.structuredContent
        assert text_payload["lease_token"] == "lease"
        assert text_payload["source"]["resource_uri"] == "file:///tmp/a.docx"
        preview_result = await mcp._tool_manager.call_tool("document_extraction_preview", {
            "task_id": TASK_ID, "worker_id": "worker", "lease_token": "x" * 20,
            "preview_key": "preview-one", "source_sha256": "a" * 64,
            "source_version_id": VERSION_ID, "provider_name": "workbuddy-docx-reader",
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "page_number": None,
                       "order": 0, "status": "READ", "text": "正文", "blocks": []}],
        })
        preview_call = next(
            item[1] for item in calls
            if isinstance(item, tuple) and item[0] == "preview"
        )
        assert "units" not in preview_call["payload"]
        assert "正文" not in str(preview_call["payload"])
        assert preview_result["display_required"] is True
        assert preview_result["preview_result"]["units"][0]["text"] == "正文"
        assert "正文" in str(preview_result)
        await mcp.call_tool("document_extraction_confirm", {
            "task_id": TASK_ID, "confirmation_id": "33333333-3333-4333-8333-333333333333",
            "result_digest": preview_call["payload"]["result_digest"],
        })
        await mcp.call_tool("document_extraction_submit", {
            "task_id": TASK_ID, "worker_id": "worker", "lease_token": "x" * 20,
            "submission_key": "submit-one", "source_sha256": "a" * 64,
            "source_version_id": VERSION_ID, "provider_name": "workbuddy-docx-reader",
            "coverage": {"status": "COMPLETE", "unit_count": 1, "warnings": []},
            "units": [{"unit_id": "section-0", "kind": "TEXT_SECTION", "page_number": None,
                       "order": 0, "status": "READ", "text": "正文", "blocks": []}],
        })
        payload = next(
            item[1] for item in calls
            if isinstance(item, tuple) and item[0] == "submit"
        )["payload"]
        assert payload["provider_name"] == "workbuddy-docx-reader"
        assert payload["units"][0]["page_number"] is None

    asyncio.run(scenario())


def test_spreadsheet_units_require_sheet_and_cell_coordinates(tmp_path) -> None:
    """旧版 XLS 与 XLSX 都通过显式 SHEET/TABLE_CELL 契约返回真实工作表坐标。"""

    mcp = FastMCP("document-extraction-spreadsheet-test")
    register_document_extraction_tools(mcp, lambda: None, lambda: tmp_path)
    schema = {
        tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())
    }["document_extraction_submit"]
    assert "SHEET" in schema["$defs"]["ExtractionUnit"]["properties"]["kind"]["enum"]
    valid = {
        "unit_id": "sheet-0",
        "kind": "SHEET",
        "sheet_name": "业绩汇总",
        "page_number": None,
        "order": 0,
        "status": "READ",
        "text": "姓名\t金额",
        "blocks": [{
            "block_id": "cell-a1",
            "kind": "TABLE_CELL",
            "text": "姓名",
            "start": 0,
            "end": 2,
            "row_index": 0,
            "column_index": 0,
        }],
    }
    assert valid["sheet_name"] == "业绩汇总"

    from file_agent_mcp.document_extraction_tools import ExtractionUnit

    ExtractionUnit.model_validate(valid)
    with pytest.raises(ValueError, match="sheet_name"):
        ExtractionUnit.model_validate({**valid, "sheet_name": None})
    with pytest.raises(ValueError, match="行列索引"):
        ExtractionUnit.model_validate({
            **valid,
            "blocks": [{
                "block_id": "cell-a1",
                "kind": "TABLE_CELL",
                "text": "姓名",
                "start": 0,
                "end": 2,
            }],
        })
