"""Catalog 驱动的 Adaptive Planner 服务和后端计划适配器。

模型只能输出 PlannerDecision。后端随后校验 Skill/Tool 是否存在、Skill 是否允许调用该 Tool、文件 ID
是否属于已解析上下文，以及 Tool 的真实风险和确认要求。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.modules.agent.planner import PlannerOutput, PlannerStep
from app.modules.agent.planner_contracts import PlannerDecision
from app.modules.llm.client import LLMResponseError, OpenAICompatibleLLMClient


ADAPTIVE_PLANNER_SYSTEM_PROMPT = """你是 File Agent 的 Adaptive Planner。
只输出符合 PlannerDecision schema 的 JSON，不直接执行 Tool，不编造文件事实。

你只能选择 catalog_snapshot 中 ACTIVE Skill 允许的 Tool。Tool 输入必须符合 input_schema；
前序 Tool 输出只能通过 bindings 引用，不能使用模板、代码、Shell、SQL 或路径表达式。
文件 document_id 只能使用 attachments/context_documents 中后端提供的 ID，不能猜测。
高风险 Tool 的确认要求以 Catalog 为准，不能降低。

不需要文件事实的普通对话使用 DIRECT_RESPONSE。缺少唯一文件范围或必要参数时使用 CLARIFY。
当用户明确要求“对上传文件进行分类/归类/整理”且 attachments 已提供后端 document_id 时，
必须使用 document-classification Skill 允许的 extract-document-text；不得改成
read-classification-taxonomy。后者只用于用户明确询问分类目录、分类体系或支持哪些分类。
当 observation 已包含足以满足原始目标的 Tool 结果时使用 FINISH；FINISH 不生成文件事实文本，
最终回复由后端根据已经验证的 Tool 结果生成。
hybrid-search 属于发现型 Tool：首次计划只执行检索，观察命中数量、实际条件和受控 document_ids 后，
再决定 FINISH、调整检索条件，或调用 evidence-answer/read-document-classifications。
观察的 result_status=\"POSSIBLE_ONLY\" 表示仅有未验证候选；此时不得调用 evidence-answer
读取这些候选，也不得把它们描述为相关事实。可以 FINISH 展示候选、调整查询或请求用户补充条件。
重新检索必须改变 query、match_mode 或 phrases，不能重复相同输入。
首次调用 hybrid-search 时应优先在 literal_input.semantic_plan 中表达完整主题：
- “学校的工作总结”的 core_topics 使用完整“工作总结”，不得拆成“工作”和“总结”；
- 此处“学校”默认是当前学校工作区，不是每份文件必须包含的字面词；scope.organization_level 使用
  ANY，可偏好 UNIVERSITY，并按 organization_level、business_topic、year 分组；
- 只有“只找学校层面/校级”才把 organization_level 设为 UNIVERSITY；明确的学院或部门名称才放入
  organization_terms；不得在 semantic_plan 中生成路径、SQL、document_id 或文件事实。
