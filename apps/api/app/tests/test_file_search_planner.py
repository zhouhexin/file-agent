"""对话文件检索 Planner 路由测试。

这些测试保护自然语言主题检索必须稳定进入正文检索链路，不能因为 LLM
意图波动或虚词误提取而退化成受管目录文件名列表。
"""

from app.modules.agent.planner import (
    _has_plain_document_summary_intent,
    _managed_filename_contains_from_list_request,
    is_missing_generated_output_feedback,
    is_structured_image_extraction_request,
)
from app.modules.agent.planner import DeterministicPlanner, build_plan_from_user_intent
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.llm.schemas import UserIntentPlan
from app.modules.retrieval.semantic_plan import FileSearchSemanticPlan


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


def test_missing_table_feedback_does_not_expand_to_workspace_file_search():
    """上一轮未展示表格的否定反馈不能因“展示+表格”被当成全局检索。"""

    message = "我没有看到你展示的表格"
    plan = DeterministicPlanner().plan(
        conversation_id="conversation-missing-table",
        user_id="user-missing-table",
        message_id="message-missing-table",
        message=message,
        attachments=[],
    )

    assert is_missing_generated_output_feedback(message) is True
    assert plan.intent == "OUTPUT_NOT_VISIBLE_FEEDBACK"
    assert plan.steps[0].tool_name == "intent-summary"


def test_structured_image_request_detector_requires_table_structure_goal():
    assert not is_structured_image_extraction_request(
        "识别图中申请人、资助金额和申请日期，并以表格形式展示"
    )
    assert is_structured_image_extraction_request(
        "逐行识别图中所有记录，并保留原始表格的行列结构"
    )
    assert not is_structured_image_extraction_request("识别图片里的所有文字")
    assert not is_structured_image_extraction_request("查找工作总结表格")


def test_image_field_request_without_format_returns_values_after_forced_ocr():
    """列出图片字段已足以表达返回值目标，不应要求用户额外说“字段”或“表格”。"""

    message = "重新识别图片中的申请人、资助金额和使用情况登记"
    attachments = [
        {
            "document_id": "image-document-1",
            "filename": "20260824-182402.jpg",
            "content_type": "image/jpeg",
        }
    ]

    deterministic = DeterministicPlanner().plan(
        conversation_id="conversation-image-fields",
        user_id="user-image-fields",
        message_id="message-image-fields",
        message=message,
        attachments=attachments,
    )
    llm_converted = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="EXTRACT_AND_RECOGNIZE_IMAGE_CONTENT",
            user_goal=message,
            needs_file_context=True,
            target_scope="current_message",
            referenced_document_ids=["image-document-1"],
            required_capabilities=["document_read", "evidence_answer"],
            tool_plan_hint=["extract-document-text", "evidence-answer"],
        ),
        message=message,
        attachments=attachments,
    )

    assert not is_structured_image_extraction_request(message)
    for plan in (deterministic, llm_converted):
        assert plan.intent == "EVIDENCE_ANSWER"
        assert [step.tool_name for step in plan.steps] == [
            "extract-document-text",
            "evidence-answer",
        ]
        assert plan.steps[0].input["force_reprocess"] is True
        assert plan.slots["requested_outputs"] == ["answer", "references", "receipt"]
        assert plan.slots["show_evidence"] is False


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


