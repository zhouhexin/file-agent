"""对话文件检索 Planner 路由测试。"""

from app.modules.agent.planner import DeterministicPlanner, build_plan_from_user_intent
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


def test_deterministic_planner_routes_list_and_article_search_phrases():
    """“列出…文档”和“文章有哪些”都属于文件检索，不能回复普通闲聊占位语。"""

    for message in ["列出与科研有关的文档", "关于科研的文章有哪些"]:
        plan = DeterministicPlanner().plan(
            conversation_id="conversation-search-list",
            user_id="user-search-list",
            message_id="message-search-list",
            message=message,
            attachments=[],
        )

        assert plan.intent == "SEARCH_FILES"
        assert plan.steps[0].tool_name == "hybrid-search"


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
