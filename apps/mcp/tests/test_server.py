"""WorkBuddy MCP 服务启动契约测试。

客户端方法通过并不代表 FastMCP 能接受全部工具签名；本模块真实导入服务并读取注册清单，
防止部署后才发现结构化输出注解不合法。
"""

from __future__ import annotations

import asyncio

from mcp.types import CallToolResult, ResourceLink, TextContent


def test_server_imports_and_registers_complete_ingest_tool_set() -> None:
    """服务必须可实例化，并完整注册批量、查重、OCR、恢复和条目动作工具。"""

    from file_agent_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "file_ingest",
        "file_batch_ingest",
        "workbuddy_attachment_ingest",
        "workbuddy_submission_ingest",
        "file_search",
        "file_read",
        "file_download",
        "evidence_answer",
        "file_rename",
        "file_set_primary_category",
        "file_move",
        "file_placement_status",
        "file_search_clarification_resolve",
        "operation_plan_get",
        "operation_plan_confirm",
        "batch_resume",
        "batch_get",
        "job_get",
        "duplicate_review_get",
        "duplicate_comparison_get",
        "duplicate_decide",
        "extraction_claim",
        "extraction_renew",
        "extraction_submit",
        "ingest_retry",
        "ingest_cancel",
    }
    descriptions = {tool.name: tool.description or "" for tool in tools}
    assert "不能只重复查询状态" in descriptions["workbuddy_submission_ingest"]
    assert "重复调用本工具不会完成 OCR" in descriptions["batch_get"]
    assert "不得让 File Agent 后端自行 OCR" in descriptions["extraction_claim"]
    assert "必须为 null" in descriptions["extraction_submit"]
    assert "必须逐字原样输出" in descriptions["file_search"]
    assert "即使用户要求文件类型" in descriptions["file_search"]
    schemas = {tool.name: tool.inputSchema for tool in tools}
    # 结构化子项也必须拒绝多余字段，不能只依赖 handler 内的二次检查。
    assert schemas["file_rename"]["$defs"]["ExplicitRenameInput"]["additionalProperties"] is False
    assert (
        schemas["file_set_primary_category"]["$defs"][
            "SetPrimaryCategoryInput"
        ]["additionalProperties"]
        is False
    )
    assert (
        schemas["file_move"]["$defs"][
            "MoveWorkingCopyInput"
        ]["additionalProperties"]
        is False
    )
    assert schemas["file_set_primary_category"]["$defs"][
        "SetPrimaryCategoryInput"
    ]["properties"]["action"]["const"] == "SET_PRIMARY"
    assert schemas["file_move"]["$defs"]["MoveWorkingCopyInput"][
        "properties"
    ]["action"]["const"] == "MOVE"
    assert (
        schemas["workbuddy_attachment_ingest"]["$defs"]["WorkBuddyAttachmentInput"][
            "additionalProperties"
        ]
        is False
    )
    assert schemas["file_read"]["properties"]["read_mode"]["enum"] == [
        "READ",
        "SUMMARY",
        "EXPLAIN",
    ]
    assert "working_copy_id" in schemas["file_download"]["properties"]


def test_file_search_tool_exposes_clickable_table_and_keeps_ids_structured(monkeypatch) -> None:
    """搜索工具展示文件名、依据与公开链接，稳定 ID 仅保留在结构化上下文。"""

    import file_agent_mcp.server as server

    class FakeClient:
        """返回包含内部检索字段的后端结果，验证 MCP 最终投影。"""

        async def file_search(self, **_kwargs) -> dict:
            """模拟后端搜索响应。"""

            return {
                "query": "人才推荐",
                "files": [
                    {
                        "filename": "推荐意见.docx",
                        "document_id": "doc-1",
                        "working_copy_id": "copy-1",
                        "resource_type": "WORKING_COPY",
                        "relative_path": "人事处/推荐意见.docx",
                        "relevance_tier": "SUPPORTED",
                        "match_reasons": ["正文主题命中：人才推荐"],
                        "preview_url": "http://file-agent.test/api/public/file-access/token/preview",
                        "download_url": "http://file-agent.test/api/public/file-access/token/download",
                    }
                ],
            }

        async def close(self) -> None:
            """模拟关闭连接。"""

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    result = asyncio.run(server.file_search("thread-1", "人才推荐"))

    assert isinstance(result, CallToolResult)
    text_items = [item for item in result.content if isinstance(item, TextContent)]
    assert len(text_items) == 1
    assert text_items[0].annotations is not None
    assert text_items[0].annotations.audience == ["user"]
    assert text_items[0].annotations.priority == 1.0
    visible = text_items[0].text
    assert "<!-- FILE_AGENT_DISPLAY_CONTRACT:" in visible
    assert "| 文件名 | 依据 | 操作 |" in visible
    assert "推荐意见.docx" in visible
    assert "正文主题命中" in visible
    assert "人事处/推荐意见.docx" not in visible
    assert "SUPPORTED" not in visible
    assert "doc-1" not in visible
    assert "[预览](http://file-agent.test/api/public/file-access/token/preview)" in visible
    assert "[下载](http://file-agent.test/api/public/file-access/token/download)" in visible
    assert result.structuredContent["files"][0]["tool_context"] == {
        "document_id": "doc-1",
        "working_copy_id": "copy-1",
    }
    assert "逐字原样展示" in result.structuredContent["display_policy"]
    assert result.structuredContent["response_contract"] == {
        "mode": "VERBATIM_USER_DISPLAY",
        "source": "content.text",
        "allow_rewrite": False,
        "allow_column_changes": False,
        "required_columns": ["文件名", "依据", "操作"],
        "preserve_markdown_links": True,
    }


def test_file_download_tool_returns_resource_without_token_or_path_text(monkeypatch) -> None:
    """下载结果应使用标准资源入口，用户文本和结构化摘要不得泄露 Token 或本机路径。"""

    import file_agent_mcp.server as server

    class FakeClient:
        """模拟已经完成鉴权和本机缓存写入的客户端。"""

        async def download_working_copy(self, **_kwargs) -> dict:
            """返回标准本机资源元数据。"""

            return {
                "filename": "推荐意见.docx",
                "resource_uri": "file:///E:/file-agent-client-data/downloads/file.docx",
                "content_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "size_bytes": 1024,
            }

        async def close(self) -> None:
            """模拟关闭连接。"""

    monkeypatch.setattr(server, "_client", lambda: FakeClient())
    result = asyncio.run(server.file_download("copy-1"))

    assert isinstance(result, CallToolResult)
    text_items = [item.text for item in result.content if isinstance(item, TextContent)]
    resources = [item for item in result.content if isinstance(item, ResourceLink)]
    assert text_items == ["已准备下载：推荐意见.docx"]
    assert len(resources) == 1
    assert resources[0].name == "推荐意见.docx"
    assert resources[0].uri.scheme == "file"
    assert "file:///" not in str(result.structuredContent)
    assert "token" not in str(result).lower()
