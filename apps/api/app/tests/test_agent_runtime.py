"""MVP LangGraph Agent Runtime 的行为测试。

这些测试保护核心安全模型：Planner 输出必须是声明式计划，Tool 必须来自白名单，
Tool 输入必须经过 schema 校验，直接文件写入必须在 dispatch 前被拒绝。
"""

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.agent.planner import DeterministicPlanner
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.tool_registry import ToolRegistry, UnknownToolError
from app.modules.agent.tool_schemas import ToolInputValidationError


def test_get_agent_tools_returns_mvp_catalog():
    """Tool catalog 接口必须暴露 tool-dispatch 使用的白名单。"""

    client = TestClient(app)

    response = client.get("/api/agent/tools")

    assert response.status_code == 200
    data = response.json()
    tool_names = {tool["name"] for tool in data["tools"]}
    assert "document-convert" in tool_names
    assert "operation-plan-create" in tool_names
    assert "confirmed-file-action" in tool_names


def test_local_web_origin_is_allowed_for_api_requests():
    """本地前端开发服务必须可以通过浏览器预检访问后端 API。"""

    client = TestClient(app)

    response = client.options(
        "/api/auth/login",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"


def test_planner_returns_declarative_tool_plan():
    """确定性 Planner 返回 Skill 和 Tool 步骤，而不是直接动作。"""

    planner = DeterministicPlanner()

    plan = planner.plan(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="帮我读取并分类这批文件",
        attachments=[{"document_id": "doc-1"}],
    )

    assert plan.intent == "CLASSIFY_FILES"
    assert plan.selected_skills == [
        "chat-intake",
        "file-ingest",
        "document-classification",
        "change-report",
    ]
    assert [step.tool_name for step in plan.steps] == [
        "document-convert",
        "metadata-extract",
        "multi-label-classify",
        "change-report",
    ]
    assert all(not step.requires_confirmation for step in plan.steps)


def test_unknown_tool_is_rejected():
    """Planner 引用白名单外 Tool 时必须关闭式失败。"""

    registry = ToolRegistry()

    with pytest.raises(UnknownToolError):
        registry.invoke("not-a-tool", {"document_id": "doc-1"})


def test_invalid_tool_input_is_rejected():
    """Tool schema 必须在 handler 执行前拒绝缺失必填字段的输入。"""

    registry = ToolRegistry()

    with pytest.raises(ToolInputValidationError):
        registry.invoke("document-convert", {})


def test_message_starts_langgraph_run_and_records_tool_invocations():
    """一条消息可以完成一次内存态 LangGraph 运行，并记录 Tool 调用。"""

    service = AgentRuntimeService()

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="帮我读取并分类这批文件",
        attachments=[{"document_id": "doc-1"}],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "CLASSIFY_FILES"
    assert result.selected_skills == [
        "chat-intake",
        "file-ingest",
        "document-classification",
        "change-report",
    ]
    assert [item.tool_name for item in result.tool_invocations] == [
        "document-convert",
        "metadata-extract",
        "multi-label-classify",
        "change-report",
    ]
    assert result.final_response


def test_graph_does_not_execute_direct_file_writes_from_planner_output():
    """不安全的直接文件系统指令必须在 Planner 校验阶段被拒绝。"""

    service = AgentRuntimeService()

    with pytest.raises(ValidationError):
        service.run_message(
            conversation_id="conv-1",
            user_id="user-1",
            message_id="msg-1",
            message="把文件写到 /tmp/unsafe",
            attachments=[{"document_id": "doc-1"}],
            planner=DeterministicPlanner(force_unsafe_step=True),
        )
