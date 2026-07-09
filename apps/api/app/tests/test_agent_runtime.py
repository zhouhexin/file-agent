"""MVP LangGraph Agent Runtime 的行为测试。

这些测试保护核心安全模型：Planner 输出必须是声明式计划，Tool 必须来自白名单，
Tool 输入必须经过 schema 校验，直接文件写入必须在 dispatch 前被拒绝。
"""

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from app.main import app
from app.modules.agent.capabilities.service import load_agent_capabilities
from app.modules.agent.capability_router import route_user_intent
from app.modules.llm.schemas import UserIntentPlan
from app.modules.agent.graph import _build_document_results_response
from app.modules.agent.repository import _safe_graph_state_snapshot
from app.modules.agent.planner import DeterministicPlanner, build_plan_from_user_intent
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry, UnknownToolError
from app.modules.agent.tool_schemas import ToolInputValidationError
from app.modules.llm.client import LLMResponseError


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
    assert "profile-spreadsheet" in tool_names
    assert "validate-spreadsheet" in tool_names
    assert "managed-file-list" in tool_names
    assert "managed-file-search" in tool_names
    assert "managed-root-scan" in tool_names


def test_capability_catalog_exposes_router_metadata():
    """能力目录必须暴露路由元数据，供 Planner 从能力选择 Tool。"""

    catalog = load_agent_capabilities(detail_level="full")
    routed = {
        capability["id"]: capability
        for capability in catalog["capabilities"]
        if capability["id"]
        in {"document_summary", "document_classification", "spreadsheet_analysis", "spreadsheet_workbench"}
    }

    assert routed["document_summary"]["tool_names"] == ["extract-document-text"]
    assert "read-document-classifications" in routed["document_classification"]["tool_names"]
    assert routed["spreadsheet_analysis"]["tool_names"] == ["analyze-spreadsheet"]
    assert routed["spreadsheet_workbench"]["tool_names"] == ["profile-spreadsheet", "validate-spreadsheet"]


def test_capability_router_maps_classification_summary_to_read_tool():
    """读取已有分类建议必须路由到分类读取 Tool。"""

    route = route_user_intent(
        intent="SUMMARIZE_CLASSIFICATIONS",
        required_capabilities=["read_document_classifications"],
        tool_plan_hint=["read-document-classifications"],
    )

    assert route is not None
    assert route.intent == "SUMMARIZE_CLASSIFICATIONS"
    assert route.tool_name == "read-document-classifications"


def test_capability_router_maps_spreadsheet_analysis_to_table_tool():
    """表格汇总能力必须路由到表格分析 Tool。"""

    route = route_user_intent(
        intent="SPREADSHEET_ANALYSIS",
        required_capabilities=["analyze_spreadsheet"],
        tool_plan_hint=[],
        attachments=[{"document_id": "doc-csv", "filename": "test.csv"}],
    )

    assert route is not None
    assert route.intent == "ANALYZE_SPREADSHEET"
    assert route.tool_name == "analyze-spreadsheet"


def test_capability_router_maps_spreadsheet_workbench_tools():
    """表格 Profile 和校验必须通过工作台能力路由到对应只读 Tool。"""

    profile_route = route_user_intent(
        intent="PROFILE_SPREADSHEET",
        required_capabilities=["profile_spreadsheet"],
        tool_plan_hint=["profile-spreadsheet"],
        attachments=[{"document_id": "doc-xlsx", "filename": "demo.xlsx"}],
    )
    validate_route = route_user_intent(
        intent="VALIDATE_SPREADSHEET",
        required_capabilities=["validate_spreadsheet"],
        tool_plan_hint=["validate-spreadsheet"],
        attachments=[{"document_id": "doc-xlsx", "filename": "demo.xlsx"}],
    )

    assert profile_route is not None
    assert profile_route.tool_name == "profile-spreadsheet"
    assert validate_route is not None
    assert validate_route.tool_name == "validate-spreadsheet"


def test_capability_router_maps_help_and_taxonomy_tools():
    """能力帮助和分类目录也必须能通过能力路由识别。"""

    help_route = route_user_intent(
        intent="CAPABILITY_HELP",
        required_capabilities=["read_agent_capabilities"],
        tool_plan_hint=[],
    )
    taxonomy_route = route_user_intent(
        intent="LIST_CLASSIFICATION_TAXONOMY",
        required_capabilities=["read_classification_taxonomy"],
        tool_plan_hint=[],
    )

    assert help_route is not None
    assert help_route.tool_name == "read-agent-capabilities"
    assert taxonomy_route is not None
    assert taxonomy_route.tool_name == "read-classification-taxonomy"


