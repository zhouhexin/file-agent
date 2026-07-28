"""对话文件检索 Planner 路由测试。

这些测试保护自然语言主题检索必须稳定进入正文检索链路，不能因为 LLM
意图波动或虚词误提取而退化成受管目录文件名列表。
"""

from app.modules.agent.planner import _managed_filename_contains_from_list_request
from app.modules.agent.planner import DeterministicPlanner, build_plan_from_user_intent
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.llm.schemas import UserIntentPlan


def test_deterministic_planner_routes_natural_language_file_search():
    """普通用户按主题找文件时应进入摘要优先检索，不要求提供目录。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-search",
        user_id="user-search",
        message_id="message-search",
        message="找我去年活动相关的奖学金材料",
        attachments=[],
    )

    assert plan.intent == "SEARCH_FILES"
    assert plan.selected_skills == ["file-search"]
    assert plan.steps[0].tool_name == "hybrid-search"
    assert plan.steps[0].input == {
        "query": "找我去年活动相关的奖学金材料",
        "document_ids": [],
    }


def test_relative_time_work_summary_routes_to_hybrid_search_not_directory_list():
    """“找去年的工作总结”必须交给可应用年份硬过滤的正文检索。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-relative-time",
        user_id="user-relative-time",
        message_id="message-relative-time",
        message="帮我找去年的工作总结",
        attachments=[],
    )

    assert plan.intent == "SEARCH_FILES"
    assert plan.steps[0].tool_name == "hybrid-search"


def test_deterministic_planner_routes_list_and_article_search_phrases():
    """“列出…文档”和“文章有哪些”都属于文件检索，不能回复普通闲聊占位语。"""

    for message in [
        "列出与科研有关的文档",
        "关于科研的文章有哪些",
        "关于科研的文档",
        "查找与科研有关的文档",
        "关于科研的文件",
        "查找与金海燕老师有关的文件",
        "查找关于金海燕老师的相关文件",
    ]:
        plan = DeterministicPlanner().plan(
            conversation_id="conversation-search-list",
            user_id="user-search-list",
            message_id="message-search-list",
            message=message,
            attachments=[],
        )

        assert plan.intent == "SEARCH_FILES"
        assert plan.steps[0].tool_name == "hybrid-search"


def test_deterministic_planner_routes_common_question_selectors_to_file_search():
    """用户无需使用固定“哪些”句式，常见文件选择问法都必须稳定进入正文检索。"""

    for message in [
        "哪个文件提到了公示期限",
        "哪些文档提到了任职通知",
        "哪份材料包含国家励志奖学金",
        "哪一份报告出现过公示期限",
        "哪几个文件涉及任职通知",
        "哪几份证明提到了家庭经济困难",
        "哪篇文章提及师德师风",
        "哪几篇文档包含科研诚信",
        "哪张表格出现了金海燕",
        "哪几张表格提到了资助金额",
    ]:
        plan = DeterministicPlanner().plan(
            conversation_id="conversation-question-selector",
            user_id="user-question-selector",
            message_id="message-question-selector",
            message=message,
            attachments=[],
        )

        assert plan.intent == "SEARCH_FILES"
        assert plan.steps[0].tool_name == "hybrid-search"


def test_semantic_search_phrase_does_not_extract_chinese_stopword_as_filename_filter():
    """“与某人有关”是正文语义检索，绝不能把“的”误当成文件名过滤条件。"""

    assert _managed_filename_contains_from_list_request("查找与金海燕老师有关的文件") is None


def test_llm_mode_bypasses_llm_for_deterministic_semantic_file_search(monkeypatch):
    """明确的自然语言文件检索必须在 LLM 前固定路由，避免同一句话得到不同 Tool 计划。"""

    # Runtime 构造仍需合法数据库配置，但本测试用 FakeRegistry，不会建立真实连接。
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/file_agent_test",
    )

    class BlockingLLMIntentService:
        """如果本测试调用 LLM，说明确定性检索预路由没有生效。"""

        enabled = True

        def understand_user_request(self, **kwargs):
            """禁止通过不稳定的模型输出决定主题检索 Tool。"""

            raise AssertionError("semantic file search should not call LLM intent routing")

    class FakeRegistry:
        """返回稳定的空检索结果，只验证路由边界。"""

        def invoke(self, tool_name, input_json):
            """记录受控 Tool 名称和输入，不访问真实数据库。"""

            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json={
                    "kind": "workspace_file_search",
                    "ok": True,
                    "query": input_json["query"],
                    "results": [],
                },
                status="COMPLETED",
            )

    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=BlockingLLMIntentService(),
    )

    result = service.run_message(
        conversation_id="conversation-stable-search",
        user_id="user-stable-search",
        message_id="message-stable-search",
        message="查找与金海燕老师有关的文件",
        attachments=[],
    )

    assert result.intent == "SEARCH_FILES"
    assert [item.tool_name for item in result.tool_invocations] == ["hybrid-search"]
    assert result.tool_invocations[0].input_json["query"] == "查找与金海燕老师有关的文件"


def test_llm_search_intent_is_converted_to_controlled_search_plan():
    """LLM 只能选择检索能力，最终 Tool 输入仍由应用层 schema 控制。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SEARCH_FILES",
            user_goal="查找干部考察结果报告",
            required_capabilities=["file_search"],
            tool_plan_hint=["hybrid-search"],
            managed_query="干部考察结果报告",
        ),
        message="帮我查找干部考察结果报告文件",
        attachments=[],
    )

    assert plan.intent == "SEARCH_FILES"
    assert plan.steps[0].tool_name == "hybrid-search"
    assert plan.steps[0].input["query"] == "干部考察结果报告"


def test_relative_time_search_overrides_llm_directory_list_misclassification():
    """相对时间检索不能因模型误判而退化为展示整个受管目录。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SEARCH_MANAGED_FILES",
            user_goal="找去年的工作总结",
            required_capabilities=["managed_file_list"],
            tool_plan_hint=["managed-file-list"],
            managed_root_key="wprk_files",
        ),
        message="帮我找去年的工作总结",
        attachments=[],
    )

    assert plan.intent == "SEARCH_FILES"
    assert plan.steps[0].tool_name == "hybrid-search"
