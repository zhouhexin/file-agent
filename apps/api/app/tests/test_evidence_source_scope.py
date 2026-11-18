"""显式来源参数校验、一次模型纠错与只读兜底测试，防止错误条件伪装成文件零结果。"""

from copy import deepcopy
from datetime import date

import pytest

from app.modules.agent.adaptive_planner import validate_and_convert_decision
from app.modules.agent.catalog import AgentCatalogService
from app.modules.agent.planner_contracts import PlannerDecision, PlannerScope, ToolPlan, ToolStep
from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import ToolInvocationRecord
from app.modules.agent.tool_registry import ToolRegistry
from app.modules.llm.client import LLMResponseError
from app.modules.llm.schemas import UserIntentPlan
from app.modules.retrieval.query_parser import FileSearchQueryParser
from app.modules.retrieval.semantic_plan import FileSearchSemanticPlan
from app.modules.retrieval.source_reference import EvidenceSourcePlanError, validate_source_search_plan
from app.tests.test_evidence_reference_discovery import PHOTO_QUESTION


QUESTION = PHOTO_QUESTION + "，并说明今年是否还按照这些要求执行"
SOURCE_QUERY = "2023届毕业生合影照片通知"


@pytest.fixture(autouse=True)
def fixed_date(monkeypatch):
    """冻结时间，保证‘今年’的检索条件校验不随测试运行年份变化。"""

    monkeypatch.setattr(FileSearchQueryParser, "_current_date", lambda self: date(2026, 10, 30))


def _semantic(phrase="毕业生合影照片"):
    """模型精确短语与来源原话的连续性必须能被确定性验证。"""

    return FileSearchSemanticPlan.model_validate({"core_topics": [{"phrase": phrase}]})


def _intent(*, invalid=False):
    """复现真实失败计划，只把拼造的精确主题作为可变项。"""

    return UserIntentPlan(
        intent="ANSWER_QUESTION_FROM_FILES", user_goal=QUESTION,
        required_capabilities=["evidence_answer"], tool_plan_hint=["hybrid-search", "evidence-answer"],
        managed_query=SOURCE_QUERY,
        file_search_plan=_semantic("毕业生合影照片归档" if invalid else "毕业生合影照片"),
    )


@pytest.mark.parametrize("query", [SOURCE_QUERY, None])
def test_current_applicability_question_keeps_historical_source_filters(query):
    """‘今年是否仍执行’保留在回答任务中，来源的年份仍为2023。"""

    validate_source_search_plan(message=QUESTION, search_query=query, semantic_plan=_semantic())
    assert FileSearchQueryParser(tokenizer=None).parse(query or QUESTION).year == 2023


@pytest.mark.parametrize(
    ("query", "semantic", "code"),
    [
        (SOURCE_QUERY, _semantic("毕业生合影照片归档"), "SOURCE_EXACT_PHRASE_NOT_GROUNDED"),
        ("今年毕业生合影照片通知", _semantic(), "SOURCE_TIME_FILTER_MISMATCH"),
        ("2023年10月毕业生合影照片通知", _semantic(), "SOURCE_TIME_FILTER_MISMATCH"),
        ("毕业生合影照片通知", _semantic(), "SOURCE_TIME_FILTER_MISMATCH"),
        ("2023届奖学金通知", _semantic(), "SOURCE_QUERY_NOT_GROUNDED"),
        (None, None, "SOURCE_QUERY_REQUIRED"),
        (SOURCE_QUERY, None, "SOURCE_QUERY_NOT_GROUNDED"),
    ],
)
def test_bad_source_conditions_are_rejected_before_search(query, semantic, code):
    """禁止合并原话、丢掉年份、混入月份或把文件任务换成其他主题。"""

    with pytest.raises(EvidenceSourcePlanError) as caught:
        validate_source_search_plan(message=QUESTION, search_query=query, semantic_plan=semantic)
    assert caught.value.code == code
    assert caught.value.diagnostic.stage == "INTENT_PLAN_VALIDATION"
    assert QUESTION not in str(caught.value)


def test_validator_applies_to_other_sources_without_business_keyword_hardcoding():
    """来源范围校验不限于照片通知，也不能把报销与预算拼成一个必需短语。"""

    with pytest.raises(EvidenceSourcePlanError, match="来源约束"):
        validate_source_search_plan(
            message="只根据2024年经费报销通知，说明今年是否执行。",
            search_query="2024年经费报销通知", semantic_plan=_semantic("经费报销预算"),
        )
    validate_source_search_plan(
        message="帮我查找经费报销相关文件", search_query="经费报销预算", semantic_plan=_semantic("经费报销预算"),
    )


def test_literal_source_query_without_semantic_plan_is_still_supported():
    """无结构化计划时保留来源原话查询，不能为修复而强制所有请求都带语义计划。"""

    validate_source_search_plan(
        message=QUESTION, search_query="2023届毕业生合影照片的通知", semantic_plan=None,
    )


