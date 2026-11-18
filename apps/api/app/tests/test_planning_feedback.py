"""保护反馈中的目标、动作和契约事实边界，禁止把推测或Gold冒充已观察原因。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.modules.evidence_harness.improvement.planning_feedback import task_diagnostic, application_intent_contract


def result(intent, tool="hybrid-search", *, schema_valid=True, backend_intent=None):
    """仅构造诊断输入；独立来源核验由adaptive_policy集成验收覆盖。"""
    return {"plan": {"intent": backend_intent or intent, "tool_plan": {"steps": [{"tool_name": tool}] if tool else []}},
            "route": "FILE_LIST", "planning_audit": {"responses": [{"phase": "ADAPTIVE", "status": "COMPLETED",
                "schema_valid": schema_valid, "raw": {"intent": intent, "user_goal": "先定位材料，再依据原文回答问题。"}}]}}


@pytest.mark.parametrize("intent,tool,schema_valid,mapping,issue", [
    ("定位指定来源后回答", "hybrid-search", True, "UNRECOGNIZED", True),
    ("ANSWER_QUESTION_FROM_FILES", "hybrid-search", True, "RECOGNIZED", False),
    ("SEARCH_FILES", "hybrid-search", True, "RECOGNIZED", False),
    ("LIST_MANAGED_FILES", "managed-file-list", True, "RECOGNIZED", False),
    ("未知意图", "hybrid-search", False, "UNRECOGNIZED", False),
    ("UNKNOWN", None, True, "NOT_APPLICABLE", False),
    ("CLASSIFY_FILES", "extract-document-text", True, "NOT_APPLICABLE", False),
    ("EVIDENCE_ANSWER", "evidence-answer", True, "RECOGNIZED", False),
])
def test_goal_action_and_schema_contract_are_separate(intent, tool, schema_valid, mapping, issue):
    """发现Tool不等于文件清单目标；结构通过与应用映射是两项独立事实。"""
    case = SimpleNamespace(question="只根据通知，回答正文要求并引用原文。")
    original = result(intent, tool, schema_valid=schema_valid)
    before = deepcopy(original)
    diagnostic = task_diagnostic(case, original)
    assert diagnostic["final_task_goal"]["original_request"] == case.question
    assert diagnostic["final_task_goal"]["model_declared_goal"] == "先定位材料，再依据原文回答问题。"
    assert diagnostic["final_task_goal"]["semantic_alignment"] == "NOT_INDEPENDENTLY_VERIFIED"
    assert diagnostic["intent_contract"]["planner_schema_valid"] is schema_valid
    assert diagnostic["intent_contract"]["mapping_status"] == mapping
    assert bool(diagnostic["mismatch_observations"]) is issue
    assert diagnostic["current_tool_action"]["determines_final_task_goal"] is False
    assert diagnostic["unverified"]["retrieval_effectiveness"] == diagnostic["unverified"]["answer_quality"] == "NOT_EXECUTED"
    assert diagnostic["unverified"]["causal_explanation"] == "NOT_VERIFIED" and original == before
    if issue:
        assert diagnostic["mismatch_observations"][0]["code"] == "UNRECOGNIZED_TASK_INTENT_FOR_DISCOVERY"
        assert diagnostic["mismatch_observations"][0]["certainty"] == "OBSERVED"


def test_missing_information_is_unknown_not_inferred_from_question():
    """原始文字含业务关键词也不能填造模型目标、意图或语义裁决。"""
    diagnostic = task_diagnostic(SimpleNamespace(question="找文件后回答正文内容"), {})
    assert diagnostic["final_task_goal"]["model_declared_goal"] is None
    assert diagnostic["intent_contract"]["planner_schema_valid"] is None
    assert diagnostic["current_tool_action"]["status"] == "NOT_PLANNED"
    assert diagnostic["mismatch_observations"] == []
    assert "expected_route" not in str(diagnostic) and "expected_intent" not in str(diagnostic)


def test_aliases_use_existing_application_mapping():
    """解释层列出的别名必须仍被当前真实分类器支持，不能另起线上路由规则。"""
    contract = application_intent_contract()
    for intent in contract["content_intents"] + contract["file_intents"]:
        diagnostic = task_diagnostic(SimpleNamespace(question="目标由原始用户请求确定。"), result(intent))
        assert diagnostic["intent_contract"]["mapping_status"] == "RECOGNIZED"
        assert diagnostic["mismatch_observations"] == []