当前消息包含完整文件名时，该名称是不能放宽的文件范围；不得借用上一轮 search_context 扩大范围。
询问“分类为何成立、为什么归到某类”时读取 read-document-classifications；询问正文与某主题的关系、
文件内容、总结或具体事实时使用 evidence-answer。需要先确定文件时先 hybrid-search，观察真实
document_ids 后再选择上述读取 Tool。
所有成熟 Tool 都会返回统一的脱敏 observation：其中只含成功/失败状态、允许继续使用的受控文件范围、
数量、是否有证据、是否已生成待确认计划及可选 decision 类型。不得把 observation 当作正文、路径或
文件事实来源。观察后可选择 TOOL_PLAN（继续或换 Tool）、CLARIFY 或 FINISH。
若 observation.requires_user_confirmation 为 true，必须 FINISH；确认后的执行只能由后端确认接口调用
confirmed-file-action，后者永不出现在 Catalog 中。若 waiting_for_async_job 为 true，不得重复调用同一
副作用 Tool，应 FINISH 并等待异步结果。重命名建议、删除/恢复/移动计划只会创建 OperationPlan，
不会执行任何工作副本变更。
图片或扫描件结构化抽取必须使用 image-structured-extraction：用户明确列出字段时使用
EXPLICIT_FIELDS 且只能包含用户要求的字段；用户要求“全部信息”且未列字段时使用 AUTO_DISCOVER。
presentation 必须遵循用户明确要求；首次调用只能使用 INITIAL。只有安全 observation 中
structured_extraction.retryable=true 时才允许一次 VISION_CROP，target_field_keys 必须是
low_confidence_field_keys 的子集。不得提供图片路径、bbox、模型名、Prompt 或执行参数。
quality_band=HIGH 且 review_count=0 时 FINISH；增强后仍不确定时 FINISH 或 CLARIFY，禁止无限重试。
现有 Catalog 确实无法完成明确用户目标时，可以输出 capability_suggestions，但建议不能进入当前 ToolPlan，
不能生成 handler 代码，也不能声称能力已经存在、已经执行或已经成功保存建议。"""


class AdaptivePlannerService:
    """调用 LLM 生成独立 PlannerDecision，并执行 schema 校验。"""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        """允许测试注入 deterministic fake，默认复用 OpenAI-compatible 配置。"""

        self.settings = settings or get_settings()
        self.enabled = (
            self.settings.llm_enabled
            and self.settings.adaptive_planner_mode in {"shadow", "enabled"}
        )
        self.client = client

    def decide(
        self,
        *,
        message: str,
        attachments: list[dict[str, Any]],
        context_documents: list[dict[str, Any]],
        observation: dict[str, Any] | None,
        catalog_snapshot: dict[str, Any],
    ) -> PlannerDecision:
        """生成 PlannerDecision；模型响应异常统一转为可降级错误。"""

        client = self.client or OpenAICompatibleLLMClient(
            api_key=self.settings.llm_api_key,
            base_url=self.settings.llm_base_url,
            model=self.settings.llm_chat_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
        )
        parsed = client.complete_json(
            system_prompt=ADAPTIVE_PLANNER_SYSTEM_PROMPT,
            user_payload={
                "message": message,
                "attachments": attachments,
                "context_documents": context_documents,
                "observation": observation or {},
                "catalog_snapshot": catalog_snapshot,
                "output_schema": PlannerDecision.model_json_schema(),
            },
        )
        try:
            return PlannerDecision.model_validate(parsed)
        except ValidationError as exc:
            raise LLMResponseError(
                f"Adaptive Planner 响应不符合 PlannerDecision schema：{exc}"
            ) from exc


def validate_and_convert_decision(
    *,
    decision: PlannerDecision,
    registry: Any,
    catalog_snapshot: dict[str, Any],
    attachments: list[dict[str, Any]],
    context_documents: list[dict[str, Any]],
    observed_document_ids: list[str] | None = None,
    has_tool_observation: bool = False,
    observation: dict[str, Any] | None = None,
) -> tuple[PlannerOutput, dict[str, Any]]:
    """把 Adaptive 决策转换为现有执行计划，并以后端 Catalog 强制风险边界。"""

    allowed_decisions = _allowed_observation_decisions(observation)
    if allowed_decisions and decision.decision_type not in allowed_decisions:
        raise LLMResponseError(
            "Adaptive Planner 的下一步决策不符合后端执行观察约束："
            f"{decision.decision_type} not in {sorted(allowed_decisions)}"
        )

    tool_catalog = {
        str(item.get("name") or ""): item
        for item in catalog_snapshot.get("tools", [])
    }
    skill_catalog = {
        str(item.get("id") or ""): item
        for item in catalog_snapshot.get("skills", [])
    }
    unknown_skills = sorted(
        set(decision.selected_skill_ids) - set(skill_catalog)
    )
    if unknown_skills:
        raise LLMResponseError(
            f"Adaptive Planner 引用了未知 Skill：{unknown_skills}"
        )
    authorized_document_ids = {
        str(item.get("document_id") or "")
        for item in [*attachments, *context_documents]
        if item.get("document_id")
    }
    # 搜索 Tool 的真实结果可以成为同一 AgentRun 后续步骤的授权范围；这些 ID
    # 必须来自后端安全观察，不能采用模型在第二轮自由生成的任意 ID。
    authorized_document_ids.update(
        str(item)
        for item in (observed_document_ids or [])
        if str(item)
    )
    _validate_document_ids(
        proposed=decision.scope.document_ids,
        authorized_document_ids=authorized_document_ids,
        source="Planner scope",
    )
    if decision.decision_type == "DIRECT_RESPONSE":
        safe_direct_intents = {
            "GENERAL_CHAT",
            "CHAT",
            "UNKNOWN",
            "UNSPECIFIED",
            "CAPABILITY_UNAVAILABLE",
            "UNSUPPORTED_REQUEST",
        }
        if (
            str(decision.intent or "").upper() not in safe_direct_intents
            or bool(decision.scope.document_ids)
            or bool(decision.scope.requires_backend_resolution)
            or bool(attachments)
            or bool(context_documents)
        ):
            # 文件事实必须回退到 Legacy 或重新生成 ToolPlan，不能把非法直接回复
            # 悄悄改造成 intent-summary 后返回“我已收到”。
            raise LLMResponseError(
                "Adaptive Planner 不能通过 DIRECT_RESPONSE 回答文件事实"
            )
    if decision.decision_type == "FINISH" and not (
        has_tool_observation or observation
    ):
        # FINISH 只能结束已经有受控 Tool 事实的循环；第一轮直接 FINISH 会让后端
        # 在没有任何业务结果时生成空泛回复。
        raise LLMResponseError(
            "Adaptive Planner 的 FINISH 缺少 Tool 观察"
        )
    user_intent_plan = {
        "decision_type": decision.decision_type,
        "direct_response": decision.direct_response,
        "clarification_question": (
            decision.clarification.question
            if decision.clarification is not None
            else None
        ),
        "target_scope": decision.scope.source,
        "referenced_document_ids": decision.scope.document_ids,
        "capability_suggestions": [
            suggestion.model_dump()
            for suggestion in decision.capability_suggestions
        ],
        "source": "adaptive_planner",
    }
    if decision.decision_type != "TOOL_PLAN":
        intent = (
            "MISSING_FILE_SCOPE"
            if decision.decision_type == "CLARIFY"
            else decision.intent
        )
        return (
            PlannerOutput(
                intent=intent,
                user_goal=decision.user_goal,
                slots={
                    "document_ids": decision.scope.document_ids,
                    "clarification_question": user_intent_plan[
                        "clarification_question"
                    ],
                },
                selected_skills=decision.selected_skill_ids
                or ["chat-intake"],
                steps=[
                    PlannerStep(
                        step_id="adaptive-response-audit",
                        skill="chat-intake",
                        tool_name="intent-summary",
                        input={
                            "intent": decision.intent,
                            "user_goal": decision.user_goal,
                        },
                    )
                ],
                evidence_policy={
                    "require_page_or_cell": False,
                    "allow_no_evidence_answer": True,
                },
                confirmation_policy={"operation_plan_required": False},
            ),
            user_intent_plan,
        )

    assert decision.tool_plan is not None
    steps: list[PlannerStep] = []
    for adaptive_step in decision.tool_plan.steps:
        tool = tool_catalog.get(adaptive_step.tool_name)
        if tool is None:
            raise LLMResponseError(
                f"Adaptive Planner 引用了未知或未启用 Tool：{adaptive_step.tool_name}"
            )
        skill = skill_catalog.get(adaptive_step.skill_id)
        if skill is None:
            raise LLMResponseError(
                f"Adaptive Planner 步骤引用未知 Skill：{adaptive_step.skill_id}"
            )
        if adaptive_step.tool_name not in set(skill.get("allowed_tools", [])):
            raise LLMResponseError(
                f"Skill {adaptive_step.skill_id} 不允许调用 Tool {adaptive_step.tool_name}"
            )
        _validate_document_scope(
            literal_input=adaptive_step.literal_input,
            authorized_document_ids=authorized_document_ids,
        )
        # Registry 是最终事实来源；即使 CatalogSnapshot 被旧 checkpoint 复用，也要重新取定义。
        definition = registry.get(adaptive_step.tool_name)
        steps.append(
            PlannerStep(
                step_id=adaptive_step.step_id,
                skill=adaptive_step.skill_id,
                tool_name=adaptive_step.tool_name,
                input=adaptive_step.literal_input,
                bindings=adaptive_step.bindings,
                requires_confirmation=(
                    definition.requires_confirmation
                    or adaptive_step.requires_confirmation
                ),
                risk_level=definition.risk_level,
                expected_outputs=(
                    [adaptive_step.expected_output_kind]
                    if adaptive_step.expected_output_kind
                    else []
                ),
                writes=list(definition.writes),
            )
        )
    return (
        PlannerOutput(
            intent=decision.intent,
            user_goal=decision.user_goal,
            slots={"document_ids": decision.scope.document_ids},
            selected_skills=decision.selected_skill_ids,
            steps=steps,
            evidence_policy={
                "require_page_or_cell": True,
                "allow_no_evidence_answer": False,
            },
            confirmation_policy={
                "operation_plan_required": any(
                    step.requires_confirmation for step in steps
                )
            },
        ),
        user_intent_plan,
    )


def _allowed_observation_decisions(
    observation: dict[str, Any] | None,
) -> set[str]:
    """计算本轮所有 Tool 观察共同允许的决策，不能只依赖提示词约束模型。"""

    if not isinstance(observation, dict):
        return set()
    result_constraints = []
    for item in observation.get("results", []):
        if not isinstance(item, dict):
            continue
        values = {
            str(value)
            for value in item.get("available_next_decisions", [])
            if str(value)
        }
        if values:
            result_constraints.append(values)
    if not result_constraints:
        return set()
    allowed = set(result_constraints[0])
    for values in result_constraints[1:]:
        allowed.intersection_update(values)
    return allowed


def _validate_document_scope(
    *,
    literal_input: dict[str, Any],
    authorized_document_ids: set[str],
) -> None:
    """拒绝模型在 Tool 字面量输入中编造未授权 document_id。"""

    proposed: list[str] = []
    if literal_input.get("document_id"):
        proposed.append(str(literal_input["document_id"]))
    proposed.extend(
        str(item) for item in literal_input.get("document_ids", []) if str(item)
    )
    _validate_document_ids(
        proposed=proposed,
        authorized_document_ids=authorized_document_ids,
        source="Tool literal input",
    )


def _validate_document_ids(
    *,
    proposed: list[str],
    authorized_document_ids: set[str],
    source: str,
) -> None:
    """统一拒绝 Planner scope 或 Tool 输入中的未授权文件 ID。"""

    unknown = sorted(
        {str(item) for item in proposed if str(item)}
        - authorized_document_ids
    )
    if unknown:
        raise LLMResponseError(
            f"Adaptive Planner 的 {source} 引用了未授权文件范围：{unknown}"
        )
