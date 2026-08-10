"""自然语言文件任务的 Adaptive Planner 规划执行回归矩阵。

这些测试不枚举学校业务关键词，而是保护三类共性闭环：先发现文件再读取证据、
先发现文件再解释分类，以及零结果后调整语义条件重新检索。所有模型与 Tool 都
使用 deterministic fake，不依赖外部服务。
"""

from __future__ import annotations

import pytest

from app.core import config
from app.modules.agent.planner_contracts import (
    PlannerDecision,
    PlannerScope,
    ToolPlan,
    ToolStep,
)
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry


@pytest.fixture
def adaptive_environment(monkeypatch, tmp_path):
    """启用百分百 Adaptive 灰度，并在测试结束后清理配置缓存。"""

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent",
    )
    monkeypatch.setenv("ADAPTIVE_PLANNER_MODE", "enabled")
    monkeypatch.setenv("ADAPTIVE_PLANNER_ROLLOUT_PERCENT", "100")
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


class _LegacyMustNotRun:
    """Adaptive 决策合法时禁止静默回退旧 Planner。"""

    enabled = True

    def understand_user_request(self, **_kwargs):
        """错误调用 Legacy 时立即让测试失败。"""

        raise AssertionError("合法 Adaptive 决策不应回退 Legacy Planner")


def _search_decision(*, message: str, query: str, step_id: str) -> PlannerDecision:
    """生成只执行一次发现型检索的受控计划。"""

    return PlannerDecision(
        decision_type="TOOL_PLAN",
        intent="SEARCH_FILES",
        user_goal=message,
        selected_skill_ids=["file-search"],
        scope=PlannerScope(source="workspace"),
        tool_plan=ToolPlan(
            plan_id=f"plan-{step_id}",
            steps=[
                ToolStep(
                    step_id=step_id,
                    skill_id="file-search",
                    tool_name="hybrid-search",
                    literal_input={"query": query},
                )
            ],
        ),
    )


def test_natural_language_search_can_continue_to_evidence_answer(
    adaptive_environment,
):
    """需要正文事实的请求应先查文件，再用真实命中 ID 进入证据回答。"""

    class SearchThenAnswerPlanner:
        """根据安全观察在检索和证据回答之间切换。"""

        enabled = True

        def __init__(self):
            """保存每轮观察，验证模型只接收脱敏投影。"""

            self.observations = []

        def decide(self, **kwargs):
            """第一轮检索，第二轮读取命中文件证据。"""

            observation = kwargs["observation"]
            self.observations.append(observation)
            if observation is None:
                return _search_decision(
                    message=kwargs["message"],
                    query="未来五年规划 重点任务",
                    step_id="search-planning-files",
                )
            document_ids = observation["results"][0]["document_ids"]
            return PlannerDecision(
                decision_type="TOOL_PLAN",
                intent="EVIDENCE_ANSWER",
                user_goal=kwargs["message"],
                selected_skill_ids=["evidence-answer"],
                scope=PlannerScope(
                    source="tool_observation",
                    document_ids=document_ids,
                ),
                tool_plan=ToolPlan(
                    plan_id="answer-matched-files",
                    steps=[
                        ToolStep(
                            step_id="answer",
                            skill_id="evidence-answer",
                            tool_name="evidence-answer",
                            literal_input={
                                "question": kwargs["message"],
                                "document_ids": document_ids,
                                "answer_mode": "FOCUSED",
                            },
                        )
                    ],
                ),
            )

    class SearchAnswerRegistry(ToolRegistry):
        """返回稳定的搜索与证据回答结果。"""

        def __init__(self):
            """初始化生产 Catalog 和调用记录。"""

            super().__init__()
            self.calls = []

        def invoke(self, name, input_json):
            """按 Tool 名称返回符合各自输出 schema 的结果。"""

            self.calls.append((name, input_json))
            if name == "hybrid-search":
                return ToolInvocationRecord(
                    tool_name=name,
                    input_json=input_json,
                    output_json={
                        "kind": "workspace_file_search",
                        "ok": True,
                        "query": input_json["query"],
                        "total_returned": 1,
                        "results": [
                            {
                                "document_id": "doc-five-year-plan",
                                "filename": "未来五年规划.docx",
                            }
                        ],
                        "document_ids": ["doc-five-year-plan"],
                        "effective_conditions": [
                            {
                                "label": "主题",
                                "value": "未来五年规划及重点任务",
                                "condition_type": "semantic",
                                "status": "APPLIED",
                                "source": "backend",
                            }
                        ],
                        "index_status": "READY",
                        "result_status": "MATCHED",
                        "available_next_actions": ["READ_MATCHED_DOCUMENTS"],
                    },
                    status="COMPLETED",
                )
            return ToolInvocationRecord(
                tool_name=name,
                input_json=input_json,
                output_json={
                    "kind": "evidence_answer",
                    "ok": True,
                    "status": "SUPPORTED",
                    "answer": "规划重点包括学生发展支持和教学条件建设。",
                    "references": [
                        {
                            "document_id": "doc-five-year-plan",
                            "filename": "未来五年规划.docx",
                        }
                    ],
                },
                status="COMPLETED",
            )

    planner = SearchThenAnswerPlanner()
    registry = SearchAnswerRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
        llm_intent_service=_LegacyMustNotRun(),
        adaptive_planner_service=planner,
    ).run_message(
        conversation_id="conv-natural-answer",
        user_id="user-natural-answer",
        message_id="msg-natural-answer",
        message="找出未来五年规划文件并说明其中最重要的任务",
    )

    assert [name for name, _input in registry.calls] == [
        "hybrid-search",
        "evidence-answer",
    ]
    assert registry.calls[1][1]["document_ids"] == ["doc-five-year-plan"]
    assert len(planner.observations) == 2
    assert "规划重点包括" in (result.final_response or "")
    assert result.search_context["attempts"][0]["result_count"] == 1