class RecordingRegistry(ToolRegistry):
    """复用生产白名单，只把真实 I/O 替换为确定性观察。"""

    def __init__(self, trace):
        """记录每次真正执行 Tool 的次序和范围。"""

        super().__init__()
        self.trace = trace
        self.calls = []

    def invoke(self, tool_name, input_json):
        """无效计划必须在本方法之前被拒绝，不允许先执行一次零结果检索。"""

        self.trace.append(tool_name)
        self.calls.append((tool_name, input_json))
        if tool_name == "hybrid-search":
            output = {
                "kind": "workspace_file_search", "ok": True, "query": input_json["query"],
                "total_returned": 1, "results": [{"document_id": "verified-notice", "filename": "2023通知.pdf", "relevance_tier": "SUPPORTED"}],
                "document_ids": ["verified-notice"], "effective_conditions": [],
                "index_status": "READY", "result_status": "MATCHED",
            }
        else:
            assert tool_name == "evidence-answer"
            output = {
                "kind": "evidence_answer", "ok": True, "status": "PARTIAL",
                "answer": "仅凭该文件无法确认今年是否仍执行。", "references": [],
            }
        return ToolInvocationRecord(tool_name=tool_name, input_json=input_json, output_json=output, status="COMPLETED")


@pytest.mark.parametrize("repair", ["success", "invalid_again", "changes_task", "network_error", "drops_semantic"])
def test_bad_plan_is_repaired_once_before_any_search_and_cannot_change_task(repair):
    """LLM 保持业务决定，只允许一次参数纠错；仍失败时走已受控的来源只读降级。"""

    trace = []
    requests = []
    registry = RecordingRegistry(trace)

    class SequenceIntentService:
        """按次序返回拼造短语、修正结果或故障，不调用外部模型。"""

        enabled = True

        def understand_user_request(self, **kwargs):
            """验证纠错请求保留完整问题并只附固定的计划错误反馈。"""

            requests.append(deepcopy(kwargs))
            trace.append("intent")
            if len(requests) == 1:
                return _intent(invalid=True)
            if repair == "network_error":
                raise LLMResponseError("接口不可用")
            if repair == "changes_task":
                return UserIntentPlan(intent="GENERAL_CHAT", user_goal="改成聊天")
            if repair == "drops_semantic":
                return _intent().model_copy(update={"file_search_plan": None})
            return _intent(invalid=repair == "invalid_again")

    result = AgentRuntimeService(
        registry_factory=lambda db, user_id: registry, llm_intent_service=SequenceIntentService(),
    ).run_message(
        conversation_id="source-scope", user_id="user-1", message_id="source-message", message=QUESTION, attachments=[],
    )

    assert trace == ["intent", "intent", "hybrid-search", "evidence-answer"]
    assert requests[1]["message"] == QUESTION
    feedback = requests[1]["observation"]["source_plan_validation"]
    assert feedback["error_code"] == "SOURCE_EXACT_PHRASE_NOT_GROUNDED"
    assert feedback["max_repairs"] == 1
    assert registry.calls[1][1]["question"] == QUESTION
    assert registry.calls[1][1]["document_ids"] == ["verified-notice"]
    assert result.intent == "EVIDENCE_ANSWER"
    search = registry.calls[0][1]
    assert FileSearchQueryParser(tokenizer=None).parse(search["query"]).year == 2023
    if repair == "success":
        assert search["semantic_plan"] == _semantic().model_dump()
    else:
        assert "semantic_plan" not in search


@pytest.mark.parametrize("invalid", ["phrase", "query_type", "semantic_type"])
def test_adaptive_global_search_checks_actual_user_source_before_dispatch(invalid):
    """Adaptive 也必须按真实用户消息校验，不能依靠模型改写的 user_goal 绕过范围。"""

    registry = RecordingRegistry([])
    literal_input = {"query": SOURCE_QUERY, "semantic_plan": _semantic("毕业生合影照片归档").model_dump()}
    if invalid == "query_type":
        literal_input["query"] = 123
    elif invalid == "semantic_type":
        literal_input["semantic_plan"] = {"core_topics": "invalid-type"}
    decision = PlannerDecision(
        decision_type="TOOL_PLAN", intent="EVIDENCE_DISCOVERY", user_goal="模型改写的目标",
        selected_skill_ids=["file-search"], scope=PlannerScope(source="unspecified"),
        tool_plan=ToolPlan(plan_id="bad-source", steps=[ToolStep(
            step_id="search", skill_id="file-search", tool_name="hybrid-search",
            literal_input=literal_input,
        )]),
    )
    with pytest.raises(EvidenceSourcePlanError) as caught:
        validate_and_convert_decision(
            decision=decision, registry=registry, catalog_snapshot=AgentCatalogService(registry=registry).build_snapshot(),
            attachments=[], context_documents=[], user_message=QUESTION,
        )
    assert not registry.calls
    assert caught.value.code == ("SOURCE_EXACT_PHRASE_NOT_GROUNDED" if invalid == "phrase" else "SOURCE_PLAN_SCHEMA_INVALID")
