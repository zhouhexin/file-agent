"""MVP Agent Runtime 使用的确定性 Planner 和计划 schema。

后续真实 Planner 可以调用 LLM，但仍必须返回这里定义的声明式结构。
Shell 命令、SQL 写入和文件系统路径会在 Tool dispatch 前被拒绝。
"""

from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


FORBIDDEN_INPUT_KEYS = {"shell", "shell_command", "sql", "sql_write", "path", "file_path", "absolute_path"}


class PlannerStep(BaseModel):
    """Planner 生成的一步声明式 Tool 调用。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    skill: str
    tool_name: str
    input: Dict[str, Any]
    requires_confirmation: bool = False
    risk_level: str = "low"
    expected_outputs: List[str] = Field(default_factory=list)
    writes: List[str] = Field(default_factory=list)

    @field_validator("input")
    @classmethod
    def reject_direct_actions(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        """拒绝 Planner 通过输入参数夹带直接执行指令。"""

        forbidden = FORBIDDEN_INPUT_KEYS.intersection(value.keys())
        if forbidden:
            raise ValueError(f"Planner step contains forbidden direct action keys: {sorted(forbidden)}")
        return value

    @field_validator("writes")
    @classmethod
    def reject_direct_writes(cls, value: List[str]) -> List[str]:
        """拒绝直接指向 shell、SQL 或文件系统的写入声明。"""

        bad_writes = [item for item in value if item.startswith(("filesystem:", "shell:", "sql:"))]
        if bad_writes:
            raise ValueError(f"Planner step contains forbidden direct writes: {bad_writes}")
        return value


class PlannerOutput(BaseModel):
    """供 LangGraph tool-dispatch 节点消费的声明式计划。"""

    model_config = ConfigDict(extra="forbid")

    intent: str
    user_goal: str
    slots: Dict[str, Any] = Field(default_factory=dict)
    selected_skills: List[str]
    steps: List[PlannerStep]
    evidence_policy: Dict[str, Any]
    confirmation_policy: Dict[str, Any]

    @model_validator(mode="after")
    def reject_empty_steps(self) -> "PlannerOutput":
        """要求每次运行至少包含一个 Tool 步骤，确保意图可审计。"""

        if not self.steps:
            raise ValueError("Planner output must contain at least one step")
        return self


class DeterministicPlanner:
    """用于测试和早期框架开发的确定性 Planner。

    它不调用外部 LLM，保证 Agent Runtime 测试输出稳定。
    """

    def __init__(self, force_unsafe_step: bool = False) -> None:
        self.force_unsafe_step = force_unsafe_step

    def plan(
        self,
        conversation_id: str,
        user_id: str,
        message_id: str,
        message: str,
        attachments: List[Dict[str, Any]],
    ) -> PlannerOutput:
        """根据用户消息和附件上下文生成声明式计划。"""

        document_id = _first_document_id(attachments)
        if self.force_unsafe_step:
            return PlannerOutput(
                intent="UNSAFE_DIRECT_WRITE",
                user_goal=message,
                slots={"document_ids": [document_id]},
                selected_skills=["file-ingest"],
                steps=[
                    {
                        "step_id": "step-unsafe",
                        "skill": "file-ingest",
                        "tool_name": "document-convert",
                        "input": {"document_id": document_id, "path": "/tmp/unsafe"},
                        "requires_confirmation": False,
                        "risk_level": "high",
                        "expected_outputs": ["file"],
                        "writes": ["filesystem:/tmp/unsafe"],
                    }
                ],
                evidence_policy={"require_page_or_cell": True, "allow_no_evidence_answer": False},
                confirmation_policy={"operation_plan_required": True},
            )

        lowered = message.lower()
        if "问" in message or "回答" in message or "answer" in lowered:
            return PlannerOutput(
                intent="EVIDENCE_ANSWER",
                user_goal=message,
                slots={"document_ids": [document_id], "question": message},
                selected_skills=["chat-intake", "file-search", "evidence-answer"],
                steps=[
                    {
                        "step_id": "step-1",
                        "skill": "file-search",
                        "tool_name": "hybrid-search",
                        "input": {"query": message, "document_ids": [document_id]},
                        "requires_confirmation": False,
                        "risk_level": "low",
                        "expected_outputs": ["retrieved_chunks"],
                        "writes": [],
                    },
                    {
                        "step_id": "step-2",
                        "skill": "evidence-answer",
                        "tool_name": "evidence-answer",
                        "input": {"question": message, "document_ids": [document_id]},
                        "requires_confirmation": False,
                        "risk_level": "low",
                        "expected_outputs": ["answer", "references"],
                        "writes": ["qa_answers", "answer_references"],
                    },
                ],
                evidence_policy={"require_page_or_cell": True, "allow_no_evidence_answer": True},
                confirmation_policy={"operation_plan_required": False},
            )

        return PlannerOutput(
            intent="CLASSIFY_FILES",
            user_goal=message,
            slots={"document_ids": [document_id], "requested_outputs": ["classification", "receipt"]},
            selected_skills=["chat-intake", "file-ingest", "document-classification", "change-report"],
            steps=[
                {
                    "step_id": "step-1",
                    "skill": "file-ingest",
                    "tool_name": "document-convert",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["pages", "metadata", "artifacts"],
                    "writes": ["document_pages", "artifacts", "change_items"],
                },
                {
                    "step_id": "step-2",
                    "skill": "file-ingest",
                    "tool_name": "metadata-extract",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["metadata"],
                    "writes": ["documents.metadata"],
                },
                {
                    "step_id": "step-3",
                    "skill": "document-classification",
                    "tool_name": "multi-label-classify",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["document_categories"],
                    "writes": ["document_categories"],
                },
                {
                    "step_id": "step-4",
                    "skill": "change-report",
                    "tool_name": "change-report",
                    "input": {"document_id": document_id},
                    "requires_confirmation": False,
                    "risk_level": "low",
                    "expected_outputs": ["receipt"],
                    "writes": ["change_sets"],
                },
            ],
            evidence_policy={"require_page_or_cell": True, "allow_no_evidence_answer": False},
            confirmation_policy={"operation_plan_required": False},
        )


def _first_document_id(attachments: List[Dict[str, Any]]) -> str:
    """为 MVP 确定性计划解析第一个附件文档 id。"""

    if not attachments:
        return "document-memory"
    return str(attachments[0].get("document_id") or "document-memory")
