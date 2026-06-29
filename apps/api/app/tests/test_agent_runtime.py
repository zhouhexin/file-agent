"""MVP LangGraph Agent Runtime 的行为测试。

这些测试保护核心安全模型：Planner 输出必须是声明式计划，Tool 必须来自白名单，
Tool 输入必须经过 schema 校验，直接文件写入必须在 dispatch 前被拒绝。
"""

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.llm.schemas import UserIntentPlan
from app.modules.agent.graph import _build_document_results_response
from app.modules.agent.repository import _safe_graph_state_snapshot
from app.modules.agent.planner import DeterministicPlanner
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
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
    """只分类请求必须进入真实正文解析链路，而不是旧占位分类链路。"""

    planner = DeterministicPlanner()

    plan = planner.plan(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="帮我分类这批文件",
        attachments=[{"document_id": "doc-1"}],
    )

    assert plan.intent == "CLASSIFY_FILES"
    assert plan.selected_skills == [
        "chat-intake",
        "document-text-extract",
        "document-classification",
        "change-report",
    ]
    assert [step.tool_name for step in plan.steps] == ["extract-document-text"]
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
    """只分类消息也必须走真实正文解析 Tool，避免占位分类结果外露。"""

    service = AgentRuntimeService()

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="帮我分类这批文件",
        attachments=[{"document_id": "doc-1"}],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "CLASSIFY_FILES"
    assert result.selected_skills == [
        "chat-intake",
        "document-text-extract",
        "document-classification",
        "change-report",
    ]
    assert [item.tool_name for item in result.tool_invocations] == ["extract-document-text"]
    assert result.final_response


def test_deterministic_planner_extracts_text_for_read_and_classify_attachments():
    """读取并分类组合意图必须优先走正文解析，再由 document_results 生成分类回执。"""

    service = AgentRuntimeService()

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="帮我读取并分类这批文件",
        attachments=[{"document_id": "doc-1"}, {"document_id": "doc-2"}],
        planner=DeterministicPlanner(),
    )

    assert result.intent == "EXTRACT_DOCUMENT_TEXT"
    assert result.tool_plan["slots"]["document_ids"] == ["doc-1", "doc-2"]
    assert result.tool_plan["slots"]["requested_outputs"] == ["text", "classification", "receipt"]
    assert [step["tool_name"] for step in result.tool_plan["steps"]] == [
        "extract-document-text",
        "extract-document-text",
    ]


def test_deterministic_planner_extracts_text_for_all_read_attachments():
    """确定性 Planner 对“读取这批文件”必须为全部附件生成真实正文解析步骤。"""

    service = AgentRuntimeService()

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="读取这批文件",
        attachments=[{"document_id": "doc-1"}, {"document_id": "doc-2"}],
        planner=DeterministicPlanner(),
    )

    assert result.intent == "EXTRACT_DOCUMENT_TEXT"
    assert result.tool_plan["slots"]["document_ids"] == ["doc-1", "doc-2"]
    assert [step["tool_name"] for step in result.tool_plan["steps"]] == [
        "extract-document-text",
        "extract-document-text",
    ]
    assert [step["input"]["document_id"] for step in result.tool_plan["steps"]] == ["doc-1", "doc-2"]


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


def test_llm_intent_extracts_text_for_all_referenced_documents():
    """LLM 解析出多个附件时，Planner 必须为每个文件生成独立解析步骤。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回多文件正文解析意图。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回所有附件对应的 document_id，验证 Planner 不再只取第一个。"""

            return UserIntentPlan(
                intent="EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[item["document_id"] for item in attachments],
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
        message="读取并分类这批文件",
        attachments=[{"document_id": "doc-1"}, {"document_id": "doc-2"}],
    )

    assert result.status == "COMPLETED"
    assert result.tool_plan["slots"]["document_ids"] == ["doc-1", "doc-2"]
    assert [step["input"]["document_id"] for step in result.tool_plan["steps"]] == ["doc-1", "doc-2"]
    assert [item.tool_name for item in result.tool_invocations] == ["extract-document-text", "extract-document-text"]


