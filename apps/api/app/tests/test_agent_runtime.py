"""MVP LangGraph Agent Runtime 的行为测试。

这些测试保护核心安全模型：Planner 输出必须是声明式计划，Tool 必须来自白名单，
Tool 输入必须经过 schema 校验，直接文件写入必须在 dispatch 前被拒绝。
"""

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.llm.schemas import UserIntentPlan
from app.modules.agent.repository import _safe_graph_state_snapshot
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


def test_initial_state_does_not_include_runtime_dependencies():
    """AgentGraphState 只能保存业务状态，不能保存运行时服务对象。"""

    service = AgentRuntimeService()

    state = service._build_initial_state(
        agent_run_id="run-1",
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="帮我读取文件",
        attachments=[{"document_id": "doc-1"}],
        planner_mode="deterministic",
    )

    for key in ["planner", "registry", "context_loader", "llm_intent_service", "prefer_explicit_planner"]:
        assert key not in state
    assert state["planner_mode"] == "deterministic"
    assert state["document_results"] == []


def test_safe_snapshot_excludes_runtime_dependencies():
    """AgentRun 快照不得包含 Planner、Registry、DB Session 或 LLM client 等运行对象。"""

    snapshot = _safe_graph_state_snapshot(
        {
            "status": "RUNNING_TOOL",
            "planner": object(),
            "registry": object(),
            "context_loader": object(),
            "llm_intent_service": object(),
            "planner_mode": "llm",
            "tool_plan": {"steps": []},
            "document_results": [{"document_id": "doc-1", "categories": []}],
        }
    )

    for key in ["planner", "registry", "context_loader", "llm_intent_service"]:
        assert key not in snapshot
    assert snapshot["planner_mode"] == "llm"
    assert snapshot["document_results"] == [{"document_id": "doc-1", "categories": []}]


def test_runtime_context_builds_fresh_user_scoped_registry():
    """每次 AgentRun 必须通过 factory 构造用户级 Registry，避免复用旧用户上下文。"""

    calls: list[tuple[object, str]] = []

    def registry_factory(db, user_id):
        """记录 Registry 构造参数，并返回真实 Registry。"""

        calls.append((db, user_id))
        return ToolRegistry(db=db, user_id=user_id)

    service = AgentRuntimeService(registry_factory=registry_factory)

    context_a = service._build_runtime_context(db=None, user_id="user-a", planner=None)
    context_b = service._build_runtime_context(db=None, user_id="user-b", planner=None)

    assert context_a.registry is not context_b.registry
    assert context_a.registry.user_id == "user-a"
    assert context_b.registry.user_id == "user-b"
    assert [item[1] for item in calls] == ["user-a", "user-b"]


def test_llm_intent_reads_document_insights_instead_of_reingesting():
    """LLM 理解到用户要看已上传文件信息时，应读取洞察而不是重复上传处理。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，避免单元测试访问真实模型。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回稳定的文件总结意图。"""

            return UserIntentPlan(
                intent="SUMMARIZE_DOCUMENTS",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[attachments[0]["document_id"]],
                required_capabilities=["read_document_insights"],
                skip_completed_ingest=True,
                tool_plan_hint=["read-document-insights"],
                response_style="concise",
            )

    service = AgentRuntimeService(llm_intent_service=FakeLLMIntentService())

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="总结我刚才上传的文件",
        attachments=[{"document_id": "doc-1"}],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "SUMMARIZE_DOCUMENTS"
    assert result.selected_skills == ["llm-understanding", "document-insight-read"]
    assert [item.tool_name for item in result.tool_invocations] == ["read-document-insights"]
    assert "document-convert" not in [item.tool_name for item in result.tool_invocations]


def test_llm_intent_extracts_document_text():
    """LLM 理解到用户要读取正文时，应调用 extract-document-text。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回文件正文解析意图。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回读取文件正文的用户意图。"""

            return UserIntentPlan(
                intent="EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[attachments[0]["document_id"]],
                required_capabilities=["extract_document_text"],
                skip_completed_ingest=True,
                tool_plan_hint=["extract-document-text"],
                response_style="concise",
            )

    service = AgentRuntimeService(llm_intent_service=FakeLLMIntentService())

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="读取这个文件内容",
        attachments=[{"document_id": "doc-1"}],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "EXTRACT_DOCUMENT_TEXT"
    assert result.selected_skills == ["llm-understanding", "document-text-extract"]
    assert [item.tool_name for item in result.tool_invocations] == ["extract-document-text"]


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
