"""WorkBuddy MCP 服务启动契约测试。

客户端方法通过并不代表 FastMCP 能接受全部工具签名；本模块真实导入服务并读取注册清单，
防止部署后才发现结构化输出注解不合法。
"""

from __future__ import annotations

import asyncio


def test_server_imports_and_registers_complete_ingest_tool_set() -> None:
    """服务必须可实例化，并完整注册批量、查重、OCR、恢复和条目动作工具。"""

    from file_agent_mcp.server import mcp

    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "file_ingest",
        "file_batch_ingest",
        "workbuddy_attachment_ingest",
        "file_search",
        "file_read",
        "evidence_answer",
        "file_rename",
        "file_search_clarification_resolve",
        "operation_plan_get",
        "operation_plan_confirm",
        "batch_resume",
        "batch_get",
        "job_get",
        "duplicate_review_get",
        "duplicate_decide",
        "extraction_claim",
        "extraction_renew",
        "extraction_submit",
        "ingest_retry",
        "ingest_cancel",
    }
    schemas = {tool.name: tool.inputSchema for tool in tools}
    # 结构化子项也必须拒绝多余字段，不能只依赖 handler 内的二次检查。
    assert schemas["file_rename"]["$defs"]["ExplicitRenameInput"]["additionalProperties"] is False
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