def test_natural_language_search_can_continue_to_classification_evidence(
    adaptive_environment,
):
    """分类原因请求应读取当前分类事实，而不是让模型根据文件名猜测。"""

    class SearchThenClassificationPlanner:
        """先确定文件，再读取当前版本分类证据。"""

        enabled = True

        def decide(self, **kwargs):
            """根据是否已有搜索观察选择下一 Tool。"""

            observation = kwargs["observation"]
            if observation is None:
                return _search_decision(
                    message=kwargs["message"],
                    query="学生工作实施建议",
                    step_id="search-classified-file",
                )
            document_ids = observation["results"][0]["document_ids"]
            return PlannerDecision(
                decision_type="TOOL_PLAN",
                intent="SUMMARIZE_CLASSIFICATIONS",
                user_goal=kwargs["message"],
                selected_skill_ids=["document-classification"],
                scope=PlannerScope(
                    source="tool_observation",
                    document_ids=document_ids,
                ),
                tool_plan=ToolPlan(
                    plan_id="read-classification-evidence",
                    steps=[
                        ToolStep(
                            step_id="read-classifications",
                            skill_id="document-classification",
                            tool_name="read-document-classifications",
                            literal_input={"document_ids": document_ids},
                        )
                    ],
                ),
            )

    class SearchClassificationRegistry(ToolRegistry):
        """返回搜索命中和带页码的分类建议。"""

        def __init__(self):
            """初始化调用记录。"""

            super().__init__()
            self.calls = []

        def invoke(self, name, input_json):
            """生成符合 Catalog 契约的 deterministic Tool 输出。"""

            self.calls.append((name, input_json))
            if name == "hybrid-search":
                return ToolInvocationRecord(
                    tool_name=name,
                    input_json=input_json,
                    output_json={
                        "kind": "workspace_file_search",
                        "ok": True,
                        "query": input_json["query"],
                        "total_returned": 1,
                        "results": [
                            {
                                "document_id": "doc-student-work",
                                "filename": "学生工作实施建议.docx",
                            }
                        ],
                        "document_ids": ["doc-student-work"],
                        "effective_conditions": [
                            {
                                "label": "文件主题",
                                "value": "学生工作实施建议",
                                "condition_type": "semantic",
                                "status": "APPLIED",
                                "source": "backend",
                            }
                        ],
                        "index_status": "READY",
                        "result_status": "MATCHED",
                        "available_next_actions": ["READ_MATCHED_DOCUMENTS"],
                    },
                    status="COMPLETED",
                )
            return ToolInvocationRecord(
                tool_name=name,
                input_json=input_json,
                output_json={
                    "ok": True,
                    "version_scope": "CURRENT_WORKING_COPY",
                    "documents": [
                        {
                            "document_id": "doc-student-work",
                            "filename": "学生工作实施建议.docx",
                            "status": "COMPLETED",
                            "categories": [
                                {
                                    "name": "学生工作",
                                    "category_path": ["学校", "学生工作"],
                                    "confidence": 0.91,
                                    "evidence_items": [
                                        {
                                            "type": "text_quote",
                                            "page_number": 2,
                                            "quote": "完善学生教育管理与服务保障机制。",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                },
                status="COMPLETED",
            )

    registry = SearchClassificationRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
        llm_intent_service=_LegacyMustNotRun(),
        adaptive_planner_service=SearchThenClassificationPlanner(),
    ).run_message(
        conversation_id="conv-natural-classification",
        user_id="user-natural-classification",
        message_id="msg-natural-classification",
        message="找到那份实施建议，说明它为什么被归到学生工作",
    )

    assert [name for name, _input in registry.calls] == [
        "hybrid-search",
        "read-document-classifications",
    ]
    assert "置信度：0.91" in (result.final_response or "")
    assert "第 2 页" in (result.final_response or "")
    assert "完善学生教育管理" in (result.final_response or "")


def test_natural_language_zero_result_can_refine_search_once(
    adaptive_environment,
):
    """首轮零结果时可调整语义条件再查，最终采用最后一次成功结果。"""

    class RefiningPlanner:
        """根据结果数量决定调整查询或结束。"""

        enabled = True

        def decide(self, **kwargs):
            """最多产生两次不同查询，命中后 FINISH。"""

            observation = kwargs["observation"]
            if observation is None:
                return _search_decision(
                    message=kwargs["message"],
                    query="未来五年计划",
                    step_id="search-strict",
                )
            if observation["results"][0]["result_count"] == 0:
                return _search_decision(
                    message=kwargs["message"],
                    query="五年发展规划 规划纲要",
                    step_id="search-expanded",
                )
            return PlannerDecision(
                decision_type="FINISH",
                intent="SEARCH_FILES",
                user_goal=kwargs["message"],
                selected_skill_ids=["file-search"],
                scope=PlannerScope(source="tool_observation"),
            )

    class RefiningRegistry(ToolRegistry):
        """第一轮返回零结果，第二轮返回真实命中。"""

        def __init__(self):
            """初始化调用记录。"""

            super().__init__()
            self.calls = []

        def invoke(self, name, input_json):
            """按查询内容返回不同检索状态。"""

            self.calls.append(input_json["query"])
            matched = "规划纲要" in input_json["query"]
            return ToolInvocationRecord(
                tool_name=name,
                input_json=input_json,
                output_json={
                    "kind": "workspace_file_search",
                    "ok": True,
                    "query": input_json["query"],
                    "total_returned": 1 if matched else 0,
                    "results": (
                        [
                            {
                                "document_id": "doc-expanded-result",
                                "filename": "五年发展规划纲要.docx",
                            }
                        ]
                        if matched
                        else []
                    ),
                    "document_ids": ["doc-expanded-result"] if matched else [],
                    "effective_conditions": [
                        {
                            "label": "主题",
                            "value": input_json["query"],
                            "condition_type": "semantic",
                            "status": "RELAXED" if matched else "APPLIED",
                            "source": "backend",
                        }
                    ],
                    "index_status": "READY",
                    "result_status": "MATCHED" if matched else "ZERO_RESULTS",
                    "available_next_actions": (
                        ["FINISH_WITH_RESULTS"] if matched else ["REFINE_SEARCH"]
                    ),
                },
                status="COMPLETED",
            )

    registry = RefiningRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
        llm_intent_service=_LegacyMustNotRun(),
        adaptive_planner_service=RefiningPlanner(),
    ).run_message(
        conversation_id="conv-natural-refine",
        user_id="user-natural-refine",
        message_id="msg-natural-refine",
        message="帮我找未来五年发展方向相关文件",
    )

    assert registry.calls == ["未来五年计划", "五年发展规划 规划纲要"]
    assert "五年发展规划纲要.docx" in (result.final_response or "")
    assert [item["result_count"] for item in result.search_context["attempts"]] == [
        0,
        1,
    ]