def test_capability_router_maps_managed_file_list_tool():
    """受管目录文件列表能力必须路由到 managed-file-list。"""

    route = route_user_intent(
        intent="LIST_MANAGED_FILES",
        required_capabilities=["managed_file_list"],
        tool_plan_hint=["managed-file-list"],
    )

    assert route is not None
    assert route.tool_name == "managed-file-list"


def test_capability_router_does_not_route_by_file_type_only():
    """文件类型只能辅助能力选择，不能仅凭 CSV 附件触发表格 Tool。"""

    route = route_user_intent(
        intent="GENERAL_CHAT",
        required_capabilities=[],
        tool_plan_hint=[],
        attachments=[{"document_id": "doc-csv", "filename": "test.csv"}],
    )

    assert route is None


def test_deterministic_planner_routes_managed_file_list_by_root_key():
    """“列出某个受管目录下文件”不应被当成普通对话。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed",
        user_id="user-1",
        message_id="msg-managed",
        message="列出file_agent_spreadsheet_patch_files下的所有文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert [step.tool_name for step in plan.steps] == ["managed-file-list"]
    assert plan.steps[0].input["root_key"] == "file_agent_spreadsheet_patch_files"


def test_deterministic_planner_routes_managed_file_list_by_subdirectory():
    """受管目录子目录查询必须生成 path_prefix，不能返回整个根目录。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-subdir",
        user_id="user-1",
        message_id="msg-managed-subdir",
        message="列出file_agent_spreadsheet_patch_files下deploy目录中的文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["root_key"] == "file_agent_spreadsheet_patch_files"
    assert plan.steps[0].input["path_prefix"] == "deploy"
    assert plan.slots["path_prefix"] == "deploy"


def test_deterministic_planner_routes_nested_managed_file_subdirectory():
    """受管目录嵌套子目录查询必须保留 POSIX 相对路径。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-nested",
        user_id="user-1",
        message_id="msg-managed-nested",
        message="查看file_agent_spreadsheet_patch_files下apps/api目录里的文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["path_prefix"] == "apps/api"


def test_deterministic_planner_routes_managed_file_list_by_extension():
    """“列出某目录下所有 PDF 文件”必须生成 extension，而不是 path_prefix。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-pdf",
        user_id="user-1",
        message_id="msg-managed-pdf",
        message="列出Downloads下所有pdf文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["root_key"] == "Downloads"
    assert plan.steps[0].input["extension"] == "pdf"
    assert "path_prefix" not in plan.steps[0].input


def test_deterministic_planner_routes_all_managed_files_by_extension_without_root():
    """没有显式 root 时，扩展名过滤请求仍应列出受管文件而不是普通聊天。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-all-pdf",
        user_id="user-1",
        message_id="msg-managed-all-pdf",
        message="列出所有pdf文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["extension"] == "pdf"
    assert "root_key" not in plan.steps[0].input
    assert "path_prefix" not in plan.steps[0].input


def test_deterministic_planner_routes_managed_file_list_by_filename_keyword():
    """文件名包含条件必须进入 filename_contains。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-name",
        user_id="user-1",
        message_id="msg-managed-name",
        message="列出Downloads下文件名包含发票的文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["filename_contains"] == "发票"
    assert "path_prefix" not in plan.steps[0].input


def test_deterministic_planner_routes_managed_file_list_by_filename_and_extension():
    """文件名关键字和扩展名过滤必须可以同时存在。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-name-pdf",
        user_id="user-1",
        message_id="msg-managed-name-pdf",
        message="列出Downloads下文件名包含发票的pdf文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["filename_contains"] == "发票"
    assert plan.steps[0].input["extension"] == "pdf"
    assert "path_prefix" not in plan.steps[0].input


def test_deterministic_planner_routes_managed_subdirectory_with_extension():
    """子目录和扩展名过滤必须可以同时存在。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conv-managed-subdir-pdf",
        user_id="user-1",
        message_id="msg-managed-subdir-pdf",
        message="列出Downloads下file_agent_spreadsheet_patch_files目录中的pdf文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["root_key"] == "Downloads"
    assert plan.steps[0].input["path_prefix"] == "file_agent_spreadsheet_patch_files"
    assert plan.steps[0].input["extension"] == "pdf"


