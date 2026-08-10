"""检索选择续跑专用 Planner。

该 Planner 只由后端在校验持久化 option_id 后创建。它仍输出声明式 Tool 计划并经过
Tool Registry schema 校验，不能直接调用数据库或检索实现。
"""

from __future__ import annotations

from typing import Any

from app.modules.agent.planner import DeterministicPlanner, PlannerOutput
from app.modules.retrieval.clarification_service import ResolvedSearchSelection


class FileSearchClarificationPlanner:
    """把已校验的用户选择转换为唯一 hybrid-search 计划。"""

    def __init__(self, selection: ResolvedSearchSelection) -> None:
        """保存不可变选择结果，不接受浏览器短语数组。"""

        self.selection = selection

    def plan(self, **_: Any) -> PlannerOutput:
        """生成受 Tool schema 约束的检索续跑计划。"""

        value = self.selection
        if value.match_mode == "RENAME_DOCUMENT_SELECTION":
            return PlannerOutput(
                intent="RESOLVE_RENAME_REVIEW",
                user_goal=value.display_content,
                slots={
                    "document_ids": list(value.document_ids),
                    "requested_outputs": ["rename_review_resolution"],
                    "search_clarification_id": value.clarification_id,
                },
                selected_skills=[
                    "file-rename",
                    "operation-plan",
                    "confirmed-file-action",
                ],
                steps=[
                    {
                        "step_id": "step-resolve-selected-rename-review",
                        "skill": "file-rename",
                        "tool_name": "resolve-rename-reviews",
                        "input": {"message": value.display_content},
                        "requires_confirmation": False,
                        "risk_level": "medium",
                        "expected_outputs": [
                            "rename_results",
                            "operation_plan",
                        ],
                        "writes": ["operation_plans"],
                    }
                ],
                evidence_policy={
                    "require_page_or_cell": False,
                    "allow_no_evidence_answer": True,
                },
                confirmation_policy={"operation_plan_required": True},
            )
        if value.document_ids:
            # 文件选择只负责确定范围，不能把“表格汇总、分类、总结”等原始任务
            # 全部改写成证据问答。重新交给确定性 Planner 后，所选一份或多份文件
            # 会沿原问题继续进入对应的受控 Tool 链路。
            plan = DeterministicPlanner().plan(
                conversation_id=value.conversation_id,
                user_id=value.user_id,
                message_id="clarification-selection",
                message=value.original_query,
                attachments=[
                    {
                        "document_id": document_id,
                        "context_scope": "clarification_selection",
                    }
                    for document_id in value.document_ids
                ],
            )
            # 只有后端校验持久化 option_id 后创建的本 Planner 才能注入选择凭据。
            # evidence-answer 还会回查记录，防止 LLM 自报凭据或替换 document_ids。
            for step in plan.steps:
                if step.tool_name == "evidence-answer":
                    step.input["document_selection_clarification_id"] = (
                        value.clarification_id
                    )
            return plan
        tool_input = {
            "query": value.original_query,
            "document_ids": [],
            "match_mode": value.match_mode,
            "phrases": list(value.phrases),
            "require_body_evidence": value.require_body_evidence,
            "clarification_id": value.clarification_id,
            "clarification_option_id": value.option_id,
            "show_all_results": value.show_all_results,
        }
        return PlannerOutput(
            intent="SEARCH_FILES",
            user_goal=value.display_content,
            slots={
                "query": value.original_query,
                "requested_outputs": ["file_search_results"],
                "search_clarification_id": value.clarification_id,
            },
            selected_skills=["file-search"],
            steps=[
                {
                    "step_id": "step-file-search-resolution",
                    "skill": "file-search",
                    "tool_name": "hybrid-search",
                    "input": tool_input,
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["ranked_working_copies"],
                    "writes": [],
                }
            ],
            evidence_policy={
                "require_page_or_cell": value.require_body_evidence,
                "allow_no_evidence_answer": not value.require_body_evidence,
            },
            confirmation_policy={"operation_plan_required": False},
        )
