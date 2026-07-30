"""Adaptive Planner 的决策、计划和能力建议契约。

LLM 只能生成本模块定义的声明式结构。文件范围、Skill、Tool、绑定、风险和确认要求仍由后端根据
CatalogSnapshot 与请求上下文再次校验。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.agent.tool_contracts import ToolResultBinding


# 计划可描述比执行预算更多的步骤，Dispatcher 仍会在第 5 次真实调用前关闭式拒绝；
# 保留较大的 schema 上限便于生成逐文件拒绝回执，而不是在规划阶段丢失对象明细。
MAX_TOOL_STEPS_PER_PLAN = 20


class CapabilitySuggestionDraft(BaseModel):
    """现有 Catalog 无法满足明确用户目标时的待评审能力建议。"""

    model_config = ConfigDict(extra="forbid")

    suggestion_kind: Literal["CAPABILITY", "TOOL", "SKILL"] = "CAPABILITY"
    title: str = Field(min_length=2, max_length=200)
    missing_capability: str = Field(min_length=2, max_length=500)
    reason: str = Field(min_length=2, max_length=1000)
    expected_inputs: list[str] = Field(default_factory=list, max_length=20)
    expected_outputs: list[str] = Field(default_factory=list, max_length=20)
    related_skill_ids: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.5, ge=0, le=1)


class PlannerScope(BaseModel):
    """Planner 描述的文件范围意图；真实 ID 仍由后端解析。"""

    model_config = ConfigDict(extra="forbid")

    document_ids: list[str] = Field(default_factory=list, max_length=100)
    source: str = "unspecified"
    requires_backend_resolution: bool = False


class ToolStep(BaseModel):
    """Adaptive ToolPlan 中的一步声明式调用。"""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=120)
    skill_id: str = Field(min_length=1, max_length=120)
    tool_name: str = Field(min_length=1, max_length=120)
    literal_input: dict[str, Any] = Field(default_factory=dict)
    bindings: list[ToolResultBinding] = Field(default_factory=list, max_length=20)
    requires_confirmation: bool = False
    expected_output_kind: str | None = Field(default=None, max_length=120)


class ToolPlan(BaseModel):
    """Adaptive Planner 生成的步骤计划。"""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=120)
    steps: list[ToolStep] = Field(
        min_length=1,
        max_length=MAX_TOOL_STEPS_PER_PLAN,
    )

    @model_validator(mode="after")
    def validate_step_dependencies(self) -> "ToolPlan":
        """拒绝重复步骤、未来步骤绑定和同一目标字段的重复写入。"""

        seen_step_ids: set[str] = set()
        for step in self.steps:
            if step.step_id in seen_step_ids:
                raise ValueError(f"duplicate step_id: {step.step_id}")
            target_fields: set[str] = set()
            for binding in step.bindings:
                if binding.source_step_id not in seen_step_ids:
                    raise ValueError(
                        "binding source must reference a previous step: "
                        f"{binding.source_step_id}"
                    )
                if binding.target_field in target_fields:
                    raise ValueError(
                        f"duplicate binding target: {binding.target_field}"
                    )
                target_fields.add(binding.target_field)
            seen_step_ids.add(step.step_id)
        return self


class PlannerClarification(BaseModel):
    """CLARIFY 决策所需的单个关键问题。"""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=1000)


class PlannerDecision(BaseModel):
    """Planner 对本轮用户请求或执行观察作出的受控决策。"""

    model_config = ConfigDict(extra="forbid")

    decision_type: Literal["TOOL_PLAN", "DIRECT_RESPONSE", "CLARIFY", "FINISH"]
    intent: str = Field(min_length=1, max_length=120)
    user_goal: str = Field(min_length=1, max_length=2000)
    selected_skill_ids: list[str] = Field(default_factory=list, max_length=20)
    scope: PlannerScope = Field(default_factory=PlannerScope)
    tool_plan: ToolPlan | None = None
    capability_suggestions: list[CapabilitySuggestionDraft] = Field(
        default_factory=list,
        max_length=5,
    )
    direct_response: str | None = Field(default=None, max_length=4000)
    clarification: PlannerClarification | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_decision_branch(self) -> "PlannerDecision":
        """保证四种决策分支互斥，避免空回复或直接回复夹带 Tool。"""

        if self.decision_type == "TOOL_PLAN":
            if self.tool_plan is None:
                raise ValueError("TOOL_PLAN requires tool_plan")
            if self.direct_response is not None or self.clarification is not None:
                raise ValueError("TOOL_PLAN cannot include response or clarification")
            selected = set(self.selected_skill_ids)
            unselected = sorted(
                {
                    step.skill_id
                    for step in self.tool_plan.steps
                    if step.skill_id not in selected
                }
            )
            if unselected:
                raise ValueError(
                    f"tool plan references unselected skills: {unselected}"
                )
        elif self.decision_type == "DIRECT_RESPONSE":
            if not str(self.direct_response or "").strip():
                raise ValueError("DIRECT_RESPONSE requires direct_response")
            if self.tool_plan is not None or self.clarification is not None:
                raise ValueError("DIRECT_RESPONSE cannot include tool plan or clarification")
        elif self.decision_type == "CLARIFY":
            if self.clarification is None:
                raise ValueError("CLARIFY requires clarification")
            if self.tool_plan is not None or self.direct_response is not None:
                raise ValueError("CLARIFY cannot include tool plan or direct response")
        else:
            # FINISH 只表示现有 Tool 结果已经满足用户目标，最终文本仍由后端聚合器生成。
            if (
                self.tool_plan is not None
                or self.direct_response is not None
                or self.clarification is not None
            ):
                raise ValueError(
                    "FINISH cannot include tool plan, response or clarification"
                )
        return self
