"""LLM 结构化文件检索计划回归测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from types import SimpleNamespace

from app.modules.agent.graph import _build_workspace_file_search_response
from app.modules.agent.tool_registry import (
    _execute_controlled_file_search,
    _intersect_required_semantic_results,
)
from app.modules.agent.tool_schemas import SearchToolInput
from app.modules.retrieval.query_parser import ParsedQuery
from app.modules.retrieval.semantic_plan import (
    FileSearchSemanticPlan,
    apply_semantic_result_plan,
)


def _school_summary_plan(*, organization_level: str = "ANY") -> FileSearchSemanticPlan:
    """构造测试使用的严格工作总结检索计划。"""

    return FileSearchSemanticPlan.model_validate(
        {
            "core_topics": [
                {
                    "phrase": "工作总结",
                    "required": True,
                    "match_mode": "EXACT_PHRASE",
                }
            ],
            "scope": {
                "type": "CURRENT_SCHOOL_WORKSPACE",
                "organization_level": organization_level,
                "organization_terms": [],
            },
            "preferred_results": [
                {"organization_level": "UNIVERSITY", "boost": 1.0}
            ],
            "group_by": ["organization_level", "business_topic", "year"],
            "response_style": "GROUPED_FILE_LIST",
        }
    )


def test_search_tool_rejects_uncontrolled_semantic_match_mode():
    """模型不能借语义计划输出模糊 OR 或未声明字段。"""

    with pytest.raises(ValidationError):
        SearchToolInput.model_validate(
            {
                "query": "学校的工作总结",
                "semantic_plan": {
                    "core_topics": [
                        {
                            "phrase": "工作总结",
                            "required": True,
                            "match_mode": "BROAD_OR",
                        }
                    ]
                },
            }
        )


def test_required_semantic_phrases_are_intersected_without_union_leakage():
    """多个必需短语只能保留同一文件交集，不能合并为 OR。"""

    result = _intersect_required_semantic_results(
        original_query="计算机学院的工作总结",
        payloads=[
            {
                "results": [
                    {"working_copy_id": "wc-1", "filename": "工作总结.docx"},
                    {"working_copy_id": "wc-2", "filename": "学校工作总结.docx"},
                ]
            },
            {
                "results": [
                    {"working_copy_id": "wc-1", "filename": "工作总结.docx"},
                    {"working_copy_id": "wc-3", "filename": "学院通知.docx"},
                ]
            },
        ],
    )

    assert [item["working_copy_id"] for item in result["results"]] == ["wc-1"]


def test_controlled_search_executes_complete_phrases_unbounded_before_intersection():
    """Tool 必须分别完整召回核心主题和机构，并在交集前取消候选上限。"""

    class FakeTokenizer:
        """返回完整短语，避免测试依赖 Jieba 词典。"""

        def tokenize(self, text):
            """保留完整输入。"""

            return [text]

    class FakeSearchService:
        """记录两阶段检索调用并返回确定性候选。"""

        workspace_id = "workspace-shared"

        def __init__(self):
            self.calls = []

        def search(self, **kwargs):
            """按 exact_phrase 返回用于交集的文件集合。"""

            self.calls.append(kwargs)
            phrase = kwargs["exact_phrase"]
            if phrase == "工作总结":
                rows = [
                    {
                        "working_copy_id": "wc-1",
                        "filename": "计算机学院2025年工作总结.docx",
                    },
                    {
                        "working_copy_id": "wc-2",
                        "filename": "西安理工大学2025年工作总结.pdf",
                    },
                ]
            else:
                rows = [
                    {
                        "working_copy_id": "wc-1",
                        "filename": "计算机学院2025年工作总结.docx",
                    }
                ]
            return {
                "ok": True,
                "kind": "workspace_file_search",
                "results": rows,
                "partial": False,
            }

    search_service = FakeSearchService()
    tool_input = SearchToolInput.model_validate(
        {
            "query": "计算机学院的工作总结",
            "semantic_plan": {
                "core_topics": [{"phrase": "工作总结"}],
                "scope": {
                    "organization_terms": [{"phrase": "计算机学院"}]
                },
            },
        }
    )
    result = _execute_controlled_file_search(
        db=None,
        user_id="user-a",
        conversation_id=None,
        agent_run_id=None,
        tool_input=tool_input,
        search_query=tool_input.query,
        parsed=ParsedQuery(
            original=tool_input.query,
            cleaned="计算机学院的工作总结",
            terms=["计算机学院", "工作总结"],
        ),
        scope=SimpleNamespace(scope_mode="global"),
        tokenizer=FakeTokenizer(),
        search_service=search_service,
    )

    assert [call["exact_phrase"] for call in search_service.calls] == [
        "工作总结",
        "计算机学院",
    ]
    assert all(call["unbounded_candidates"] for call in search_service.calls)
    assert [item["working_copy_id"] for item in result["results"]] == ["wc-1"]


def test_semantic_plan_groups_results_and_prefers_university_level():
    """校级只做排序偏好时仍保留学院结果，并生成清晰分组。"""

    result = apply_semantic_result_plan(
        result={
            "ok": True,
            "kind": "workspace_file_search",
            "query": "学校的工作总结",
            "results": [
                {
                    "working_copy_id": "wc-college",
                    "filename": "计算机学院2025年工作总结.docx",
                    "relative_path": "办公/学院工作总结/计算机学院2025年工作总结.docx",
                    "year": 2025,
                    "category_path": ["学院", "工作总结"],
                    "relevance_tier": "SUPPORTED",
                },
                {
                    "working_copy_id": "wc-school",
                    "filename": "西安理工大学2024年工作总结.pdf",
                    "relative_path": "学校文件/2025年/西安理工大学2024年工作总结.pdf",
                    "year": 2024,
                    "category_path": ["学校", "工作总结"],
                    "relevance_tier": "SUPPORTED",
                },
            ],
        },
        plan=_school_summary_plan(),
    )

    assert [item["working_copy_id"] for item in result["results"]] == [
        "wc-school",
        "wc-college",
    ]
    assert [
        group["group_values"][0] for group in result["result_groups"]
    ] == ["UNIVERSITY", "COLLEGE"]

    response = _build_workspace_file_search_response(result)
    assert "学校层面" in response
    assert "学院层面" in response
    assert "路径：学校文件/2025年" in response


def test_university_only_scope_filters_college_results():
    """只有用户明确要求校级时才应用 UNIVERSITY 硬过滤。"""

    result = apply_semantic_result_plan(
        result={
            "ok": True,
            "kind": "workspace_file_search",
            "query": "只找学校层面的工作总结",
            "results": [
                {
                    "working_copy_id": "wc-school",
                    "filename": "西安理工大学2025年工作总结.pdf",
                    "relative_path": "学校文件/2026年/工作总结.pdf",
                },
                {
                    "working_copy_id": "wc-college",
                    "filename": "计算机学院2025年工作总结.docx",
                },
            ],
        },
        plan=_school_summary_plan(organization_level="UNIVERSITY"),
    )

    assert [item["working_copy_id"] for item in result["results"]] == [
        "wc-school"
    ]