def test_llm_managed_file_list_uses_structured_filters():
    """LLM 输出的扩展名和文件名过滤字段必须进入 Tool 输入。"""

    intent_plan = UserIntentPlan(
        intent="LIST_MANAGED_FILES",
        user_goal="列出发票 PDF",
        needs_file_context=False,
        required_capabilities=["managed_file_list"],
        tool_plan_hint=["managed-file-list"],
        managed_root_key="downloads",
        managed_extension="pdf",
        managed_filename_contains="发票",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="列出Downloads下文件名包含发票的pdf文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["root_key"] == "downloads"
    assert plan.steps[0].input["extension"] == "pdf"
    assert plan.steps[0].input["filename_contains"] == "发票"


def test_llm_managed_file_list_uses_structured_path_prefix():
    """LLM 输出的受管目录子目录字段必须进入 Tool 输入。"""

    intent_plan = UserIntentPlan(
        intent="LIST_MANAGED_FILES",
        user_goal="查看 deploy 子目录文件",
        needs_file_context=False,
        required_capabilities=["managed_file_list"],
        tool_plan_hint=["managed-file-list"],
        managed_root_key="file_agent_spreadsheet_patch_files",
        managed_path_prefix="deploy",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="查看file_agent_spreadsheet_patch_files下deploy目录里的文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert plan.steps[0].input["root_key"] == "file_agent_spreadsheet_patch_files"
    assert plan.steps[0].input["path_prefix"] == "deploy"


def test_llm_general_chat_is_overridden_for_managed_file_list_request():
    """LLM 把受管目录列表误判成普通对话时，Planner 必须按目录文件列表执行。"""

    intent_plan = UserIntentPlan(
        intent="GENERAL_CHAT",
        user_goal="列出file_agent_spreadsheet_patch_files下的所有文件",
        needs_file_context=False,
        required_capabilities=[],
        tool_plan_hint=[],
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="列出file_agent_spreadsheet_patch_files下的所有文件",
        attachments=[],
    )

    assert plan.intent == "LIST_MANAGED_FILES"
    assert [step.tool_name for step in plan.steps] == ["managed-file-list"]
    assert plan.steps[0].input["root_key"] == "file_agent_spreadsheet_patch_files"


def test_agent_runtime_bypasses_llm_for_managed_file_list_request():
    """受管目录列表请求必须先走确定性 Planner，不能被 LLM 网络调用阻塞。"""

    class BlockingLLMIntentService:
        """测试用 LLM 服务；如果被调用说明路由顺序错误。"""

        enabled = True

        def understand_user_request(self, **kwargs):
            """模拟不可用的 LLM 意图服务。"""

            raise AssertionError("managed file list request should not call LLM")

    class FakeRegistry:
        """测试用 Registry，返回空受管文件清单。"""

        def invoke(self, tool_name, input_json):
            """返回稳定 Tool 输出。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "query": {"root_key": input_json.get("root_key")},
                    "files": [],
                },
                status="COMPLETED",
            )

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=BlockingLLMIntentService(),
    )

    result = service.run_message(
        conversation_id="conv-managed-bypass",
        user_id="user-1",
        message_id="msg-managed-bypass",
        message="列出file_agent_spreadsheet_patch_files下的所有文件",
    )

    assert result.intent == "LIST_MANAGED_FILES"
    assert [item.tool_name for item in result.tool_invocations] == ["managed-file-list"]


def test_agent_runtime_formats_managed_file_list_response():
    """受管目录文件列表 Tool 结果必须展示为文件清单，而不是普通对话回复。"""

    class FakeRegistry:
        """测试用 Registry，模拟受管目录文件列表返回。"""

        def invoke(self, tool_name, input_json):
            """返回一个稳定的受管文件清单。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "files": [
                        {
                            "root_key": "file_agent_spreadsheet_patch_files",
                            "display_name": "表格补丁文件",
                            "relative_path": "a.xlsx",
                            "category_path": None,
                            "filename": "a.xlsx",
                            "extension": ".xlsx",
                            "size_bytes": 1024,
                            "modified_at": None,
                            "status": "ACTIVE",
                        }
                    ],
                },
                status="COMPLETED",
            )

    service = AgentRuntimeService(registry_factory=lambda db, user_id: FakeRegistry())

    result = service.run_message(
        conversation_id="conv-managed-list",
        user_id="user-1",
        message_id="msg-managed-list",
        message="列出file_agent_spreadsheet_patch_files下的所有文件",
    )

    assert [item.tool_name for item in result.tool_invocations] == ["managed-file-list"]
    assert "file_agent_spreadsheet_patch_files 下共有 1 个文件" in (result.final_response or "")
    assert "a.xlsx" in (result.final_response or "")
    assert "我已收到" not in (result.final_response or "")