def test_school_work_summary_builds_protected_grouped_semantic_plan():
    """“工作总结”必须作为完整主题，普通“学校”只表示业务域和排序偏好。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-school-summary",
        user_id="user-school-summary",
        message_id="message-school-summary",
        message="帮我找学校的工作总结",
        attachments=[],
    )

    semantic_plan = FileSearchSemanticPlan.model_validate(
        plan.steps[0].input["semantic_plan"]
    )
    assert [item.phrase for item in semantic_plan.core_topics] == ["工作总结"]
    assert semantic_plan.scope.organization_level == "ANY"
    assert semantic_plan.scope.organization_terms == []
    assert semantic_plan.preferred_results[0].organization_level == "UNIVERSITY"
    assert semantic_plan.group_by == [
        "organization_level",
        "business_topic",
        "year",
    ]


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


def test_llm_mode_routes_semantic_file_search_through_catalog_planner(monkeypatch):
    """正常 LLM 主路径应依据 Catalog 选择检索 Tool，关键词规则只保留作降级。"""

    # Runtime 构造仍需合法数据库配置，但本测试用 FakeRegistry，不会建立真实连接。
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@localhost:5432/file_agent_test",
    )

    class CatalogSearchIntentService:
        """验证请求级 Catalog 后返回受控检索意图。"""

        enabled = True
        call_count = 0

        def understand_user_request(
            self,
            *,
            message,
            attachments,
            context_documents,
            catalog_snapshot,
        ):
            """只选择 Catalog 中已启用的检索能力。"""

            self.call_count += 1
            assert "hybrid-search" in catalog_snapshot["enabled_tool_names"]
            assert "file-search" in catalog_snapshot["enabled_skill_ids"]
            return UserIntentPlan(
                intent="SEARCH_FILES",
                user_goal=message,
                required_capabilities=["file_search"],
                tool_plan_hint=["hybrid-search"],
                managed_query=message,
            )

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

    llm_service = CatalogSearchIntentService()
    service = AgentRuntimeService(
        registry_factory=lambda db, user_id: FakeRegistry(),
        llm_intent_service=llm_service,
    )

    result = service.run_message(
        conversation_id="conversation-stable-search",
        user_id="user-stable-search",
        message_id="message-stable-search",
        message="查找与金海燕老师有关的文件",
        attachments=[],
    )

    assert result.intent == "SEARCH_FILES"
    assert llm_service.call_count == 1
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


def test_llm_search_plan_is_passed_to_tool_after_schema_validation():
    """LLM 选择关键词时只能通过严格语义计划进入检索 Tool。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan.model_validate(
            {
                "intent": "SEARCH_FILES",
                "user_goal": "查找学校工作总结",
                "required_capabilities": ["file_search"],
                "tool_plan_hint": ["hybrid-search"],
                "managed_query": "学校的工作总结",
                "file_search_plan": {
                    "core_topics": [
                        {
                            "phrase": "工作总结",
                            "required": True,
                            "match_mode": "EXACT_PHRASE",
                        }
                    ],
                    "scope": {
                        "type": "CURRENT_SCHOOL_WORKSPACE",
                        "organization_level": "ANY",
                        "organization_terms": [],
                    },
                    "preferred_results": [
                        {"organization_level": "UNIVERSITY", "boost": 1.0}
                    ],
                    "group_by": ["organization_level", "year"],
                    "response_style": "GROUPED_FILE_LIST",
                },
            }
        ),
        message="帮我找学校的工作总结",
        attachments=[],
    )

    semantic_plan = plan.steps[0].input["semantic_plan"]
    assert semantic_plan["core_topics"][0]["phrase"] == "工作总结"
    assert "工作" not in [
        item["phrase"] for item in semantic_plan["core_topics"]
    ]


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


def test_semantic_topic_search_overrides_llm_directory_list_misclassification():
    """模型误报目录列表时，普通主题检索仍必须进入 hybrid-search。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="LIST_MANAGED_FILES",
            user_goal="列出涉及劳务费发放的文件",
            required_capabilities=["managed_file_list"],
            tool_plan_hint=["managed-file-list"],
            managed_root_key="wprk_files",
        ),
        message="列出涉及劳务费发放的文件",
        attachments=[],
    )

    assert plan.intent == "SEARCH_FILES"
    assert plan.steps[0].tool_name == "hybrid-search"


def test_explicit_year_file_search_overrides_llm_summary_misclassification():
    """“找 2025 年计算机学院的工作总结”不能被误当成单文件总结。"""

    plan = build_plan_from_user_intent(
        intent_plan=UserIntentPlan(
            intent="SUMMARIZE_MANAGED_FILE",
            user_goal="找 2025 年计算机学院的工作总结",
            required_capabilities=["managed_file_read"],
            tool_plan_hint=["managed-file-read-document"],
        ),
        message="帮我找2025年计算机学院的工作总结",
        attachments=[],
    )

    assert plan.intent == "SEARCH_FILES"
    assert plan.steps[0].tool_name == "hybrid-search"
    semantic_plan = FileSearchSemanticPlan.model_validate(
        plan.steps[0].input["semantic_plan"]
    )
    assert [
        item.phrase for item in semantic_plan.scope.organization_terms
    ] == ["计算机学院"]


def test_work_summary_search_is_not_treated_as_document_summary():
    """“找……工作总结”中的“总结”是文件主题，不能触发正文总结。"""

    message = "找2025年计算机学院的工作总结"

    assert not _has_plain_document_summary_intent(
        message=message,
        lowered=message.lower(),
    )


def test_fuzzy_work_summary_request_enters_candidate_resolution():
    """未写完整文件名的总结请求应先召回候选，不能直接要求用户重新附加文件。"""

    plan = DeterministicPlanner().plan(
        conversation_id="conversation-summary-candidates",
        user_id="user-summary-candidates",
        message_id="message-summary-candidates",
        message="总结2025年计算机学院的工作总结",
        attachments=[],
    )

    assert plan.intent == "EVIDENCE_ANSWER"
    assert plan.steps[0].tool_name == "evidence-answer"
    assert plan.steps[0].input["document_ids"] == []
    assert plan.steps[0].input["answer_mode"] == "AUTO"
