"""新增共享累计表格 MCP 的严格契约及公共下载复用测试。"""

import asyncio

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink

from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry
from file_agent_mcp.dataset_tools import register_dataset_tools


def test_demo_tools_are_additive_and_reject_extra_fields(tmp_path):
    """显式注册独立工具，字段子对象禁止自由 Prompt 或路径。"""
    mcp = FastMCP("dataset-test")
    register_dataset_tools(mcp, lambda: None, lambda: tmp_path)
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {
        "structured_dataset_create", "structured_dataset_list", "structured_dataset_get",
        "structured_dataset_source", "structured_dataset_append", "structured_dataset_query",
        "structured_dataset_export",
    }
    schemas = {t.name: t.inputSchema for t in tools}
    assert schemas["structured_dataset_append"]["$defs"]["DatasetValue"]["additionalProperties"] is False
    assert schemas["structured_dataset_create"]["$defs"]["DatasetField"]["additionalProperties"] is False
    assert "owner_user_id" not in schemas["structured_dataset_create"]["properties"]


def test_export_download_uses_authenticated_atomic_cache(tmp_path):
    """累计 Excel 复用工作副本下载器，认证不进入下载链接。"""
    async def scenario():
        """MockTransport 不访问真实网络。"""
        requests = []
        def handler(request):
            """返回固定受控导出数据。"""
            requests.append(request)
            assert request.headers["authorization"] == "Bearer test-token"
            if request.method == "POST":
                return httpx.Response(200, json={"export_id": "export-1", "revision": 1})
            assert request.url.path == "/api/structured-datasets/dataset-1/exports/export-1/content"
            return httpx.Response(200, content=b"xlsx-demo-bytes", headers={
                "Content-Disposition": 'attachment; filename="dataset-r1.xlsx"',
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            })
        client = FileAgentIntegrationClient(base_url="http://file-agent.test", access_token="test-token",
            roots=LocalRootRegistry({}), transport=httpx.MockTransport(handler))
        try:
            result = await client.dataset_request("POST", "/dataset-1/exports")
            resource = await client.download_dataset_export(dataset_id="dataset-1", export_id=result["export_id"], output_dir=tmp_path)
            assert resource["filename"] == "dataset-r1.xlsx"
            assert resource["size_bytes"] == len(b"xlsx-demo-bytes")
            assert len(list(tmp_path.rglob("*.xlsx"))) == 1
            assert not list(tmp_path.rglob("*.part"))
            assert len(requests) == 2
        finally:
            await client.close()
    asyncio.run(scenario())


def test_tools_forward_fixed_schema_and_close_client(tmp_path):
    """真实 FastMCP 调用把 source 原样映射给后端，未知参数在执行前拒绝。"""
    calls = []
    class FakeClient:
        """只记录 HTTP 业务调用，禁止工具直接存储字段。"""
        async def dataset_request(self, method, path, payload=None):
            """记录固定路径和业务载荷。"""
            calls.append((method, path, payload))
            return {"dataset_id": "demo", "revision": 1}
        async def close(self):
            """连接释放也进入测试轨迹。"""
            calls.append("closed")
    async def scenario():
        """校验嵌套 Pydantic 输入经 MCP 正常解析。"""
        mcp = FastMCP("dataset-test")
        register_dataset_tools(mcp, FakeClient, lambda: tmp_path)
        await mcp.call_tool("structured_dataset_create", {
            "name": "累计表格", "fields": [{"key": "name", "label": "姓名", "field_type": "string"}],
            "idempotency_key": "request-1",
        })
        assert calls[0][0:2] == ("POST", "")
        assert calls[0][2]["fields"][0]["key"] == "name"
        assert calls[-1] == "closed"
    asyncio.run(scenario())


def test_export_tool_delivers_resource_link(tmp_path):
    """导出应交付宿主可识别的 ResourceLink，不只给需要认证的 API URL。"""
    class FakeClient:
        """隔离后端与本机写入，只验证真实工具包装。"""
        async def dataset_request(self, *args):
            """返回已发布快照。"""
            return {"export_id": "export-one", "revision": 1}
        async def download_dataset_export(self, **kwargs):
            """返回固定缓存资源。"""
            return {"filename": "table.xlsx", "resource_uri": "file:///tmp/table.xlsx",
                    "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "size_bytes": 10}
        async def close(self):
            """释放模拟客户端。"""
    async def scenario():
        """执行注册后的 Tool handler，验证 MCP 内容类型。"""
        mcp = FastMCP("export-test")
        register_dataset_tools(mcp, FakeClient, lambda: tmp_path)
        result = await mcp._tool_manager.call_tool("structured_dataset_export", {"dataset_id": "11111111-1111-4111-8111-111111111111"})
        assert isinstance(result, CallToolResult)
        assert any(isinstance(item, ResourceLink) for item in result.content)
        assert "file:///" not in str(result.structuredContent)
        assert result.structuredContent["downloaded"]
    asyncio.run(scenario())