def test_agent_runtime_formats_empty_managed_file_list_response():
    """受管目录文件列表为空时也必须给出明确回复，不能落到兜底文本。"""

    class FakeRegistry:
        """测试用 Registry，模拟受管目录没有扫描到文件。"""

        def invoke(self, tool_name, input_json):
            """返回空受管文件清单。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "query": {"root_key": input_json.get("root_key")},
                    "files": [],
                },
                status="COMPLETED",
            )

    service = AgentRuntimeService(registry_factory=lambda db, user_id: FakeRegistry())

    result = service.run_message(
        conversation_id="conv-managed-empty",
        user_id="user-1",
        message_id="msg-managed-empty",
        message="列出file_agent_spreadsheet_patch_files下的所有文件",
    )

    assert [item.tool_name for item in result.tool_invocations] == ["managed-file-list"]
    assert "file_agent_spreadsheet_patch_files 下暂未找到文件" in (result.final_response or "")
    assert "暂未生成可展示的业务结果" not in (result.final_response or "")


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


def test_deterministic_planner_routes_spreadsheet_profile_and_validation():
    """确定性 Planner 必须把表结构和质量检查分流到表格工作台 Tool。"""

    profile_plan = DeterministicPlanner().plan(
        conversation_id="conversation-1",
        user_id="user-1",
        message_id="message-1",
        message="查看这个 Excel 有哪些工作表和字段",
        attachments=[{"document_id": "doc-1", "filename": "demo.xlsx"}],
    )
    validation_plan = DeterministicPlanner().plan(
        conversation_id="conversation-1",
        user_id="user-1",
        message_id="message-2",
        message="检查这份表格有没有公式错误",
        attachments=[{"document_id": "doc-1", "filename": "demo.xlsx"}],
    )

    assert profile_plan.intent == "PROFILE_SPREADSHEET"
    assert profile_plan.steps[0].tool_name == "profile-spreadsheet"
    assert validation_plan.intent == "VALIDATE_SPREADSHEET"
    assert validation_plan.steps[0].tool_name == "validate-spreadsheet"


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


def test_summary_of_previous_uploaded_files_uses_text_summary_plan():
    """“总结之前上传的文件”必须总结正文内容，不能回落成分类回执。"""

    planner = DeterministicPlanner()

    plan = planner.plan(
        conversation_id="conv-summary",
        user_id="user-1",
        message_id="msg-summary",
        message="再次帮我对之前所有上传的文件进行总结",
        attachments=[{"document_id": "doc-1"}, {"document_id": "doc-2"}],
    )

    assert plan.intent == "SUMMARIZE_DOCUMENTS"
    assert plan.slots["requested_outputs"] == ["text", "summary", "receipt"]
    assert [step.tool_name for step in plan.steps] == ["extract-document-text", "extract-document-text"]


def test_llm_classification_hint_is_overridden_for_plain_file_summary():
    """LLM 若把“总结文件”误判成分类读取，Planner 也必须按正文总结纠偏。"""

    intent_plan = UserIntentPlan(
        intent="SUMMARIZE_CLASSIFICATIONS",
        user_goal="再次帮我对之前所有上传的文件进行总结",
        needs_file_context=True,
        referenced_document_ids=["doc-1", "doc-2"],
        required_capabilities=["read_document_classifications"],
        skip_completed_ingest=True,
        tool_plan_hint=["read-document-classifications"],
        response_style="concise",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="再次帮我对之前所有上传的文件进行总结",
        attachments=[{"document_id": "doc-1"}, {"document_id": "doc-2"}],
    )

    assert plan.intent == "SUMMARIZE_DOCUMENTS"
    assert plan.slots["requested_outputs"] == ["text", "summary", "receipt"]
    assert [step.tool_name for step in plan.steps] == ["extract-document-text", "extract-document-text"]


def test_llm_classification_hint_is_overridden_for_table_column_summary():
    """LLM 若把“汇总 CSV 关键词列”误判成分类读取，Planner 也必须按表格分析纠偏。"""

    intent_plan = UserIntentPlan(
        intent="SUMMARIZE_CLASSIFICATIONS",
        user_goal="汇总刚刚上传的csv文件中的关键词列",
        needs_file_context=True,
        referenced_document_ids=["doc-csv"],
        required_capabilities=["read_document_classifications"],
        skip_completed_ingest=True,
        tool_plan_hint=["read-document-classifications"],
        response_style="concise",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="汇总刚刚上传的csv文件中的关键词列",
        attachments=[{"document_id": "doc-csv"}],
    )

    assert plan.intent == "ANALYZE_SPREADSHEET"
    assert plan.slots["requested_outputs"] == ["spreadsheet_analysis"]
    assert [step.tool_name for step in plan.steps] == ["analyze-spreadsheet"]


def test_llm_capability_route_prefers_router_for_classification_summary():
    """LLM 能力 hint 应先经过 CapabilityRouter，再生成受控 ToolPlan。"""

    intent_plan = UserIntentPlan(
        intent="SUMMARIZE_CLASSIFICATIONS",
        user_goal="帮我总结刚刚上传文件的分类",
        needs_file_context=True,
        referenced_document_ids=["doc-1"],
        required_capabilities=["read_document_classifications"],
        tool_plan_hint=["read-document-classifications"],
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="帮我总结刚刚上传文件的分类",
        attachments=[{"document_id": "doc-1"}],
    )

    assert plan.intent == "SUMMARIZE_CLASSIFICATIONS"
    assert [step.tool_name for step in plan.steps] == ["read-document-classifications"]
    assert plan.slots["route_source"] == "capability_router"


def test_llm_target_scope_is_preserved_from_backend_resolved_attachments():
    """LLM target_scope 只能作为意图说明，Planner 必须保留后端已解析附件范围。"""

    intent_plan = UserIntentPlan(
        intent="SUMMARIZE_CLASSIFICATIONS",
        user_goal="帮我总结上传的所有文件分类",
        needs_file_context=True,
        target_scope="all_conversation",
        required_capabilities=["read_document_classifications"],
        tool_plan_hint=["read-document-classifications"],
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="帮我总结上传的所有文件分类",
        attachments=[
            {"document_id": "doc-1", "context_scope": "all_conversation"},
            {"document_id": "doc-2", "context_scope": "all_conversation"},
        ],
    )

    assert plan.intent == "SUMMARIZE_CLASSIFICATIONS"
    assert plan.slots["target_scope"] == "all_conversation"
    assert plan.slots["resolved_scope"] == "all_conversation"
    assert plan.slots["document_ids"] == ["doc-1", "doc-2"]


def test_llm_classification_hint_is_overridden_for_explicit_file_classification():
    """LLM 若把“对上传文件进行分类”误判成读取历史分类，Planner 也必须重新解析并分类。"""

    intent_plan = UserIntentPlan(
        intent="SUMMARIZE_CLASSIFICATIONS",
        user_goal="对上传文件进行分类",
        needs_file_context=True,
        referenced_document_ids=["doc-1"],
        required_capabilities=["read_document_classifications"],
        skip_completed_ingest=True,
        tool_plan_hint=["read-document-classifications"],
        response_style="concise",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="对上传文件进行分类",
        attachments=[{"document_id": "doc-1"}],
    )

    assert plan.intent == "CLASSIFY_FILES"
    assert plan.slots["requested_outputs"] == ["classification", "receipt"]
    assert plan.selected_skills == [
        "llm-understanding",
        "document-text-extract",
        "document-classification",
        "change-report",
    ]
    assert [step.tool_name for step in plan.steps] == ["extract-document-text"]


def test_llm_classify_intent_without_keyword_uses_classify_plan():
    """工具入口必须以 LLM 结构化 intent 为主，不依赖用户原文包含“分类”关键词。"""

    intent_plan = UserIntentPlan(
        intent="CLASSIFY_FILES",
        user_goal="判断这些材料应该放到哪个目录",
        needs_file_context=True,
        referenced_document_ids=["doc-1"],
        target_scope="current_message",
        required_capabilities=["classify_files"],
        tool_plan_hint=[],
        response_style="concise",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="判断这些材料应该放到哪个目录",
        attachments=[{"document_id": "doc-1", "context_scope": "current_message"}],
    )

    assert plan.intent == "CLASSIFY_FILES"
    assert plan.slots["route_source"] == "capability_router"
    assert plan.slots["target_scope"] == "current_message"
    assert plan.slots["resolved_scope"] == "current_message"
    assert [step.tool_name for step in plan.steps] == ["extract-document-text"]


def test_planning_falls_back_when_llm_intent_schema_is_invalid():
    """LLM 意图响应异常时，planning 节点必须回退确定性 Planner，避免接口 500。"""

    class BrokenLLMIntentService:
        """模拟真实模型返回坏 JSON 后被服务层转成 LLMResponseError。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """始终抛出可兜底的 LLM 错误。"""

            raise LLMResponseError("LLM 意图响应不符合 schema。")

    class FakeRegistry:
        """测试用 Registry，返回稳定的正文解析结果。"""

        def invoke(self, tool_name, input_json):
            """模拟 extract-document-text 成功解析正文。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "document_id": input_json["document_id"],
                    "extraction_run_id": "run-fallback-summary",
                    "status": "COMPLETED",
                    "extractor": "plain-text",
                    "pages": [
                        {
                            "page_number": 1,
                            "text_preview": "这份文件说明了电子发票承诺事项和办理要求。",
                            "char_count": 24,
                        }
                    ],
                    "error": None,
                },
                status="COMPLETED",
            )

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=BrokenLLMIntentService(),
    )

    result = service.run_message(
        conversation_id="conv-llm-fallback",
        user_id="user-1",
        message_id="msg-llm-fallback",
        message="总结这个文件",
        attachments=[{"document_id": "doc-1"}],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "SUMMARIZE_DOCUMENTS"
    assert [step["tool_name"] for step in result.tool_plan["steps"]] == ["extract-document-text"]
    assert "内容总结" in (result.final_response or "")


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
            """返回稳定的文件信息读取意图。"""

            return UserIntentPlan(
                intent="READ_DOCUMENT_INSIGHTS",
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
        message="查看我刚才上传的文件信息",
        attachments=[{"document_id": "doc-1"}],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "READ_DOCUMENT_INSIGHTS"
    assert result.selected_skills == ["llm-understanding", "document-insight-read"]
    assert [item.tool_name for item in result.tool_invocations] == ["read-document-insights"]
    assert "document-convert" not in [item.tool_name for item in result.tool_invocations]


def test_deterministic_greeting_without_attachments_uses_general_chat():
    """无附件普通问候不应触发文件处理工具链。"""

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    service = AgentRuntimeService(llm_intent_service=DisabledLLMIntentService())

    result = service.run_message(
        conversation_id="conv-general-chat",
        user_id="user-1",
        message_id="msg-general-chat",
        message="你好",
        attachments=[],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "GENERAL_CHAT"
    assert result.selected_skills == ["llm-understanding"]
    assert [item.tool_name for item in result.tool_invocations] == ["intent-summary"]
    assert "AgentRun completed" not in (result.final_response or "")
    assert "文件" not in (result.final_response or "")


def test_capability_help_uses_fixed_capability_catalog():
    """用户询问系统能力时，必须读取固定能力清单，不能返回通用 fallback。"""

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    service = AgentRuntimeService(llm_intent_service=DisabledLLMIntentService())

    result = service.run_message(
        conversation_id="conv-capability-help",
        user_id="user-1",
        message_id="msg-capability-help",
        message="你可以做什么",
        attachments=[],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "CAPABILITY_HELP"
    assert result.selected_skills == ["capability-help"]
    assert [item.tool_name for item in result.tool_invocations] == ["read-agent-capabilities"]
    assert "我可以帮你完成这些文件工作" in (result.final_response or "")
    assert "我已收到" not in (result.final_response or "")


def test_llm_capability_help_hint_uses_fixed_capability_catalog():
    """LLM 判断用户询问能力时，也只能转为固定能力清单 Tool。"""

    intent_plan = UserIntentPlan(
        intent="CAPABILITY_HELP",
        user_goal="介绍系统能力",
        needs_file_context=False,
        referenced_document_ids=[],
        required_capabilities=["read_agent_capabilities"],
        tool_plan_hint=["read-agent-capabilities"],
        response_style="concise",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="你有什么功能",
        attachments=[],
    )

    assert plan.intent == "CAPABILITY_HELP"
    assert plan.selected_skills == ["capability-help"]
    assert [step.tool_name for step in plan.steps] == ["read-agent-capabilities"]


def test_classification_taxonomy_request_returns_fixed_catalog():
    """用户询问系统分类目录时，必须读取固定分类体系配置。"""

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    service = AgentRuntimeService(llm_intent_service=DisabledLLMIntentService())

    result = service.run_message(
        conversation_id="conv-taxonomy-catalog",
        user_id="user-1",
        message_id="msg-taxonomy-catalog",
        message="列出系统当前支持的文件分类目录",
        attachments=[],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "LIST_CLASSIFICATION_TAXONOMY"
    assert result.selected_skills == ["classification-taxonomy-read"]
    assert [item.tool_name for item in result.tool_invocations] == ["read-classification-taxonomy"]
    assert "学校文件归类表" in (result.final_response or "")
    assert "学校" in (result.final_response or "")
    assert "学院" in (result.final_response or "")


def test_llm_classification_taxonomy_hint_uses_fixed_catalog():
    """LLM 判断用户询问分类目录时，必须转为固定 taxonomy Tool。"""

    intent_plan = UserIntentPlan(
        intent="LIST_CLASSIFICATION_TAXONOMY",
        user_goal="列出系统分类目录",
        needs_file_context=False,
        referenced_document_ids=[],
        required_capabilities=["read_classification_taxonomy"],
        tool_plan_hint=["read-classification-taxonomy"],
        response_style="concise",
    )

    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message="系统当前支持哪些文件分类",
        attachments=[],
    )

    assert plan.intent == "LIST_CLASSIFICATION_TAXONOMY"
    assert plan.selected_skills == ["classification-taxonomy-read"]
    assert [step.tool_name for step in plan.steps] == ["read-classification-taxonomy"]


def test_llm_general_chat_returns_conversational_response():
    """LLM 识别为普通对话时，最终回复应是自然对话而不是工具审计信息。"""

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回普通聊天意图。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回不需要文件上下文的普通对话意图。"""

            return UserIntentPlan(
                intent="GENERAL_CHAT",
                user_goal=message,
                needs_file_context=False,
                referenced_document_ids=[],
                required_capabilities=[],
                tool_plan_hint=[],
                response_style="concise",
            )

    service = AgentRuntimeService(llm_intent_service=FakeLLMIntentService())

    result = service.run_message(
        conversation_id="conv-llm-general-chat",
        user_id="user-1",
        message_id="msg-llm-general-chat",
        message="你好",
        attachments=[],
    )

    assert result.status == "COMPLETED"
    assert result.intent == "GENERAL_CHAT"
    assert [item.tool_name for item in result.tool_invocations] == ["intent-summary"]
    assert "AgentRun completed" not in (result.final_response or "")
    assert "你好" in (result.final_response or "")