def test_tool_dispatch_records_step_failure_and_continues_batch():
    """批量 Tool 执行中单个文件失败时，必须继续处理后续文件并生成失败回执。"""

    class FakeRegistry:
        """测试用 Registry，第一个文件抛异常，第二个文件成功。"""

        user_id = "user-1"

        def invoke(self, tool_name, input_json):
            """按 document_id 模拟单步失败和后续成功。"""

            document_id = input_json["document_id"]
            if document_id == "doc-bad":
                raise ValueError("模拟解析失败")
            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "document_id": document_id,
                    "extraction_run_id": "run-good",
                    "status": "COMPLETED",
                    "extractor": "plain-text",
                    "pages": [{"page_number": 1, "text_preview": "教师职称材料", "char_count": 6}],
                    "error": None,
                },
                status="COMPLETED",
            )

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回两个文件解析意图。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回全部附件，触发批量 Tool dispatch。"""

            return UserIntentPlan(
                intent="EXTRACT_DOCUMENT_TEXT",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[item["document_id"] for item in attachments],
                required_capabilities=["extract_document_text"],
                skip_completed_ingest=True,
                tool_plan_hint=["extract-document-text"],
                response_style="concise",
            )

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=FakeLLMIntentService(),
    )

    result = service.run_message(
        conversation_id="conv-1",
        user_id="user-1",
        message_id="msg-1",
        message="读取并分类这批文件",
        attachments=[{"document_id": "doc-bad"}, {"document_id": "doc-good"}],
    )

    assert result.status == "COMPLETED"
    assert [item.status for item in result.tool_invocations] == ["FAILED", "COMPLETED"]
    assert [item["status"] for item in result.tool_results] == ["FAILED", "COMPLETED"]
    assert [item["document_id"] for item in result.document_results] == ["doc-bad", "doc-good"]
    assert [item["extraction_status"] for item in result.document_results] == ["FAILED", "COMPLETED"]
    assert result.document_results[1]["categories"][0]["name"] == "学校/人事师资/职称"
    assert result.document_results[1]["categories"][0]["confidence"] > 0
    assert result.document_results[1]["categories"][0]["evidence"]
    assert "模拟解析失败" in (result.final_response or "")
    assert "学校/人事师资/职称" in (result.final_response or "")


def test_document_results_response_lists_multiple_categories_with_confidence():
    """逐文件回执必须用分层文本展示多个分类、置信度和证据。"""

    response = _build_document_results_response(
        [
            {
                "filename": "multi.txt",
                "extraction_status": "COMPLETED",
                "page_count": 1,
                "char_count": 30,
                "categories": [
                    {
                        "name": "学校/人事师资/职称",
                        "confidence": 0.8,
                        "evidence": ["职称"],
                    },
                    {
                        "name": "学校/党委相关/干部工作",
                        "confidence": 0.78,
                        "evidence": ["干部工作"],
                    },
                ],
            }
        ]
    )

    assert response.startswith("已处理 1 个文件：\n\n1. multi.txt")
    assert "解析结果：成功，提取 1 页/Sheet，共 30 个字符" in response
    assert (
        "分类建议：\n"
        "- 学校/人事师资/职称\n"
        "  置信度：0.80\n"
        "  依据：职称"
    ) in response
    assert (
        "- 学校/党委相关/干部工作\n"
        "  置信度：0.78\n"
        "  依据：干部工作"
    ) in response


def test_document_results_response_hides_extra_low_confidence_categories():
    """分类建议过多时只展示前三个，避免对话回执过长。"""

    response = _build_document_results_response(
        [
            {
                "filename": "many.txt",
                "extraction_status": "COMPLETED",
                "page_count": 1,
                "char_count": 30,
                "categories": [
                    {"name": f"分类{i}", "confidence": 0.9 - i * 0.01, "evidence": [f"证据{i}"]}
                    for i in range(5)
                ],
            }
        ]
    )

    assert "- 分类0" in response
    assert "- 分类1" in response
    assert "- 分类2" in response
    assert "- 分类3" not in response
    assert "- 分类4" not in response
    assert "另有 2 个低置信度候选未展示。" in response


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
