"""Adaptive Planner 契约、Catalog、结果绑定和能力建议安全边界测试。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from pydantic import BaseModel, ConfigDict

from app.db.base import Base
from app.db.models import (
    AgentRun,
    CapabilitySuggestion,
    Conversation,
    Document,
    DocumentCategorySuggestion,
    DocumentClassificationRun,
    DocumentInsight,
    DocumentVersion,
    Message,
    PlannerShadowComparison,
    User,
    WorkingCopy,
)
from app.modules.agent.binding_resolver import (
    MAX_BOUND_ARRAY_ITEMS,
    ToolBindingError,
    ToolResultBindingResolver,
)
from app.modules.agent.adaptive_planner import validate_and_convert_decision
from app.modules.agent.capability_suggestions import (
    CapabilitySuggestionRecordInput,
    CapabilitySuggestionService,
)
from app.modules.agent.catalog import AgentCatalogService
from app.modules.agent.planner import PlannerOutput, PlannerStep
from app.modules.agent.planner_contracts import (
    CapabilitySuggestionDraft,
    PlannerDecision,
    PlannerScope,
    ToolPlan,
    ToolStep,
)
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.repository import AgentRunRepository
from app.modules.agent.planner_shadow import PlannerShadowMetricsService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_contracts import ToolOutputValidationError
from app.modules.agent.tool_registry import (
    ToolDefinition,
    ToolRegistry,
    _with_search_binding_projection,
)
from app.modules.llm.client import LLMResponseError
from app.db.models import utcnow
from app.modules.classification.evidence_reader import (
    CurrentClassificationEvidenceReader,
)
from app.modules.file_lifecycle.shared_workspace import get_shared_workspace_id
from app.tests.helpers import clear_overrides, client_with_database


def _db_session():
    """创建包含全部 ORM 模型的隔离 SQLite 会话。"""

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_dynamic_catalog_loads_manifests_and_hides_internal_tool():
    """Catalog 必须来自真实 Registry/manifest，内部建议记录 Tool 不得暴露给 LLM。"""

    snapshot = AgentCatalogService(registry=ToolRegistry()).build_snapshot()

    assert len(snapshot["enabled_skill_ids"]) == 13
    assert "evidence-answer" in snapshot["enabled_skill_ids"]
    assert "capability-suggestion-record" not in snapshot["enabled_tool_names"]
    assert "document-convert" not in {
        item["name"] for item in ToolRegistry().list_tools(planner_only=False)
    }
    assert "extract-document-text" in snapshot["enabled_tool_names"]
    evidence_tool = next(
        item
        for item in snapshot["tools"]
        if item["name"] == "evidence-answer"
    )
    assert evidence_tool["input_schema"]["type"] == "object"
    assert evidence_tool["output_schema"]["type"] == "object"
    assert "answer" in evidence_tool["output_schema"]["properties"]
    classification_tool = next(
        item
        for item in snapshot["tools"]
        if item["name"] == "read-document-classifications"
    )
    assert "documents" in classification_tool["output_schema"]["properties"]
    assert len(snapshot["catalog_fingerprint"]) == 64


def test_planner_decision_branches_are_mutually_exclusive():
    """PlannerDecision 不得让直接回复同时夹带 ToolPlan。"""

    decision = PlannerDecision(
        decision_type="DIRECT_RESPONSE",
        intent="GENERAL_CHAT",
        user_goal="打个招呼",
        scope=PlannerScope(),
        direct_response="你好。",
    )

    assert decision.tool_plan is None
    assert decision.direct_response == "你好。"


def test_planner_finish_decision_contains_no_unverified_answer():
    """FINISH 只能结束规划循环，最终文件事实仍由后端聚合 Tool 结果。"""

    decision = PlannerDecision(
        decision_type="FINISH",
        intent="SEARCH_FILES",
        user_goal="查找未来五年规划文件",
        selected_skill_ids=["file-search"],
        scope=PlannerScope(source="tool_observation"),
    )

    assert decision.tool_plan is None
    assert decision.direct_response is None
    assert decision.clarification is None


def test_tool_plan_rejects_duplicate_or_forward_step_bindings():
    """ToolPlan 必须拒绝重复步骤和对尚未执行步骤的前向绑定。"""

    with pytest.raises(ValueError, match="duplicate step_id"):
        ToolPlan(
            plan_id="duplicate-plan",
            steps=[
                ToolStep(
                    step_id="same",
                    skill_id="file-search",
                    tool_name="hybrid-search",
                ),
                ToolStep(
                    step_id="same",
                    skill_id="file-search",
                    tool_name="hybrid-search",
                ),
            ],
        )

    with pytest.raises(
        ValueError,
        match="binding source must reference a previous step",
    ):
        ToolPlan(
            plan_id="forward-plan",
            steps=[
                ToolStep(
                    step_id="answer",
                    skill_id="evidence-answer",
                    tool_name="evidence-answer",
                    bindings=[
                        {
                            "target_field": "document_ids",
                            "source_step_id": "future-search",
                            "source_field": "document_ids",
                        }
                    ],
                ),
                ToolStep(
                    step_id="future-search",
                    skill_id="file-search",
                    tool_name="hybrid-search",
                ),
            ],
        )


def test_adaptive_scope_rejects_document_ids_outside_backend_context():
    """即使 ID 只出现在 PlannerScope，后端也必须拒绝未授权文件。"""

    registry = ToolRegistry()
    snapshot = AgentCatalogService(registry=registry).build_snapshot()
    decision = PlannerDecision(
        decision_type="DIRECT_RESPONSE",
        intent="GENERAL_CHAT",
        user_goal="解释这个文件",
        scope=PlannerScope(document_ids=["invented-document"]),
        direct_response="这是一个文件。",
    )

    with pytest.raises(LLMResponseError, match="未授权文件范围"):
        validate_and_convert_decision(
            decision=decision,
            registry=registry,
            catalog_snapshot=snapshot,
            attachments=[],
            context_documents=[],
        )


def test_adaptive_direct_response_rejects_authorized_file_fact():
    """即使文件 ID 已授权，模型也不能用 DIRECT_RESPONSE 绕过证据 Tool。"""

    registry = ToolRegistry()
    snapshot = AgentCatalogService(registry=registry).build_snapshot()
    decision = PlannerDecision(
        decision_type="DIRECT_RESPONSE",
        intent="EVIDENCE_ANSWER",
        user_goal="说明附件的主要内容",
        scope=PlannerScope(document_ids=["doc-authorized"]),
        direct_response="附件主要介绍了学生工作。",
    )

    with pytest.raises(LLMResponseError, match="不能通过 DIRECT_RESPONSE 回答文件事实"):
        validate_and_convert_decision(
            decision=decision,
            registry=registry,
            catalog_snapshot=snapshot,
            attachments=[{"document_id": "doc-authorized"}],
            context_documents=[],
        )


def test_adaptive_finish_requires_prior_tool_observation():
    """第一轮不得直接 FINISH，否则自然语言任务会在没有业务结果时结束。"""

    registry = ToolRegistry()
    snapshot = AgentCatalogService(registry=registry).build_snapshot()
    decision = PlannerDecision(
        decision_type="FINISH",
        intent="SEARCH_FILES",
        user_goal="查找未来五年规划",
        selected_skill_ids=["file-search"],
        scope=PlannerScope(source="tool_observation"),
    )

    with pytest.raises(LLMResponseError, match="FINISH 缺少 Tool 观察"):
        validate_and_convert_decision(
            decision=decision,
            registry=registry,
            catalog_snapshot=snapshot,
            attachments=[],
            context_documents=[],
            has_tool_observation=False,
        )


def test_binding_resolver_reads_completed_output_and_blocks_trusted_fields():
    """绑定只能读取成功步骤，且不能覆盖 agent_run_id 等受信任字段。"""

    resolver = ToolResultBindingResolver()
    resolved = resolver.resolve(
        literal_input={"question": "为什么"},
        bindings=[
            {
                "target_field": "document_ids",
                "source_step_id": "search",
                "source_field": "document_ids",
            }
        ],
        step_results={
            "search": {
                "status": "COMPLETED",
                "output": {"document_ids": ["doc-1"]},
            }
        },
    )

    assert resolved == {
        "question": "为什么",
        "document_ids": ["doc-1"],
    }
    try:
        resolver.resolve(
            literal_input={},
            bindings=[
                {
                    "target_field": "agent_run_id",
                    "source_step_id": "search",
                    "source_field": "document_ids",
                }
            ],
            step_results={
                "search": {
                    "status": "COMPLETED",
                    "output": {"document_ids": ["doc-1"]},
                }
            },
        )
    except ToolBindingError as exc:
        assert "受信任运行字段" in str(exc)
    else:
        raise AssertionError("绑定不得覆盖 agent_run_id")


def test_search_projection_exposes_only_deduplicated_document_ids():
    """检索 Tool 只从真实结果投影去重文件 ID，供后续证据回答绑定。"""

    class EmptyInput(BaseModel):
        """搜索投影包装器的最小测试输入。"""

        model_config = ConfigDict(extra="forbid")

    projected = _with_search_binding_projection(
        lambda _input: {
            "ok": True,
            "kind": "workspace_file_search",
            "results": [
                {"document_id": "doc-1"},
                {"document_id": "doc-1"},
                {"document_id": "doc-2"},
                {"working_copy_id": "copy-without-document"},
            ],
        }
    )(EmptyInput())

    assert projected["document_ids"] == ["doc-1", "doc-2"]


def test_search_projection_records_backend_effective_conditions():
    """LLM 解释出的查询条件必须标记实际生效情况，不能伪装成数据库硬过滤。"""

    from app.modules.agent.tool_schemas import SearchToolInput

    projected = _with_search_binding_projection(
        lambda _input: {
            "ok": True,
            "kind": "workspace_file_search",
            "query": "未来五年规划",
            "results": [],
            "_effective_scope": {
                "label": "文件范围",
                "value": "当前对话已确认的 2 个文件",
                "condition_type": "scope",
                "status": "APPLIED",
                "source": "backend",
            },
        }
    )(
        SearchToolInput(
            query="未来五年规划",
            interpreted_conditions=[
                {
                    "label": "时间范围",
                    "value": "未来五年",
                    "condition_type": "time",
                },
                {
                    "label": "审批状态",
                    "value": "已审批",
                    "condition_type": "other",
                },
            ],
        )
    )

    statuses = {
        item["label"]: item["status"]
        for item in projected["effective_conditions"]
    }
    assert statuses["时间范围"] == "SEMANTIC_ONLY"
    assert statuses["审批状态"] == "UNSUPPORTED"
    scope = next(
        item
        for item in projected["effective_conditions"]
        if item["label"] == "文件范围"
    )
    assert scope["value"] == "当前对话已确认的 2 个文件"
    assert "_effective_scope" not in projected
    assert projected["result_status"] == "ZERO_RESULTS"
    assert "REFINE_SEARCH" in projected["available_next_actions"]


def test_enabled_adaptive_search_observes_result_then_finishes(
    monkeypatch,
    tmp_path,
):
    """Adaptive 模式下检索完成后必须把安全观察交给 LLM，再由 FINISH 结束循环。"""

    from app.core import config
    from app.modules.llm.schemas import UserIntentPlan

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent",
    )
    monkeypatch.setenv("ADAPTIVE_PLANNER_MODE", "enabled")
    monkeypatch.setenv("ADAPTIVE_PLANNER_ROLLOUT_PERCENT", "100")
    config.get_settings.cache_clear()

    class LegacyMustNotRun:
        """Adaptive 成功时 Legacy LLM 不应参与可见规划。"""

        enabled = True

        def understand_user_request(self, **kwargs):
            """若被调用说明 Adaptive 闭环发生了错误回退。"""

            raise AssertionError("Adaptive 成功时不得调用 Legacy Planner")

    class SearchThenFinishPlanner:
        """第一轮检索，第二轮基于观察结束。"""

        enabled = True

        def __init__(self):
            """记录后端传入的安全观察。"""

            self.observations = []

        def decide(self, **kwargs):
            """返回 deterministic PlannerDecision。"""

            self.observations.append(kwargs["observation"])
            if kwargs["observation"] is None:
                return PlannerDecision(
                    decision_type="TOOL_PLAN",
                    intent="SEARCH_FILES",
                    user_goal=kwargs["message"],
                    selected_skill_ids=["file-search"],
                    scope=PlannerScope(source="workspace"),
                    tool_plan=ToolPlan(
                        plan_id="search-plan",
                        steps=[
                            ToolStep(
                                step_id="search",
                                skill_id="file-search",
                                tool_name="hybrid-search",
                                literal_input={
                                    "query": "未来五年规划",
                                    "interpreted_conditions": [
                                        {
                                            "label": "时间范围",
                                            "value": "未来五年",
                                            "condition_type": "time",
                                        }
                                    ],
                                },
                            )
                        ],
                    ),
                )
            return PlannerDecision(
                decision_type="FINISH",
                intent="SEARCH_FILES",
                user_goal=kwargs["message"],
                selected_skill_ids=["file-search"],
                scope=PlannerScope(source="tool_observation"),
            )

    class SearchRegistry(ToolRegistry):
        """复用生产 Catalog 元数据，只替换测试中的真实数据库搜索。"""

        def __init__(self):
            """初始化真实白名单并记录调用次数。"""

            super().__init__()
            self.calls = []

        def invoke(self, name, input_json):
            """返回符合安全观察契约的固定检索结果。"""

            self.calls.append((name, input_json))
            return ToolInvocationRecord(
                tool_name=name,
                input_json=input_json,
                output_json={
                    "kind": "workspace_file_search",
                    "ok": True,
                    "query": input_json["query"],
                    "total_returned": 1,
                    "partial": False,
                    "results": [
                        {
                            "document_id": "doc-observed",
                            "filename": "未来五年规划.docx",
                        }
                    ],
                    "document_ids": ["doc-observed"],
                    "effective_conditions": [
                        {
                            "label": "检索内容",
                            "value": input_json["query"],
                            "condition_type": "semantic",
                            "status": "APPLIED",
                            "source": "backend",
                        }
                    ],
                    "index_status": "READY",
                    "result_status": "MATCHED",
                    "available_next_actions": ["FINISH_WITH_RESULTS"],
                },
                status="COMPLETED",
            )

    adaptive = SearchThenFinishPlanner()
    registry = SearchRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
        llm_intent_service=LegacyMustNotRun(),
        adaptive_planner_service=adaptive,
    ).run_message(
        conversation_id="conv-adaptive-search",
        user_id="user-adaptive-search",
        message_id="msg-adaptive-search",
        message="帮我找未来五年规划相关文件",
    )

    assert len(registry.calls) == 1
    assert len(adaptive.observations) == 2
    assert adaptive.observations[1]["results"][0]["result_status"] == "MATCHED"
    assert adaptive.observations[1]["results"][0]["document_ids"] == [
        "doc-observed"
    ]
    assert result.status == "COMPLETED"
    assert result.search_context["attempts"][0]["result_count"] == 1


def test_binding_resolver_rejects_oversized_array():
    """前序 Tool 不得通过绑定把无上限数组注入后续 Tool。"""

    try:
        ToolResultBindingResolver().resolve(
            literal_input={},
            bindings=[
                {
                    "target_field": "document_ids",
                    "source_step_id": "search",
                    "source_field": "document_ids",
                }
            ],
            step_results={
                "search": {
                    "status": "COMPLETED",
                    "output": {
                        "document_ids": [
                            f"doc-{index}"
                            for index in range(MAX_BOUND_ARRAY_ITEMS + 1)
                        ]
                    },
                }
            },
        )
    except ToolBindingError as exc:
        assert "数组超过" in str(exc)
    else:
        raise AssertionError("超长绑定数组必须在目标 Tool 调用前被拒绝")


def test_registry_rejects_output_that_does_not_match_registered_schema():
    """handler 返回结构不符合 output_model 时必须按输出错误关闭式失败。"""

    class EmptyInput(BaseModel):
        """无输入 Tool 的测试 schema。"""

        model_config = ConfigDict(extra="forbid")

    class StrictOutput(BaseModel):
        """要求 answer 字段的测试输出 schema。"""

        model_config = ConfigDict(extra="forbid")

        ok: bool
        answer: str

    registry = ToolRegistry()
    registry._tools["strict-output-test"] = ToolDefinition(
        name="strict-output-test",
        version="1",
        description="测试严格输出",
        input_model=EmptyInput,
        output_model=StrictOutput,
        side_effects=False,
        risk_level="low",
        requires_confirmation=False,
        allowed_roles=["user"],
        allowed_skill_ids=["chat-intake"],
        writes=[],
        failure_strategy="fail",
        retry_policy="never",
        enabled=True,
        expose_to_planner=False,
        adaptive_ready=False,
        handler=lambda _input: {"ok": True},
    )

    try:
        registry.invoke("strict-output-test", {})
    except ToolOutputValidationError as exc:
        assert "output validation failed" in str(exc)
    else:
        raise AssertionError("不符合 output schema 的 Tool 结果必须被拒绝")


def test_langgraph_converts_tool_schema_error_to_audited_failure():
    """Tool 输入 schema 错误必须形成 FAILED 调用记录，不能冒泡为接口 500。"""

    class EmptyInput(BaseModel):
        """拒绝任何额外参数的严格输入 schema。"""

        model_config = ConfigDict(extra="forbid")

    class StrictOutput(BaseModel):
        """本测试不会执行 handler，但仍提供完整输出契约。"""

        model_config = ConfigDict(extra="forbid")

        ok: bool

    class InvalidInputPlanner:
        """生成一个会被 Registry schema 拒绝的确定性计划。"""

        def plan(self, **kwargs):
            """保留非法字段以验证 Dispatcher 的结构化降级。"""

            return PlannerOutput(
                intent="INVALID_TOOL_INPUT_TEST",
                user_goal=kwargs["message"],
                slots={},
                selected_skills=["chat-intake"],
                steps=[
                    PlannerStep(
                        step_id="invalid-input",
                        skill="chat-intake",
                        tool_name="strict-input-test",
                        input={"unexpected": True},
                    )
                ],
                evidence_policy={},
                confirmation_policy={},
            )

    registry = ToolRegistry()
    registry._tools["strict-input-test"] = ToolDefinition(
        name="strict-input-test",
        version="1",
        description="测试输入 schema 拒绝",
        input_model=EmptyInput,
        output_model=StrictOutput,
        side_effects=False,
        risk_level="low",
        requires_confirmation=False,
        allowed_roles=["user"],
        allowed_skill_ids=["chat-intake"],
        writes=[],
        failure_strategy="fail",
        retry_policy="never",
        enabled=True,
        expose_to_planner=False,
        adaptive_ready=False,
        handler=lambda _input: (_ for _ in ()).throw(
            AssertionError("输入非法时不得执行 handler")
        ),
    )

    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
    ).run_message(
        conversation_id="conv-invalid-input",
        user_id="user-invalid-input",
        message_id="msg-invalid-input",
        message="执行非法输入测试",
        planner=InvalidInputPlanner(),
    )

    assert result.tool_invocations[0].status == "FAILED"
    assert (
        result.tool_invocations[0].output_json["error"]["code"]
        == "TOOL_INPUT_VALIDATION_FAILED"
    )
    assert result.status == "NEEDS_REVIEW"


def test_langgraph_executes_bound_steps_one_at_a_time():
    """LangGraph 必须先记录前序结果，再解析下一 ToolStep 的输入绑定。"""

    class BoundPlanner:
        """返回两个存在显式结果依赖的 deterministic 计划。"""

        def plan(self, **kwargs):
            """第一步返回文件 ID，第二步消费该 ID。"""

            return PlannerOutput(
                intent="BOUND_TEST",
                user_goal=kwargs["message"],
                slots={},
                selected_skills=["chat-intake"],
                steps=[
                    PlannerStep(
                        step_id="search",
                        skill="chat-intake",
                        tool_name="first-tool",
                        input={},
                    ),
                    PlannerStep(
                        step_id="answer",
                        skill="chat-intake",
                        tool_name="second-tool",
                        input={"question": "为什么"},
                        bindings=[
                            {
                                "target_field": "document_ids",
                                "source_step_id": "search",
                                "source_field": "document_ids",
                            }
                        ],
                    ),
                ],
                evidence_policy={},
                confirmation_policy={},
            )

    class BoundRegistry:
        """记录步骤顺序和绑定后的真实输入。"""

        def __init__(self):
            self.calls = []

        def invoke(self, tool_name, input_json):
            """返回稳定结构化结果。"""

            self.calls.append((tool_name, input_json))
            output = (
                {"ok": True, "document_ids": ["doc-1"]}
                if tool_name == "first-tool"
                else {"ok": True, "answer": "测试回答"}
            )
            return ToolInvocationRecord(
                tool_name=tool_name,
                input_json=input_json,
                output_json=output,
                status="COMPLETED",
            )

    registry = BoundRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
    ).run_message(
        conversation_id="conv-bound",
        user_id="user-bound",
        message_id="msg-bound",
        message="执行绑定计划",
        planner=BoundPlanner(),
    )

    assert [name for name, _payload in registry.calls] == [
        "first-tool",
        "second-tool",
    ]
    assert registry.calls[1][1]["document_ids"] == ["doc-1"]
    assert len(result.tool_invocations) == 2


def test_langgraph_pauses_before_confirmation_required_step():
    """高风险步骤未确认时必须保留执行位置并停止，不能跳过后继续后续步骤。"""

    class ConfirmationPlanner:
        """返回一个需要确认的高风险步骤和一个不应被执行的后续步骤。"""

        def plan(self, **kwargs):
            """构造确认暂停场景。"""

            return PlannerOutput(
                intent="CONFIRMED_OPERATION",
                user_goal=kwargs["message"],
                slots={},
                selected_skills=["confirmed-file-action"],
                steps=[
                    PlannerStep(
                        step_id="confirmed-action",
                        skill="confirmed-file-action",
                        tool_name="confirmed-file-action",
                        input={
                            "operation_plan_id": "plan-1",
                            "confirmation_text": "确认执行",
                        },
                        requires_confirmation=True,
                        risk_level="high",
                    ),
                    PlannerStep(
                        step_id="must-not-run",
                        skill="chat-intake",
                        tool_name="intent-summary",
                        input={
                            "intent": "SHOULD_NOT_RUN",
                            "user_goal": "不应执行",
                        },
                    ),
                ],
                evidence_policy={},
                confirmation_policy={"operation_plan_required": True},
            )

    class RejectingRegistry:
        """任何实际 Tool 调用都意味着确认边界被绕过。"""

        def invoke(self, tool_name, input_json):
            """拒绝本测试中的一切 Tool 调用。"""

            raise AssertionError(f"未确认步骤不得调用 Tool: {tool_name}")

    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: RejectingRegistry(),
    ).run_message(
        conversation_id="conv-confirmation",
        user_id="user-confirmation",
        message_id="msg-confirmation",
        message="确认执行高风险操作",
        planner=ConfirmationPlanner(),
    )

    assert result.status == "WAITING_FOR_CONFIRMATION"
    assert result.tool_invocations == []
    assert "尚未生成可确认的操作计划" in (result.final_response or "")


def test_failed_graph_run_remains_persisted_after_transaction_rollback():
    """图执行异常时必须保留 FAILED AgentRun，同时回滚未提交的部分结果。"""

    class ExplodingPlanner:
        """生成一个会触发未预期 handler 异常的确定性计划。"""

        def plan(self, **kwargs):
            """返回单步测试计划。"""

            return PlannerOutput(
                intent="FAILURE_PERSISTENCE_TEST",
                user_goal=kwargs["message"],
                slots={},
                selected_skills=["chat-intake"],
                steps=[
                    PlannerStep(
                        step_id="explode",
                        skill="chat-intake",
                        tool_name="explode-tool",
                        input={},
                    )
                ],
                evidence_policy={},
                confirmation_policy={},
            )

    class ExplodingRegistry:
        """模拟 Tool handler 中未预期的程序异常。"""

        def invoke(self, tool_name, input_json):
            """抛出异常，验证请求事务不会吞掉 AgentRun。"""

            raise RuntimeError("simulated graph failure")

    db = _db_session()
    try:
        user = User(
            id="user-failed-run",
            username="failed-run-user",
            password_hash="hash",
            display_name="失败运行用户",
            role="user",
        )
        conversation = Conversation(
            id="conv-failed-run",
            user_id=user.id,
            title="失败运行",
        )
        message = Message(
            id="msg-failed-run",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="触发失败",
        )
        db.add_all([user, conversation, message])
        db.commit()

        with pytest.raises(RuntimeError, match="simulated graph failure"):
            AgentRuntimeService(
                registry_factory=lambda _db, _user_id: ExplodingRegistry(),
            ).run_message(
                conversation_id=conversation.id,
                user_id=user.id,
                message_id=message.id,
                message=message.content,
                planner=ExplodingPlanner(),
                db=db,
            )

        persisted = db.query(AgentRun).one()
        assert persisted.status == "FAILED"
        assert persisted.error_message == "simulated graph failure"
    finally:
        db.close()


def test_shadow_planner_does_not_change_legacy_visible_execution(
    monkeypatch,
    tmp_path,
):
    """Shadow 只生成并校验决策，用户可见执行仍由 Legacy Planner 决定。"""

    from app.core import config
    from app.modules.llm.schemas import UserIntentPlan

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent",
    )
    monkeypatch.setenv("ADAPTIVE_PLANNER_MODE", "shadow")
    monkeypatch.setenv("ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT", "100")
    config.get_settings.cache_clear()

    class LegacyIntentService:
        """Legacy Planner 固定选择能力清单读取 Tool。"""

        enabled = True

        def understand_user_request(
            self,
            *,
            message,
            attachments,
            context_documents,
            catalog_snapshot,
        ):
            """返回用户可见 Legacy 计划。"""

            return UserIntentPlan(
                intent="CAPABILITY_HELP",
                user_goal=message,
                required_capabilities=["read_agent_capabilities"],
                tool_plan_hint=["read-agent-capabilities"],
            )

    class ShadowAdaptivePlanner:
        """Adaptive Planner 固定提出另一条只读计划。"""

        enabled = True

        def __init__(self):
            """记录 Shadow 是否被调用。"""

            self.call_count = 0

        def decide(self, **kwargs):
            """返回 Catalog 内合法、但不得实际执行的搜索计划。"""

            self.call_count += 1
            return PlannerDecision(
                decision_type="TOOL_PLAN",
                intent="SEARCH_FILES",
                user_goal=kwargs["message"],
                selected_skill_ids=["file-search"],
                scope=PlannerScope(source="workspace"),
                tool_plan=ToolPlan(
                    plan_id="shadow-plan",
                    steps=[
                        ToolStep(
                            step_id="shadow-search",
                            skill_id="file-search",
                            tool_name="hybrid-search",
                            literal_input={"query": "测试"},
                        )
                    ],
                ),
            )

    class RecordingRegistry(ToolRegistry):
        """记录实际调用，验证 Shadow 不产生第二次 Tool 调用。"""

        def __init__(self):
            """初始化真实白名单和调用记录。"""

            super().__init__()
            self.calls = []

        def invoke(self, name, input_json):
            """记录后委托真实 Registry。"""

            self.calls.append(name)
            return super().invoke(name, input_json)

    adaptive = ShadowAdaptivePlanner()
    registry = RecordingRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
        llm_intent_service=LegacyIntentService(),
        adaptive_planner_service=adaptive,
    ).run_message(
        conversation_id="conv-shadow",
        user_id="user-shadow",
        message_id="msg-shadow",
        message="你能做什么",
    )

    assert adaptive.call_count == 1
    assert registry.calls == ["read-agent-capabilities"]
    assert result.intent == "CAPABILITY_HELP"
    assert result.status == "COMPLETED"


def test_enabled_adaptive_planner_failure_falls_back_to_legacy(
    monkeypatch,
    tmp_path,
):
    """Adaptive 校验失败时先使用 Legacy Planner，不能直接让消息入口失败。"""

    from app.core import config
    from app.modules.llm.client import LLMResponseError
    from app.modules.llm.schemas import UserIntentPlan

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://user:pass@127.0.0.1:5432/fileAgent",
    )
    monkeypatch.setenv("ADAPTIVE_PLANNER_MODE", "enabled")
    monkeypatch.setenv("ADAPTIVE_PLANNER_ROLLOUT_PERCENT", "100")
    config.get_settings.cache_clear()

    class LegacyDirectResponse:
        """Adaptive 失败后返回用户可见 Legacy 直接回复。"""

        enabled = True
        call_count = 0

        def understand_user_request(self, **kwargs):
            """返回不依赖文件事实的普通回复。"""

            self.call_count += 1
            return UserIntentPlan(
                intent="GENERAL_CHAT",
                user_goal=kwargs["message"],
                decision_type="DIRECT_RESPONSE",
                direct_response="已安全回退到现有规划链路。",
            )

    class FailingAdaptivePlanner:
        """模拟 Adaptive Planner schema 或网关失败。"""

        enabled = True
        call_count = 0

        def decide(self, **kwargs):
            """抛出可降级模型错误。"""

            self.call_count += 1
            raise LLMResponseError("invalid adaptive decision")

    legacy = LegacyDirectResponse()
    adaptive = FailingAdaptivePlanner()
    result = AgentRuntimeService(
        llm_intent_service=legacy,
        adaptive_planner_service=adaptive,
    ).run_message(
        conversation_id="conv-adaptive-fallback",
        user_id="user-adaptive-fallback",
        message_id="msg-adaptive-fallback",
        message="你好",
    )

    assert adaptive.call_count == 1
    assert legacy.call_count == 1
    assert result.status == "COMPLETED"
    assert result.final_response == "已安全回退到现有规划链路。"


def test_missing_capability_can_return_direct_response_without_placeholder_tool():
    """Catalog 缺少能力时应直接说明并记录建议，不执行 intent-summary 占位 Tool。"""

    from app.modules.llm.schemas import UserIntentPlan

    class MissingCapabilityIntentService:
        """模拟 LLM 判断当前 Catalog 无法完成用户目标。"""

        enabled = True

        def understand_user_request(self, **kwargs):
            """返回安全直接回复和管理员待评审建议。"""

            return UserIntentPlan(
                intent="CAPABILITY_UNAVAILABLE",
                user_goal=kwargs["message"],
                decision_type="DIRECT_RESPONSE",
                direct_response="当前还不支持读取该格式，可由管理员评估该能力建议。",
                capability_suggestions=[
                    CapabilitySuggestionDraft(
                        title="支持新格式",
                        missing_capability="读取测试格式",
                        reason="当前 Catalog 中没有对应 Tool",
                        confidence=0.9,
                    )
                ],
            )

    class SuggestionRecordingRegistry(ToolRegistry):
        """仅记录内部建议 Tool，禁止执行普通占位 Tool。"""

        def __init__(self):
            """初始化真实白名单与调用记录。"""

            super().__init__()
            self.calls: list[str] = []

        def invoke(self, name, input_json):
            """模拟内部建议写入失败，确认它不会阻断用户回复。"""

            self.calls.append(name)
            raise RuntimeError("suggestion storage unavailable")

    registry = SuggestionRecordingRegistry()
    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry,
        llm_intent_service=MissingCapabilityIntentService(),
    ).run_message(
        conversation_id="conv-missing-capability",
        user_id="user-missing-capability",
        message_id="msg-missing-capability",
        message="读取测试格式",
    )

    assert result.status == "COMPLETED"
    assert result.final_response == "当前还不支持读取该格式，可由管理员评估该能力建议。"
    assert registry.calls == ["capability-suggestion-record"]
    assert (
        result.tool_invocations[0].output_json["error"]["code"]
        == "CAPABILITY_SUGGESTION_RECORD_FAILED"
    )


def test_planner_shadow_metrics_only_aggregate_safe_comparison_fields():
    """Shadow 指标必须从脱敏比较表聚合，失败样本不能被排除。"""

    db = _db_session()
    try:
        user = User(
            id="user-shadow-metrics",
            username="shadow-metrics-user",
            password_hash="hash",
            display_name="Shadow 指标用户",
            role="admin",
        )
        conversation = Conversation(
            id="conv-shadow-metrics",
            user_id=user.id,
            title="Shadow 指标",
        )
        message = Message(
            id="msg-shadow-metrics",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="测试",
        )
        run = AgentRun(
            id="run-shadow-metrics",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
        )
        db.add_all([user, conversation, message, run])
        db.flush()
        now = utcnow()
        db.add_all(
            [
                PlannerShadowComparison(
                    id="shadow-current-success",
                    agent_run_id=run.id,
                    legacy_decision_type="TOOL_PLAN",
                    adaptive_decision_type="TOOL_PLAN",
                    scope_match=True,
                    risk_match=True,
                    confirmation_match=True,
                    adaptive_validation_status="COMPLETED",
                    catalog_fingerprint="a" * 64,
                    schema_version="planner-decision-v1",
                    created_at=now,
                ),
                PlannerShadowComparison(
                    id="shadow-current-failed",
                    agent_run_id=run.id,
                    legacy_decision_type="TOOL_PLAN",
                    adaptive_decision_type="CLARIFY",
                    scope_match=False,
                    risk_match=True,
                    confirmation_match=False,
                    adaptive_validation_status="FAILED",
                    adaptive_error_code="UNKNOWN_TOOL",
                    catalog_fingerprint="a" * 64,
                    schema_version="planner-decision-v1",
                    created_at=now + timedelta(seconds=2),
                ),
                PlannerShadowComparison(
                    id="shadow-old-catalog",
                    agent_run_id=run.id,
                    legacy_decision_type="TOOL_PLAN",
                    adaptive_decision_type="TOOL_PLAN",
                    scope_match=True,
                    risk_match=True,
                    confirmation_match=True,
                    adaptive_validation_status="COMPLETED",
                    catalog_fingerprint="b" * 64,
                    schema_version="planner-decision-v1",
                    created_at=now - timedelta(seconds=2),
                ),
            ]
        )
        db.flush()

        metrics = PlannerShadowMetricsService(db).summarize()

        assert metrics.catalog_fingerprint == "a" * 64
        assert metrics.schema_version == "planner-decision-v1"
        assert metrics.sample_count == 2
        assert metrics.validation_success_rate == 0.5
        assert metrics.decision_match_rate == 0.5
        assert metrics.scope_match_rate == 0.5
        assert metrics.risk_match_rate == 1
        assert metrics.confirmation_match_rate == 0.5
        assert metrics.adaptive_error_counts == {"UNKNOWN_TOOL": 1}
    finally:
        db.close()


def test_failed_shadow_generation_is_persisted_for_rollout_metrics():
    """Shadow 没有生成合法 decision 时也必须写入失败样本，防止成功率虚高。"""

    db = _db_session()
    try:
        user = User(
            id="user-shadow-failure",
            username="shadow-failure-user",
            password_hash="hash",
            display_name="Shadow 失败用户",
            role="admin",
        )
        conversation = Conversation(
            id="conv-shadow-failure",
            user_id=user.id,
            title="Shadow 失败",
        )
        message = Message(
            id="msg-shadow-failure",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="测试",
        )
        run = AgentRun(
            id="run-shadow-failure",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
        )
        db.add_all([user, conversation, message, run])
        db.flush()

        comparison = AgentRunRepository(db).record_shadow_comparison(
            run=run,
            state={
                "planner_schema_version": "planner-decision-v1",
                "catalog_snapshot": {
                    "catalog_fingerprint": "f" * 64,
                },
                "planner_decision": {
                    "decision_type": "TOOL_PLAN",
                    "intent": "SEARCH_FILES",
                    "scope": {"document_ids": []},
                    "tool_plan": {"steps": []},
                },
                "tool_plan": {"steps": []},
                "shadow_planner_decision": {
                    "validation_status": "FAILED",
                    "error_code": "LLMResponseError",
                    "decision": None,
                },
            },
        )

        assert comparison is not None
        assert comparison.adaptive_validation_status == "FAILED"
        assert comparison.adaptive_decision_type == "UNAVAILABLE"
        assert comparison.scope_match is False
        assert comparison.risk_match is False
        assert comparison.confirmation_match is False
    finally:
        db.close()


def test_capability_suggestion_is_deduplicated_and_never_becomes_active_skill():
    """相同能力缺口只累计次数，不得自动创建 Skill 或改变建议状态。"""

    db = _db_session()
    try:
        user = User(
            id="user-suggestion",
            username="suggestion-user",
            password_hash="hash",
            display_name="建议用户",
            role="user",
        )
        conversation = Conversation(
            id="conv-suggestion",
            user_id=user.id,
            title="建议测试",
        )
        message = Message(
            id="msg-suggestion",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="请读取加密 PDF",
        )
        run = AgentRun(
            id="run-suggestion",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
        )
        db.add_all([user, conversation, message, run])
        db.flush()
        payload = CapabilitySuggestionRecordInput(
            suggestions=[
                CapabilitySuggestionDraft(
                    title="支持加密 PDF",
                    missing_capability="读取用户提供密码的加密 PDF",
                    reason="当前只支持未加密 PDF",
                    confidence=0.9,
                    related_skill_ids=["file-ingest"],
                )
            ],
            user_goal="请读取加密 PDF",
            catalog_fingerprint="a" * 64,
            enabled_tool_names=["extract-document-text"],
            enabled_skill_ids=["file-ingest"],
        )
        service = CapabilitySuggestionService(db)
        service.record(
            payload=payload,
            user_id=user.id,
            agent_run_id=run.id,
        )
        differently_worded_payload = payload.model_copy(
            update={"user_goal": "能不能打开需要密码的 PDF 文件"}
        )
        service.record(
            payload=differently_worded_payload,
            user_id=user.id,
            agent_run_id=run.id,
        )

        suggestion = db.query(CapabilitySuggestion).one()
        assert suggestion.occurrence_count == 2
        assert suggestion.status == "NEW"
        assert suggestion.related_skill_ids_json == ["file-ingest"]
    finally:
        db.close()


def test_classification_evidence_reader_ignores_historical_version():
    """分类证据读取只能使用当前 DocumentVersion，不能被更新的旧版本运行污染。"""

    db = _db_session()
    try:
        user = User(
            id="user-classification-evidence",
            username="classification-evidence-user",
            password_hash="hash",
            display_name="证据用户",
            role="user",
        )
        conversation = Conversation(
            id="conv-classification-evidence",
            user_id=user.id,
            title="证据测试",
        )
        message = Message(
            id="msg-classification-evidence",
            conversation_id=conversation.id,
            user_id=user.id,
            role="user",
            content="为什么这样分类",
        )
        run = AgentRun(
            id="run-classification-evidence",
            conversation_id=conversation.id,
            message_id=message.id,
            user_id=user.id,
        )
        document = Document(
            id="doc-classification-evidence",
            user_id=user.id,
            original_filename="测试材料.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=100,
            sha256="a" * 64,
        )
        old_version = DocumentVersion(
            id="version-old",
            document_id=document.id,
            version_number=1,
            storage_path="old.docx",
            filename="旧版.docx",
            size_bytes=100,
            sha256="a" * 64,
        )
        current_version = DocumentVersion(
            id="version-current",
            document_id=document.id,
            version_number=2,
            storage_path="current.docx",
            filename="新版.docx",
            size_bytes=100,
            sha256="b" * 64,
        )
        current_run = DocumentClassificationRun(
            id="classification-current",
            agent_run_id=run.id,
            document_id=document.id,
            taxonomy_key="school",
            taxonomy_version="2",
            classifier_version="test",
            status="COMPLETED",
        )
        old_run = DocumentClassificationRun(
            id="classification-old",
            agent_run_id=run.id,
            document_id=document.id,
            taxonomy_key="school",
            taxonomy_version="1",
            classifier_version="test",
            status="COMPLETED",
        )
        db.add_all(
            [
                user,
                conversation,
                message,
                run,
                document,
                old_version,
                current_version,
                current_run,
                old_run,
            ]
        )
        db.flush()
        db.add_all(
            [
                DocumentCategorySuggestion(
                    classification_run_id=current_run.id,
                    document_id=document.id,
                    document_version_id=current_version.id,
                    category_id="student-work",
                    category_name="学生工作",
                    category_path_json=["学生工作"],
                    taxonomy_key="school",
                    taxonomy_version="2",
                    confidence=0.9,
                    status="SUGGESTED",
                    evidence_json=[
                        {
                            "type": "text_quote",
                            "page_number": 1,
                            "quote": "开展学生资助工作",
                            "signals": ["学生资助"],
                            "source": "document_pages",
                        }
                    ],
                    rank=1,
                ),
                DocumentCategorySuggestion(
                    classification_run_id=old_run.id,
                    document_id=document.id,
                    document_version_id=old_version.id,
                    category_id="research",
                    category_name="科研",
                    category_path_json=["科研"],
                    taxonomy_key="school",
                    taxonomy_version="1",
                    confidence=0.99,
                    status="SUGGESTED",
                    evidence_json=[],
                    rank=1,
                ),
            ]
        )
        db.flush()

        result = CurrentClassificationEvidenceReader(
            db=db,
            user_id=user.id,
        ).read(document_ids=[document.id])

        assert result[0]["document_version_id"] == current_version.id
        assert [item["name"] for item in result[0]["categories"]] == ["学生工作"]
        assert result[0]["categories"][0]["evidence_items"][0]["page_number"] == 1
    finally:
        db.close()


def test_read_tools_can_use_shared_active_working_copy_from_another_importer():
    """检索命中的共享文件必须可继续读取概览和分类，不能按导入者再次隔离。"""

    db = _db_session()
    try:
        requester = User(
            id="shared-read-requester",
            username="shared-read-requester",
            password_hash="hash",
            display_name="请求用户",
        )
        importer = User(
            id="shared-read-importer",
            username="shared-read-importer",
            password_hash="hash",
            display_name="导入用户",
        )
        document = Document(
            id="shared-read-document",
            user_id=importer.id,
            original_filename="导入时名称.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=100,
            sha256="c" * 64,
            ingest_status="READY",
        )
        version = DocumentVersion(
            id="shared-read-version",
            document_id=document.id,
            version_number=1,
            storage_path="working/shared-read.docx",
            filename="学生工作实施建议.docx",
            size_bytes=100,
            sha256=document.sha256,
        )
        unrelated_version = DocumentVersion(
            id="shared-read-unrelated-version",
            document_id=document.id,
            version_number=2,
            storage_path="legacy/unrelated.docx",
            filename="其他工作区旧副本.docx",
            size_bytes=100,
            sha256="e" * 64,
        )
        working_copy = WorkingCopy(
            id="shared-read-working-copy",
            working_copy_root_id="shared-read-root",
            workspace_id=get_shared_workspace_id(db),
            managed_file_id="shared-read-managed-file",
            document_id=document.id,
            current_version_id=version.id,
            relative_path="学生工作实施建议.docx",
            relative_path_hash="d" * 64,
            filename="学生工作实施建议.docx",
            extension=".docx",
            size_bytes=100,
            content_sha256=document.sha256,
            imported_source_sha256=document.sha256,
            status="ACTIVE",
        )
        unrelated_working_copy = WorkingCopy(
            id="shared-read-unrelated-working-copy",
            working_copy_root_id="shared-read-unrelated-root",
            workspace_id="legacy-user-workspace",
            managed_file_id="shared-read-unrelated-managed-file",
            document_id=document.id,
            current_version_id=unrelated_version.id,
            relative_path="其他工作区旧副本.docx",
            relative_path_hash="f" * 64,
            filename="其他工作区旧副本.docx",
            extension=".docx",
            size_bytes=100,
            content_sha256=unrelated_version.sha256,
            imported_source_sha256=unrelated_version.sha256,
            status="ACTIVE",
            updated_at=utcnow() + timedelta(days=1),
        )
        run = DocumentClassificationRun(
            id="shared-read-classification-run",
            document_id=document.id,
            agent_run_id="shared-read-agent-run",
            taxonomy_key="school",
            taxonomy_version="2",
            classifier_version="test",
            status="COMPLETED",
        )
        suggestion = DocumentCategorySuggestion(
            classification_run_id=run.id,
            document_id=document.id,
            document_version_id=version.id,
            category_id="student-work",
            category_name="学生工作",
            category_path_json=["学校", "学生工作"],
            taxonomy_key="school",
            taxonomy_version="2",
            confidence=0.91,
            status="SUGGESTED",
            evidence_json=[
                {
                    "type": "text_quote",
                    "page_number": 2,
                    "quote": "完善学生教育管理与服务保障机制。",
                    "signals": ["学生教育管理"],
                    "source": "document_pages",
                }
            ],
            rank=1,
        )
        insight = DocumentInsight(
            document_id=document.id,
            summary="文件说明学生教育管理工作安排。",
            keywords_json=["学生教育", "管理"],
            labels_json=["学生工作"],
        )
        owned_document = Document(
            id="shared-read-owned-document",
            user_id=requester.id,
            original_filename="本人上传材料.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            size_bytes=80,
            sha256="1" * 64,
            ingest_status="READY",
        )
        owned_version = DocumentVersion(
            id="shared-read-owned-version",
            document_id=owned_document.id,
            version_number=1,
            storage_path="uploads/owned.docx",
            filename=owned_document.original_filename,
            size_bytes=80,
            sha256=owned_document.sha256,
        )
        owned_run = DocumentClassificationRun(
            id="shared-read-owned-run",
            document_id=owned_document.id,
            agent_run_id="shared-read-agent-run",
            taxonomy_key="school",
            taxonomy_version="2",
            classifier_version="test",
            status="COMPLETED",
        )
        owned_suggestion = DocumentCategorySuggestion(
            classification_run_id=owned_run.id,
            document_id=owned_document.id,
            document_version_id=owned_version.id,
            category_id="teaching",
            category_name="教学",
            category_path_json=["学校", "教学"],
            taxonomy_key="school",
            taxonomy_version="2",
            confidence=0.88,
            status="SUGGESTED",
            evidence_json=[],
            rank=1,
        )
        db.add_all(
            [
                requester,
                importer,
                document,
                version,
                unrelated_version,
                working_copy,
                unrelated_working_copy,
                run,
                suggestion,
                insight,
                owned_document,
                owned_version,
                owned_run,
                owned_suggestion,
            ]
        )
        db.flush()

        registry = ToolRegistry(db=db, user_id=requester.id)
        classifications = registry.invoke(
            "read-document-classifications",
            {"document_ids": [document.id, owned_document.id]},
        ).output_json
        insights = registry.invoke(
            "read-document-insights",
            {"document_ids": [document.id]},
        ).output_json

        assert classifications["documents"][0]["filename"] == "学生工作实施建议.docx"
        assert classifications["documents"][0]["categories"][0]["name"] == "学生工作"
        assert classifications["documents"][1]["filename"] == "本人上传材料.docx"
        assert classifications["documents"][1]["categories"][0]["name"] == "教学"
        assert (
            classifications["documents"][0]["categories"][0]["evidence_items"][0][
                "page_number"
            ]
            == 2
        )
        assert insights["documents"][0]["filename"] == "学生工作实施建议.docx"
        assert insights["documents"][0]["summary"] == "文件说明学生教育管理工作安排。"
    finally:
        db.close()


def test_capability_suggestion_admin_page_api_enforces_review_roles():
    """ops 可以查看和开始评审，但只有 admin 可以接受能力建议。"""

    client, session_factory = client_with_database()
    try:
        for username, role in [("ops-reviewer", "ops"), ("admin-reviewer", "admin")]:
            response = client.post(
                "/api/auth/register",
                json={
                    "username": username,
                    "password": "password123",
                    "display_name": username,
                },
            )
            assert response.status_code == 200
            with session_factory() as db:
                user = db.query(User).filter(User.username == username).one()
                user.role = role
                db.commit()
        with session_factory() as db:
            suggestion = CapabilitySuggestion(
                suggestion_kind="CAPABILITY",
                title="支持新格式",
                missing_capability="读取新格式文件",
                reason="当前 Catalog 不支持",
                confidence=0.9,
                deduplication_fingerprint="c" * 64,
                occurrence_count=1,
                catalog_fingerprint="d" * 64,
                status="NEW",
            )
            db.add(suggestion)
            db.commit()
            suggestion_id = suggestion.id

        def auth_header(username: str) -> dict[str, str]:
            """登录指定管理员并返回 Bearer header。"""

            login = client.post(
                "/api/auth/login",
                json={"username": username, "password": "password123"},
            )
            assert login.status_code == 200
            return {
                "Authorization": f"Bearer {login.json()['access_token']}"
            }

        ops_headers = auth_header("ops-reviewer")
        assert client.get(
            "/api/admin/capability-suggestions",
            headers=ops_headers,
        ).status_code == 200
        assert client.get(
            "/api/admin/planner-shadow/metrics",
            headers=ops_headers,
        ).status_code == 200
        assert client.post(
            f"/api/admin/capability-suggestions/{suggestion_id}/review",
            headers=ops_headers,
            json={"status": "ACCEPTED", "review_note": "接受"},
        ).status_code == 403
        assert client.post(
            f"/api/admin/capability-suggestions/{suggestion_id}/review",
            headers=ops_headers,
            json={"status": "UNDER_REVIEW", "review_note": "评审中"},
        ).status_code == 200

        admin_headers = auth_header("admin-reviewer")
        accepted = client.post(
            f"/api/admin/capability-suggestions/{suggestion_id}/review",
            headers=admin_headers,
            json={"status": "ACCEPTED", "review_note": "进入开发排期"},
        )
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "ACCEPTED"
        assert client.post(
            f"/api/admin/capability-suggestions/{suggestion_id}/review",
            headers=ops_headers,
            json={"status": "REJECTED", "review_note": "试图覆盖管理员结论"},
        ).status_code == 403
    finally:
        clear_overrides()