def test_summary_request_returns_content_summary_not_classification_receipt():
    """用户要求总结文章内容时，应返回内容总结而不是分类回执。"""

    class FakeRegistry:
        """测试用 Registry，返回稳定的正文解析结果。"""

        def invoke(self, tool_name, input_json):
            """模拟 extract-document-text 成功解析正文。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "document_id": input_json["document_id"],
                    "extraction_run_id": "run-summary",
                    "status": "COMPLETED",
                    "extractor": "plain-text",
                    "pages": [
                        {
                            "page_number": 1,
                            "text_preview": "本文围绕青年教师岗位锻炼安排展开，说明了选派对象、岗位职责、考核要求和组织保障。",
                            "char_count": 42,
                        }
                    ],
                    "error": None,
                },
                status="COMPLETED",
            )

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=DisabledLLMIntentService(),
    )

    result = service.run_message(
        conversation_id="conv-summary",
        user_id="user-1",
        message_id="msg-summary",
        message="读取上面上传的文件，给我讲解大概总结一下文章内容",
        attachments=[{"document_id": "doc-summary"}],
    )

    assert result.intent == "SUMMARIZE_DOCUMENTS"
    assert "内容总结" in (result.final_response or "")
    assert "青年教师岗位锻炼安排" in (result.final_response or "")
    assert "分类建议" not in (result.final_response or "")


def test_table_summary_request_uses_spreadsheet_analysis_plan():
    """用户要求汇总表格列或金额时，应走表格分析工具，不能回落到分类。"""

    class FakeRegistry:
        """测试用 Registry，返回稳定的表格分析结果。"""

        def invoke(self, tool_name, input_json):
            """模拟 analyze-spreadsheet 成功完成只读分析。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "kind": "spreadsheet_analysis",
                    "ok": True,
                    "document_id": input_json["document_id"],
                    "status": "COMPLETED",
                    "analysis": {
                        "title": "表格汇总结果",
                        "summary": "已完成表格汇总。",
                        "columns": ["姓名", "金额"],
                        "rows": [
                            {"姓名": "张三", "金额": 100},
                            {"姓名": "李四", "金额": 200},
                        ],
                    },
                    "result": [
                        {
                            "label": "合计",
                            "value": 300,
                        }
                    ],
                    "error": None,
                },
                status="COMPLETED",
            )

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=DisabledLLMIntentService(),
    )

    for message in ["汇总表中金额", "汇总表中关键词列"]:
        result = service.run_message(
            conversation_id="conv-table-summary",
            user_id="user-1",
            message_id=f"msg-table-summary-{message}",
            message=message,
            attachments=[{"document_id": "doc-table"}],
        )

        assert result.intent == "ANALYZE_SPREADSHEET"
        assert result.tool_plan["slots"]["requested_outputs"] == ["spreadsheet_analysis"]
        assert [item.tool_name for item in result.tool_invocations] == ["analyze-spreadsheet"]
        assert "分类建议" not in (result.final_response or "")
        assert "AgentRun completed" not in (result.final_response or "")


