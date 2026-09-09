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
