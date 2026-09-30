"""新增共享累计表格 MCP 的严格契约及公共下载复用测试。"""

import asyncio
import json

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ResourceLink
from pydantic import ValidationError

from file_agent_mcp.client import FileAgentIntegrationClient, LocalRootRegistry
from file_agent_mcp.dataset_tools import register_dataset_tools
from file_agent_mcp.dataset_task_contracts import TaskTarget, TaskSource


def test_task_manifest_rejects_backend_invalid_bindings_before_http():
    """来源半绑定和目标多选在 MCP 本地失败，避免后端 422。"""
    fixed_id = "66666666-6666-4666-8666-666666666666"
    source = {"source_sha256": "0" * 64, "document_id": fixed_id}
    with pytest.raises(ValidationError, match="document/version/extraction"):
        TaskSource.model_validate(source)
    source["extraction_task_id"] = fixed_id
    with pytest.raises(ValidationError, match="不能混用"):
        TaskSource.model_validate(source)
    target = {"filename": "登记表.xlsx", "labels": ["姓名"], "dataset_id": fixed_id,
              "document_version_id": fixed_id, "uploaded_task_id": fixed_id}
    with pytest.raises(ValidationError, match="只能指定一个目标策略"):
        TaskTarget.model_validate(target)
    target.pop("uploaded_task_id")
    target.pop("document_version_id")
    with pytest.raises(ValidationError, match="必须绑定当前版本"):
        TaskTarget.model_validate(target)


def test_demo_tools_are_additive_and_reject_extra_fields(tmp_path):
    """显式注册独立工具，字段子对象禁止自由 Prompt 或路径。"""
    mcp = FastMCP("dataset-test")
    register_dataset_tools(mcp, lambda: None, lambda: tmp_path)
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == {
        "structured_dataset_create", "structured_dataset_create_from_excel",
        "structured_dataset_resolve", "structured_dataset_list", "structured_dataset_get",
        "structured_dataset_source", "structured_dataset_append", "structured_dataset_query",
        "structured_dataset_export",
        "structured_dataset_preview", "structured_dataset_confirm",
            "structured_dataset_commit", "structured_dataset_task_get",
            "structured_dataset_capabilities",
    }
    schemas = {t.name: t.inputSchema for t in tools}
    assert schemas["structured_dataset_append"]["$defs"]["DatasetValue"]["additionalProperties"] is False
    assert schemas["structured_dataset_create"]["$defs"]["DatasetField"]["additionalProperties"] is False
    assert "owner_user_id" not in schemas["structured_dataset_create"]["properties"]
    legacy_properties = schemas["structured_dataset_create_from_excel"]["properties"]
    assert "extraction_run_id" in legacy_properties
    assert legacy_properties["legacy_target_policy"]["anyOf"][0]["const"] == "CONVERT_TO_XLSX"


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

        calls.clear()
        await mcp.call_tool("structured_dataset_create_from_excel", {
            "document_id": "66666666-6666-4666-8666-666666666666",
            "document_version_id": "77777777-7777-4777-8777-777777777777",
            "extraction_run_id": "88888888-8888-4888-8888-888888888888",
            "sheet_name": "登记",
            "idempotency_key": "uploaded-excel-target",
            "legacy_target_policy": "CONVERT_TO_XLSX",
        })
        assert calls[0] == (
            "POST",
            "/from-excel",
            {
                "document_id": "66666666-6666-4666-8666-666666666666",
                "document_version_id": "77777777-7777-4777-8777-777777777777",
                "extraction_run_id": "88888888-8888-4888-8888-888888888888",
                "sheet_name": "登记",
                "idempotency_key": "uploaded-excel-target",
                "legacy_target_policy": "CONVERT_TO_XLSX",
            },
        )
        assert calls[-1] == "closed"

        calls.clear()
        await mcp.call_tool("structured_dataset_resolve", {
            "filename": "2026年人才材料登记表.xlsx",
        })
        assert calls[0] == (
            "GET",
            "/targets/by-filename?filename=2026%E5%B9%B4%E4%BA%BA%E6%89%8D%E6%9D%90%E6%96%99%E7%99%BB%E8%AE%B0%E8%A1%A8.xlsx",
            None,
        )
        assert calls[-1] == "closed"

        calls.clear()
        await mcp.call_tool("structured_dataset_source", {
            "document_id": "22222222-2222-4222-8222-222222222222",
            "content_origin": "WORKBUDDY",
            "mode": "INDEX",
        })
        assert calls[0] == (
            "GET", "/sources/22222222-2222-4222-8222-222222222222?content_origin=WORKBUDDY&mode=INDEX", None,
        )
        assert calls[-1] == "closed"

        calls.clear()
        await mcp.call_tool("structured_dataset_source", {
            "document_id": "22222222-2222-4222-8222-222222222222",
            "extraction_run_id": "44444444-4444-4444-8444-444444444444",
            "unit_orders": [1, 3],
        })
        assert calls[0] == (
            "GET", "/sources/22222222-2222-4222-8222-222222222222?"
                   "extraction_run_id=44444444-4444-4444-8444-444444444444&unit_orders=1&unit_orders=3", None,
        )
        assert calls[-1] == "closed"

        calls.clear()
        await mcp.call_tool("structured_dataset_append", {
            "dataset_id": "11111111-1111-4111-8111-111111111111",
            "document_id": "22222222-2222-4222-8222-222222222222",
            "document_version_id": "33333333-3333-4333-8333-333333333333",
            "extraction_run_id": "44444444-4444-4444-8444-444444444444",
            "source_sha256": "a" * 64,
            "idempotency_key": "append-with-evidence",
            "content_origin": "WORKBUDDY",
            "target_working_copy_id": "66666666-6666-4666-8666-666666666666",
            "target_document_id": "77777777-7777-4777-8777-777777777777",
            "target_document_version_id": "88888888-8888-4888-8888-888888888888",
            "records": [{
                "fields": {
                    "name": {
                        "raw_text": "张三",
                        "page_number": None,
                        "evidence_refs": [{
                            "source_unit_id": "55555555-5555-4555-8555-555555555555",
                            "quote": "姓名：张三",
                            "start": 0,
                            "end": 5,
                        }],
                    }
                }
            }],
        })
        append_payload = calls[0][2]
        assert append_payload["records"][0]["fields"]["name"]["evidence_refs"][0]["source_unit_id"] == (
            "55555555-5555-4555-8555-555555555555"
        )
        assert append_payload["target_working_copy_id"] == "66666666-6666-4666-8666-666666666666"
        assert append_payload["target_document_id"] == "77777777-7777-4777-8777-777777777777"
        assert append_payload["target_document_version_id"] == "88888888-8888-4888-8888-888888888888"
        # 回归真实故障：此处必须能被 HTTP JSON 编码器直接序列化。
        json.dumps(append_payload, ensure_ascii=False)
        assert calls[-1] == "closed"
    asyncio.run(scenario())