def test_llm_summary_with_text_extraction_returns_content_summary():
    """LLM 摘要意图即使调用正文解析，也必须返回内容总结而不是分类回执。"""

    class FakeRegistry:
        """测试用 Registry，返回稳定的正文解析结果。"""

        def invoke(self, tool_name, input_json):
            """模拟 extract-document-text 成功解析正文。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "document_id": input_json["document_id"],
                    "extraction_run_id": "run-llm-summary",
                    "status": "COMPLETED",
                    "extractor": "plain-text",
                    "pages": [
                        {
                            "page_number": 1,
                            "text_preview": "这份文件主要说明科研成果激励办法，包含适用范围、奖励类型、申报流程和审核要求。",
                            "char_count": 38,
                        }
                    ],
                    "error": None,
                },
                status="COMPLETED",
            )

    class FakeLLMIntentService:
        """测试用 LLM 服务，固定返回摘要正文解析意图。"""

        enabled = True

        def understand_user_request(self, *, message, attachments, context_documents):
            """返回需要读取正文后总结的用户意图。"""

            return UserIntentPlan(
                intent="SUMMARIZE_DOCUMENTS",
                user_goal=message,
                needs_file_context=True,
                referenced_document_ids=[attachments[0]["document_id"]],
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
        conversation_id="conv-llm-summary",
        user_id="user-1",
        message_id="msg-llm-summary",
        message="读取上面上传的文件，给我讲解大概总结一下文章内容",
        attachments=[{"document_id": "doc-llm-summary"}],
    )

    assert result.intent == "SUMMARIZE_DOCUMENTS"
    assert result.tool_plan["slots"]["requested_outputs"] == ["text", "summary", "receipt"]
    assert "内容总结" in (result.final_response or "")
    assert "科研成果激励办法" in (result.final_response or "")
    assert "分类建议" not in (result.final_response or "")


def test_summary_request_uses_document_summary_service():
    """摘要请求应优先使用完整正文 LLM 总结服务生成最终回复。"""

    class FakeRegistry:
        """测试用 Registry，返回稳定的正文解析结果。"""

        def invoke(self, tool_name, input_json):
            """模拟 extract-document-text 成功解析正文。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "document_id": input_json["document_id"],
                    "extraction_run_id": "run-summary-service",
                    "status": "COMPLETED",
                    "extractor": "plain-text",
                    "pages": [
                        {
                            "page_number": 1,
                            "text_preview": "只是一段预览",
                            "char_count": 6,
                        }
                    ],
                    "error": None,
                },
                status="COMPLETED",
            )

    class FakeSummaryService:
        """测试用文档总结服务，记录 Agent 传入的数据。"""

        def __init__(self):
            """初始化调用记录。"""

            self.calls = []

        def summarize_documents(self, *, document_results, tool_results, user_message):
            """返回固定 LLM 总结文本。"""

            self.calls.append(
                {
                    "document_results": document_results,
                    "tool_results": tool_results,
                    "user_message": user_message,
                }
            )
            return "LLM 总结：这是基于完整正文生成的讲解。"

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    summary_service = FakeSummaryService()
    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=DisabledLLMIntentService(),
        document_summary_service=summary_service,
    )

    result = service.run_message(
        conversation_id="conv-summary-service",
        user_id="user-1",
        message_id="msg-summary-service",
        message="读取上面上传的文件，给我总结文章内容",
        attachments=[{"document_id": "doc-summary-service"}],
    )

    assert result.final_response == "LLM 总结：这是基于完整正文生成的讲解。"
    assert summary_service.calls[0]["user_message"] == "读取上面上传的文件，给我总结文章内容"
    assert summary_service.calls[0]["document_results"][0]["document_id"] == "doc-summary-service"


