"""本地预览缓存的跨会话恢复、租约重试与确认安全回归。"""

import asyncio
from uuid import UUID

import pytest
from mcp.server.fastmcp import FastMCP

from file_agent_mcp.document_extraction_tools import ExtractionUnit, register_document_extraction_tools
from file_agent_mcp.extraction_drafts import ExtractionDraftStore


TASK = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
CONFIRM = "33333333-3333-4333-8333-333333333333"


def test_preview_confirm_cached_after_restart_and_expired_lease(tmp_path):
    """长参数只生成一次；预览不发送正文，确认后缓存提交遇过期仅领取一次。"""
    calls = []

    class Client:
        """确定性模拟真实服务端，不依赖 OCR 或网络。"""
        async def close(self):
            pass

        async def document_extraction_renew(self, **kwargs):
            if kwargs["lease_token"] == "old-token" * 3:
                raise RuntimeError("LEASE_EXPIRED: 过期")
            return {"lease_token": kwargs["lease_token"]}

        async def document_extraction_request(self, *args):
            calls.append(("claim", args))
            return {"source_version_id": VERSION, "source_sha256": "a" * 64, "lease_token": "new-token" * 3}

        async def document_extraction_prepare_confirmation(self, **kwargs):
            calls.append(("preview", kwargs["payload"]))
            return {"id": CONFIRM, "status": "WAITING_CONFIRMATION", "result_digest": kwargs["payload"]["result_digest"]}

        async def document_extraction_confirm(self, **kwargs):
            calls.append(("confirm", kwargs))
            return {"status": "CONFIRMED"}

        async def document_extraction_submit(self, **kwargs):
            assert any(c[0] == "confirm" for c in calls)
            calls.append(("submit", kwargs["payload"]))
            return {"accepted": True, "ok": True, "task": {"extraction_run_id": "run-one"}}

    async def scenario():
        mcp = FastMCP("draft")
        register_document_extraction_tools(mcp, Client, lambda: tmp_path)
        result = await mcp._tool_manager.call_tool("document_extraction_preview", {
            "task_id": TASK, "worker_id": "worker", "lease_token": "old-token" * 3,
            "preview_key": "one", "source_sha256": "a" * 64, "source_version_id": VERSION,
            "provider_name": "actual-provider", "coverage": {"status": "COMPLETE", "unit_count": 1},
            "units": [{"unit_id": "p1", "kind": "PDF_PAGE", "page_number": 1,
                       "order": 0, "status": "READ", "text": "确定正文", "blocks": [
                           {"block_id": "b1", "kind": "TEXT", "text": "正文"}]}]})
        assert result["content_saved"] is False
        assert "units" not in next(c[1] for c in calls if c[0] == "preview")
        restarted = FastMCP("restarted")
        register_document_extraction_tools(restarted, Client, lambda: tmp_path)
        saved = await restarted._tool_manager.call_tool("document_extraction_confirm", {
            "task_id": TASK, "confirmation_id": result["confirmation_id"],
            "result_digest": result["result_digest"], "save_cached_result": True})
        assert saved["content_saved"] is True and saved["excel_published"] is False
        submitted = next(c[1] for c in calls if c[0] == "submit")
        assert submitted["units"][0]["blocks"][0]["start"] == 2
        assert submitted["units"] == result["preview_result"]["units"]
        assert len([c for c in calls if c[0] == "claim"]) == 1
    asyncio.run(scenario())


def test_draft_tampering_and_ambiguous_offsets_fail(tmp_path):
    """缓存引用不能跨任务、遍历路径或将重复文字猜成唯一证据。"""
    store = ExtractionDraftStore(tmp_path)
    digest = store.save(TASK, "worker", "token", {"source_sha256": "a" * 64})
    assert ExtractionDraftStore(tmp_path).load(TASK, digest)["worker_id"] == "worker"
    with pytest.raises(ValueError):
        store.load(str(UUID(int=4)), digest)
    with pytest.raises(ValueError):
        store.load(TASK, "../x")
    with pytest.raises(ValueError, match="唯一定位"):
        ExtractionUnit(unit_id="p1", kind="PDF_PAGE", page_number=1, order=0, status="READ",
                       text="正文正文", blocks=[{"block_id": "b1", "kind": "TEXT", "text": "正文"}])