def test_export_tool_delivers_resource_link(tmp_path):
    """导出应交付宿主可识别的 ResourceLink，不只给需要认证的 API URL。"""
    class FakeClient:
        """隔离后端与本机写入，只验证真实工具包装。"""
        async def dataset_request(self, *args):
            """返回已发布快照。"""
            return {"export_id": "export-one", "revision": 1,
                    "working_copy_id": "copy-one", "document_version_id": "version-one",
                    "version_number": 2}
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
        assert json.loads(result.content[0].text)["document_version_id"] == "version-one"
        assert "v2" in result.content[1].text
        assert "working_copy_id=copy-one" in result.content[1].text
        assert "宿主下载副本" in result.content[1].text
        assert "file:///" not in str(result.structuredContent)
        assert result.structuredContent["downloaded"]
    asyncio.run(scenario())
def test_complex_template_preview_requires_real_cell_coordinates(tmp_path):
    """宿主有固定读取草稿时先检查真实网格，不能凭 Sheet 文字猜复杂表头坐标。"""

    from uuid import uuid4
    import pytest
    from file_agent_mcp.dataset_tasks import _check_template_grid
    from file_agent_mcp.extraction_drafts import ExtractionDraftStore, result_digest

    task_id = str(uuid4())
    draft = {"units": [{"kind": "SHEET", "sheet_name": "登记", "text": "姓名\t金额", "blocks": []}]}
    digest = result_digest(draft)
    ExtractionDraftStore(tmp_path).save(task_id, "test", "lease", draft)
    payload = {"manifest": {"target": {"uploaded_task_id": task_id, "sheet_name": "登记",
        "layout": {"header_range": "A2:B2", "data_range": "A3:B3"}},
        "extraction_confirmations": [{"task_id": task_id, "result_digest": digest}]}}
    with pytest.raises(ValueError, match="TARGET_EXCEL_HEADER_CELLS_MISSING"):
        _check_template_grid(payload, tmp_path)

    draft["units"][0]["blocks"] = [{"kind": "TABLE_CELL", "row_index": 1,
                                      "column_index": 0, "text": "姓名"}]
    digest = result_digest(draft)
    ExtractionDraftStore(tmp_path).save(task_id, "test", "lease", draft)
    payload["manifest"]["extraction_confirmations"][0]["result_digest"] = digest
    _check_template_grid(payload, tmp_path)