def test_document_question_uses_text_extraction_and_llm_reader():
    """针对附件的问答请求必须先提取全文，再交给 LLM 文档阅读服务回答。"""

    class FakeRegistry:
        """测试用 Registry，返回稳定的正文解析结果。"""

        def invoke(self, tool_name, input_json):
            """模拟 extract-document-text 成功解析正文。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "document_id": input_json["document_id"],
                    "extraction_run_id": "run-answer-service",
                    "status": "COMPLETED",
                    "extractor": "plain-text",
                    "pages": [{"page_number": 1, "text_preview": "预览", "char_count": 2}],
                    "error": None,
                },
                status="COMPLETED",
            )

    class FakeReaderService:
        """测试用文档阅读服务，返回固定问答文本。"""

        def summarize_documents(self, *, document_results, tool_results, user_message):
            """返回固定答案。"""

            return "LLM 回答：文件要求申报人按时提交材料。"

    class DisabledLLMIntentService:
        """测试用关闭态 LLM 服务，强制走确定性 Planner。"""

        enabled = False

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=DisabledLLMIntentService(),
        document_summary_service=FakeReaderService(),
    )

    result = service.run_message(
        conversation_id="conv-answer-service",
        user_id="user-1",
        message_id="msg-answer-service",
        message="这个文件中申报人需要做什么？",
        attachments=[{"document_id": "doc-answer-service"}],
    )

    assert result.intent == "ANSWER_DOCUMENTS"
    assert result.tool_plan["slots"]["requested_outputs"] == ["text", "answer", "receipt"]
    assert [item.tool_name for item in result.tool_invocations] == ["extract-document-text"]
    assert result.final_response == "LLM 回答：文件要求申报人按时提交材料。"


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
                        "evidence_items": [
                            {
                                "type": "text_quote",
                                "page_number": 2,
                                "sheet_name": None,
                                "quote": "最后一页才出现教师职称申报材料。",
                                "signals": ["职称"],
                                "source": "rule",
                            }
                        ],
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
        "  依据：第 2 页：“最后一页才出现教师职称申报材料。”"
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
