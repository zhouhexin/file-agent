"""MVP File Agent Runtime 的 LangGraph 状态图。

当前图保持最小实现，但已经拆分 intake、planning、Tool dispatch、证据/变更处理和响应生成等边界。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime

from app.core.logging import log_context, log_event
from app.modules.agent.adaptive_planner import validate_and_convert_decision
from app.modules.agent.binding_resolver import (
    ToolBindingError,
    ToolResultBindingResolver,
)
from app.modules.agent.planner import (
    build_plan_from_user_intent,
    has_explicit_filename_content_request,
)
from app.modules.agent.planner_contracts import (
    PlannerClarification,
    PlannerDecision,
    PlannerScope,
    ToolPlan,
    ToolStep,
)
from app.modules.agent.runtime import AgentRuntimeContext
from app.modules.agent.state import AgentGraphState, ToolInvocationRecord
from app.modules.agent.tool_contracts import ExecutionObservation, ToolOutputValidationError
from app.modules.agent.tool_registry import UnknownToolError
from app.modules.agent.tool_schemas import ToolInputValidationError
from app.modules.classification.result_builder import build_document_results_from_extraction_results
from app.modules.llm.client import LLMResponseError
# 两个表格结果格式化器，用于最终 response 阶段生成自然语言回复。
from app.modules.spreadsheet_analysis.formatter import format_spreadsheet_analysis_response
from app.modules.spreadsheet_workbench.formatter import format_spreadsheet_workbench_response


MAX_PLANNING_ROUNDS = 3
MAX_TOOL_CALLS = 5


# 构建 LangGraph 主流程
def build_agent_graph():
    """编译带受控决策分支和最多 3 轮规划的 LangGraph 工作流。"""

    graph = StateGraph(AgentGraphState, context_schema=AgentRuntimeContext)
    graph.add_node("chat_intake", _logged_node("chat_intake", chat_intake))
    graph.add_node("collect_context", _logged_runtime_node("collect_context", collect_context))
    graph.add_node(
        "build_catalog_snapshot",
        _logged_runtime_node("build_catalog_snapshot", build_catalog_snapshot),
    )
    graph.add_node("planning", _logged_runtime_node("planning", planning))
    graph.add_node(
        "record_capability_suggestions",
        _logged_runtime_node(
            "record_capability_suggestions",
            record_capability_suggestions,
        ),
    )
    graph.add_node("direct_response", _logged_node("direct_response", direct_response))
    graph.add_node(
        "clarification_response",
        _logged_node("clarification_response", clarification_response),
    )
    graph.add_node("tool_dispatch", _logged_runtime_node("tool_dispatch", tool_dispatch))
    graph.add_node(
        "observe_tool_result",
        _logged_runtime_node("observe_tool_result", observe_tool_result),
    )
    graph.add_node("async_job_wait", _logged_node("async_job_wait", async_job_wait))
    graph.add_node("evidence_or_change", _logged_runtime_node("evidence_or_change", evidence_or_change))
    graph.add_node("response", _logged_runtime_node("response", response))

    graph.set_entry_point("chat_intake")
    graph.add_edge("chat_intake", "collect_context")
    graph.add_edge("collect_context", "build_catalog_snapshot")
    graph.add_edge("build_catalog_snapshot", "planning")
    graph.add_edge("planning", "record_capability_suggestions")
    graph.add_conditional_edges(
        "record_capability_suggestions",
        route_after_planning,
        {
            "tool_dispatch": "tool_dispatch",
            "direct_response": "direct_response",
            "clarification_response": "clarification_response",
            "finalize": "evidence_or_change",
        },
    )
    graph.add_edge("direct_response", END)
    graph.add_edge("clarification_response", END)
    graph.add_edge("tool_dispatch", "observe_tool_result")
    graph.add_conditional_edges(
        "observe_tool_result",
        route_after_observation,
        {
            "planning": "planning",
            "tool_dispatch": "tool_dispatch",
            "async_job_wait": "async_job_wait",
        },
    )
    graph.add_edge("async_job_wait", "evidence_or_change")
    graph.add_edge("evidence_or_change", "response")
    graph.add_edge("response", END)
    return graph.compile()


def _logged_node(name: str, handler):
    """为 LangGraph 节点增加进入、退出、耗时日志。"""

    # 内层包装函数，state是LangGraph运行时自动注入的实参
    def wrapped(state: AgentGraphState):
        """执行节点并记录结构化日志。"""
        # 此处state = 图引擎传入的当前会话全局可变状态
        return _run_logged_node(name=name, state=state, callback=lambda: handler(state))

    return wrapped


def _logged_runtime_node(name: str, handler):
    """为需要 Runtime 注入的 LangGraph 节点增加日志，同时保留显式签名。"""

    def wrapped(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]):
        """执行带 Runtime 的节点并记录结构化日志。"""
        # 产出节点运行结果
        return _run_logged_node(name=name, state=state, callback=lambda: handler(state, runtime))
    # 产出一个新函数
    return wrapped


def _run_logged_node(name: str, state: AgentGraphState, callback):
    """执行节点回调并记录统一的节点日志。"""

    start = time.perf_counter()
    with log_context(
        agent_run_id=state.get("agent_run_id"),
        user_id=state.get("user_id"),
        conversation_id=state.get("conversation_id"),
    ):
        log_event(
            "agent.node.entered",
            status=state.get("status"),
            message="Agent 节点开始",
            node=name,
        )
        try:
            result = callback()
        except Exception as exc:
            log_event(
                "agent.node.failed",
                level="ERROR",
                status="FAILED",
                duration_ms=int((time.perf_counter() - start) * 1000),
                error_code=exc.__class__.__name__,
                message=str(exc),
                node=name,
            )
            raise
        log_event(
            "agent.node.completed",
            status=result.get("status", state.get("status")) if isinstance(result, dict) else state.get("status"),
            duration_ms=int((time.perf_counter() - start) * 1000),
            message="Agent 节点完成",
            node=name,
        )
        return result


def chat_intake(state: AgentGraphState) -> Dict[str, Any]:
    """在规划前初始化运行状态。

    此节点不执行副作用，只负责把运行状态带入受控 Planner 路径。
    """

    return {
        "status": "PLANNING",
        "errors": state.get("errors", []),
        "tool_results": state.get("tool_results", []),
        "tool_invocations": state.get("tool_invocations", []),
        "planning_round": state.get("planning_round", 0),
        "tool_call_count": state.get("tool_call_count", 0),
        "executed_tool_signatures": state.get("executed_tool_signatures", []),
        "last_dispatch_results": [],
        "last_dispatch_tool_name": None,
        "last_dispatch_step_id": None,
        "observation": state.get("observation", {}),
        "search_attempts": state.get("search_attempts", []),
        "effective_conditions": state.get("effective_conditions", []),
        "observed_document_ids": state.get("observed_document_ids", []),
        "replan_requested": False,
        "waiting_for_confirmation": False,
    }


def collect_context(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """加载 LLM 理解用户需求所需的文件上下文。"""

    return {
        "context_documents": runtime.context.context_loader.load_documents(
            user_id=state["user_id"],
            attachments=state.get("attachments", []),
        )
    }


def build_catalog_snapshot(
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
) -> Dict[str, Any]:
    """把运行时完整 Catalog 投影为可持久化身份，不把大段 schema 写入 State。"""

    snapshot = runtime.context.catalog_snapshot
    return {
        "catalog_snapshot": {
            "catalog_version": snapshot.get("catalog_version"),
            "catalog_fingerprint": snapshot.get("catalog_fingerprint"),
            "enabled_tool_names": snapshot.get("enabled_tool_names", []),
            "enabled_skill_ids": snapshot.get("enabled_skill_ids", []),
        }
    }


def planning(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """调用 Planner，并且只保存通过校验的声明式计划和受控决策。"""

    planning_round = int(state.get("planning_round", 0)) + 1
    planning_attachments = _planning_attachments(state)
    shadow_planner_decision: Dict[str, Any] = {}
    if state.get("planner_mode") == "llm":
        # 重规划必须消费上一轮观察，不能再次被同一个 deterministic preflight 截回原计划。
        preflight_plan = (
            _deterministic_preflight_plan(
                state=state,
                runtime=runtime,
                attachments=planning_attachments,
            )
            if planning_round == 1
            else None
        )
        if (
            preflight_plan is not None
            and runtime.context.adaptive_planner_mode == "enabled"
            and not (
                preflight_plan.intent == "EVIDENCE_ANSWER"
                and has_explicit_filename_content_request(state["message"])
            )
        ):
            # enabled 模式由 Catalog Planner 选择成熟 Tool；只有完整文件名构成后端已验证的硬对象范围，
            # 不能让模型改写为全库检索。其余旧关键词预检只保留给 Legacy/Shadow 与故障降级。
            preflight_plan = None
        if preflight_plan is not None:
            update = _planner_state_update(
                plan=preflight_plan,
                user_intent_plan={"source": "deterministic_preflight"},
                planning_round=planning_round,
            )
            if runtime.context.adaptive_planner_mode == "shadow":
                shadow_planner_decision = _run_shadow_planner(
                    state=state,
                    runtime=runtime,
                    attachments=planning_attachments,
                )
                update["shadow_planner_decision"] = shadow_planner_decision
            return update
        try:
            if runtime.context.adaptive_planner_mode == "enabled":
                decision = _request_adaptive_decision(
                    state=state,
                    runtime=runtime,
                    attachments=planning_attachments,
                )
                plan, user_intent_plan = validate_and_convert_decision(
                    decision=decision,
                    registry=runtime.context.registry,
                    catalog_snapshot=runtime.context.catalog_snapshot,
                    attachments=planning_attachments,
                    context_documents=state.get("context_documents", []),
                    observed_document_ids=state.get(
                        "observed_document_ids", []
                    ),
                    has_tool_observation=bool(state.get("observation")),
                    observation=state.get("observation") or None,
                )
            else:
                plan, user_intent_plan = _run_legacy_llm_planner(
                    state=state,
                    runtime=runtime,
                    attachments=planning_attachments,
                    planning_round=planning_round,
                )
                if runtime.context.adaptive_planner_mode == "shadow":
                    shadow_planner_decision = _run_shadow_planner(
                        state=state,
                        runtime=runtime,
                        attachments=planning_attachments,
                    )
        except LLMResponseError as exc:
            # Adaptive 失败时先回退已经验证的 Legacy LLM 链路；若模型网关整体不可用，
            # Legacy 同样会抛出 LLMResponseError，再进入确定性降级。
            log_event(
                "llm.intent.fallback",
                level="WARNING",
                status="FAILED",
                error_code=exc.__class__.__name__,
                message=str(exc),
            )
            if (
                runtime.context.adaptive_planner_mode == "enabled"
                and state.get("observation")
            ):
                # 已经执行过 Tool 后，Adaptive 输出异常或网关失败时只能基于现有验证结果结束。
                # 重新交给 Legacy 解释原请求可能再次创建不同的副作用计划，违反“不盲目重试”边界。
                plan, user_intent_plan = _finish_after_observation_failure(
                    state=state,
                    runtime=runtime,
                    attachments=planning_attachments,
                    adaptive_error=exc,
                )
            else:
                plan, user_intent_plan = _fallback_planner(
                    state=state,
                    runtime=runtime,
                    attachments=planning_attachments,
                    planning_round=planning_round,
                    adaptive_error=exc,
                )
    else:
        plan = runtime.context.planner.plan(
            conversation_id=state["conversation_id"],
            user_id=state["user_id"],
            message_id=state["message_id"],
            message=state["message"],
            attachments=planning_attachments,
        )
        user_intent_plan = {}
    log_event(
        "agent.planning.final_tool_plan",
        status="COMPLETED",
        intent=plan.intent,
        tool_name=plan.steps[0].tool_name if plan.steps else None,
        tool_input=plan.steps[0].input if plan.steps else {},
    )
    # 把 plan 转成 LangGraph state 更新
    update = _planner_state_update(
        plan=plan,
        user_intent_plan=user_intent_plan,
        planning_round=planning_round,
    )
    if shadow_planner_decision:
        update["shadow_planner_decision"] = shadow_planner_decision
    return update


def _fallback_planner(
    *,
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
    attachments: List[Dict[str, Any]],
    planning_round: int,
    adaptive_error: LLMResponseError,
):
    """Adaptive 失败时优先回退 Legacy，网关失败时再使用确定性 Planner。"""

    if runtime.context.adaptive_planner_mode == "enabled":
        try:
            plan, intent_plan = _run_legacy_llm_planner(
                state=state,
                runtime=runtime,
                attachments=attachments,
                planning_round=planning_round,
            )
            intent_plan["fallback_reason"] = "ADAPTIVE_PLANNER_FAILED"
            intent_plan["adaptive_error_code"] = adaptive_error.__class__.__name__
            return plan, intent_plan
        except LLMResponseError as legacy_error:
            log_event(
                "llm.intent.legacy_fallback",
                level="WARNING",
                status="FAILED",
                error_code=legacy_error.__class__.__name__,
                message=str(legacy_error),
            )
    plan = runtime.context.planner.plan(
        conversation_id=state["conversation_id"],
        user_id=state["user_id"],
        message_id=state["message_id"],
        message=state["message"],
        attachments=attachments,
    )
    return plan, {
        "fallback_reason": "LLM_INTENT_FAILED",
        "error_code": adaptive_error.__class__.__name__,
        "message": str(adaptive_error),
    }


def _finish_after_observation_failure(
    *,
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
    attachments: List[Dict[str, Any]],
    adaptive_error: LLMResponseError,
):
    """重规划异常时关闭式结束，保留已验证结果且不重复执行任何 Tool。"""

    existing_slots = dict(state.get("slots", {}))
    document_ids = list(
        dict.fromkeys(
            [
                *list(existing_slots.get("document_ids", [])),
                *list(state.get("observed_document_ids", [])),
            ]
        )
    )
    decision = PlannerDecision(
        decision_type="FINISH",
        intent=str(state.get("intent") or "TOOL_RESULT_AVAILABLE"),
        user_goal=str(state.get("message") or "完成当前文件任务"),
        selected_skill_ids=list(state.get("selected_skills") or ["chat-intake"]),
        scope=PlannerScope(
            document_ids=document_ids,
            source="tool_observation",
        ),
    )
    plan, intent_plan = validate_and_convert_decision(
        decision=decision,
        registry=runtime.context.registry,
        catalog_snapshot=runtime.context.catalog_snapshot,
        attachments=attachments,
        context_documents=state.get("context_documents", []),
        observed_document_ids=state.get("observed_document_ids", []),
        has_tool_observation=True,
        observation=state.get("observation") or None,
    )
    plan = plan.model_copy(update={"slots": existing_slots})
    intent_plan["fallback_reason"] = "ADAPTIVE_REPLAN_FAILED_AFTER_TOOL"
    intent_plan["adaptive_error_code"] = adaptive_error.__class__.__name__
    return plan, intent_plan


def _run_legacy_llm_planner(
    *,
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
    attachments: List[Dict[str, Any]],
    planning_round: int,
):
    """运行现有 UserIntentPlan 兼容链路，作为 Legacy Planner 用户可见基线。"""

    llm_request = {
        "message": state["message"],
        "attachments": attachments,
        "context_documents": state.get("context_documents", []),
    }
    if state.get("observation"):
        llm_request["observation"] = state["observation"]
    intent_plan = _understand_user_request(
        service=runtime.context.llm_intent_service,
        request=llm_request,
        catalog_snapshot=runtime.context.catalog_snapshot,
    )
    log_event(
        "agent.planning.llm_intent",
        status="COMPLETED",
        intent=intent_plan.intent,
        decision_type=intent_plan.decision_type,
        planning_round=planning_round,
        target_scope=intent_plan.target_scope,
        required_capabilities=intent_plan.required_capabilities,
        tool_plan_hint=intent_plan.tool_plan_hint,
    )
    plan = build_plan_from_user_intent(
        intent_plan=intent_plan,
        message=state["message"],
        attachments=attachments,
    )
    _validate_legacy_llm_plan_against_catalog(
        plan=plan,
        intent_plan=intent_plan,
        catalog_snapshot=runtime.context.catalog_snapshot,
    )
    return plan, intent_plan.model_dump()


def _validate_legacy_llm_plan_against_catalog(
    *,
    plan: Any,
    intent_plan: Any,
    catalog_snapshot: Dict[str, Any],
) -> None:
    """Legacy LLM 兼容输出也只能引用本次 Catalog 中已启用的 Tool/Skill。"""

    if str(intent_plan.decision_type or "TOOL_PLAN") in {
        "DIRECT_RESPONSE",
        "CLARIFY",
    }:
        # 兼容链路会创建不执行的 intent-summary 占位步骤，实际图分支不会 Dispatch。
        return
    tool_names = set(catalog_snapshot.get("enabled_tool_names", []))
    for step in plan.steps:
        if step.tool_name not in tool_names:
            raise LLMResponseError(
                f"Legacy Planner 引用了 Catalog 外 Tool：{step.tool_name}"
            )


def _request_adaptive_decision(
    *,
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
    attachments: List[Dict[str, Any]],
) -> PlannerDecision:
    """调用 Adaptive Planner；这里只生成决策，不执行任何 Tool。"""

    return runtime.context.adaptive_planner_service.decide(
        message=state["message"],
        attachments=attachments,
        context_documents=state.get("context_documents", []),
        observation=state.get("observation") or None,
        catalog_snapshot=runtime.context.catalog_snapshot,
    )


def _run_shadow_planner(
    *,
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
    attachments: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """只读运行 Adaptive Planner；失败只记录状态，绝不改变用户计划。"""

    decision: PlannerDecision | None = None
    try:
        decision = _request_adaptive_decision(
            state=state,
            runtime=runtime,
            attachments=attachments,
        )
        # Shadow 也必须执行 Catalog 与文件范围校验，但转换结果绝不送入 Dispatcher。
        normalized_plan, _intent_projection = validate_and_convert_decision(
            decision=decision,
            registry=runtime.context.registry,
            catalog_snapshot=runtime.context.catalog_snapshot,
            attachments=attachments,
            context_documents=state.get("context_documents", []),
            observed_document_ids=state.get("observed_document_ids", []),
            has_tool_observation=bool(state.get("observation")),
            observation=state.get("observation") or None,
        )
        return {
            "validation_status": "COMPLETED",
            "decision": decision.model_dump(exclude_none=True),
            "normalized_tool_plan": normalized_plan.model_dump(),
        }
    except Exception as exc:
        log_event(
            "agent.planner_shadow.failed",
            level="WARNING",
            status="FAILED",
            error_code=exc.__class__.__name__,
            message=str(exc),
        )
        return {
            "validation_status": "FAILED",
            "error_code": exc.__class__.__name__,
            "decision": (
                decision.model_dump(exclude_none=True)
                if decision is not None
                else None
            ),
        }


def _understand_user_request(
    *,
    service: Any,
    request: Dict[str, Any],
    catalog_snapshot: Dict[str, Any],
):
    """兼容测试 fake，并向支持新契约的 LLM 服务注入完整请求级 Catalog。"""

    parameters = inspect.signature(service.understand_user_request).parameters
    if "catalog_snapshot" in parameters:
        request["catalog_snapshot"] = catalog_snapshot
    return service.understand_user_request(**request)


def _deterministic_preflight_plan(
    *,
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
    attachments: List[Dict[str, Any]],
):
    """在 LLM 前固定安全操作和用户已经明确给出的文件范围。

    完整文件名不是业务关键词路由，而是后端可验证的对象约束。此类正文请求必须
    先进入 ``evidence-answer`` 做同名检测和活动副本校验，不能允许模型把它改成
    全工作区 ``hybrid-search`` 后进入无关的后台准备链。
    """

    plan = runtime.context.planner.plan(
        conversation_id=state["conversation_id"],
        user_id=state["user_id"],
        message_id=state["message_id"],
        message=state["message"],
        attachments=attachments,
    )
    if plan.intent in {
        "SUGGEST_RENAME",
        "CLASSIFY_AND_SUGGEST_RENAME",
        "RESOLVE_RENAME_REVIEW",
        "PREPARE_WORKING_COPY_ACTION",
    }:
        return plan
    if plan.intent.endswith("_CLASSIFICATION"):
        return plan
    if (
        plan.intent == "EVIDENCE_ANSWER"
        and has_explicit_filename_content_request(state["message"])
    ):
        return plan
    return None


def _planning_attachments(state: AgentGraphState) -> List[Dict[str, Any]]:
    """把附件 ID 与后端已校验的文档元数据合并，仅供本次 Planning 使用。

    用户请求仍只能提交 ``document_id``；文件名、MIME 和状态必须来自 ContextLoader 的授权查询，
    不能采用前端自报值，也不能把服务对象或正文写入 Graph State。
    """

    context_by_document_id = {
        str(document.get("document_id") or ""): document
        for document in state.get("context_documents", [])
        if document.get("document_id")
    }
    enriched: List[Dict[str, Any]] = []
    for attachment in state.get("attachments", []):
        document_id = str(attachment.get("document_id") or "")
        if not document_id:
            continue
        context_document = context_by_document_id.get(document_id, {})
        enriched.append(
            {
                **attachment,
                "document_id": document_id,
                "filename": context_document.get("filename"),
                "content_type": context_document.get("content_type"),
                "status": context_document.get("status"),
                "ingest_status": context_document.get("ingest_status"),
            }
        )
    return enriched


def _planner_state_update(
    *,
    plan,
    user_intent_plan: Dict[str, Any],
    planning_round: int,
) -> Dict[str, Any]:
    """把 Planner 输出转换为 LangGraph State 更新，并固化三类决策。"""

    decision_type = str(user_intent_plan.get("decision_type") or "TOOL_PLAN")
    if plan.intent == "MISSING_FILE_SCOPE":
        decision_type = "CLARIFY"
    elif decision_type == "CLARIFY":
        # LLM 不能用澄清分支跳过后端已经能够执行的文件计划。
        decision_type = "TOOL_PLAN"
    if decision_type == "DIRECT_RESPONSE" and not _is_direct_response_plan(
        plan=plan,
        user_intent_plan=user_intent_plan,
    ):
        # 直接回复只允许无文件事实的普通对话；文件任务仍必须经过白名单 Tool。
        decision_type = "TOOL_PLAN"
    direct_response_text = str(user_intent_plan.get("direct_response") or "").strip() or None
    clarification_question = (
        str(
            user_intent_plan.get("clarification_question")
            or plan.slots.get("clarification_question")
            or ""
        ).strip()
        or None
    )
    planner_decision = _build_planner_decision(
        plan=plan,
        user_intent_plan=user_intent_plan,
        decision_type=decision_type,
        direct_response=direct_response_text,
        clarification_question=clarification_question,
        planning_round=planning_round,
    )
    return {
        "intent": plan.intent,
        "slots": plan.slots,
        "selected_skills": plan.selected_skills,
        "tool_plan": plan.model_dump(),
        "user_intent_plan": user_intent_plan,
        "planner_decision": planner_decision.model_dump(exclude_none=True),
        "capability_suggestions": [
            item.model_dump()
            for item in planner_decision.capability_suggestions
        ],
        "decision_type": decision_type,
        "direct_response": direct_response_text,
        "clarification_question": clarification_question,
        "planning_round": planning_round,
        "current_step_index": 0,
        # 新计划使用独立步骤命名空间；历史调用仍留在 ToolInvocation 审计列表，
        # 但不能因重用 step_id 被本轮绑定解析器误读。
        "step_results": {},
        "completed_step_ids": [],
        "failed_step_ids": [],
        "replan_requested": False,
        "waiting_for_confirmation": False,
        "status": "RUNNING_TOOL" if decision_type == "TOOL_PLAN" else "SUMMARIZING",
    }


def _build_planner_decision(
    *,
    plan: Any,
    user_intent_plan: Dict[str, Any],
    decision_type: str,
    direct_response: str | None,
    clarification_question: str | None,
    planning_round: int,
) -> PlannerDecision:
    """把现有 PlannerOutput 适配为独立 PlannerDecision，逐步移除占位 Tool 依赖。"""

    suggestions = user_intent_plan.get("capability_suggestions", [])
    if decision_type == "DIRECT_RESPONSE":
        return PlannerDecision(
            decision_type="DIRECT_RESPONSE",
            intent=plan.intent,
            user_goal=plan.user_goal,
            selected_skill_ids=plan.selected_skills,
            scope=PlannerScope(
                document_ids=list(plan.slots.get("document_ids", [])),
                source=str(user_intent_plan.get("target_scope") or "unspecified"),
            ),
            capability_suggestions=suggestions,
            direct_response=direct_response or "我已收到。",
            confidence=0.5,
        )
    if decision_type == "CLARIFY":
        return PlannerDecision(
            decision_type="CLARIFY",
            intent=plan.intent,
            user_goal=plan.user_goal,
            selected_skill_ids=plan.selected_skills,
            scope=PlannerScope(
                document_ids=list(plan.slots.get("document_ids", [])),
                source=str(user_intent_plan.get("target_scope") or "unspecified"),
                requires_backend_resolution=True,
            ),
            capability_suggestions=suggestions,
            clarification=PlannerClarification(
                question=clarification_question or "请补充完成任务所需的文件范围。"
            ),
            confidence=0.5,
        )
    if decision_type == "FINISH":
        return PlannerDecision(
            decision_type="FINISH",
            intent=plan.intent,
            user_goal=plan.user_goal,
            selected_skill_ids=plan.selected_skills,
            scope=PlannerScope(
                document_ids=list(plan.slots.get("document_ids", [])),
                source=str(
                    user_intent_plan.get("target_scope")
                    or "tool_observation"
                ),
            ),
            capability_suggestions=suggestions,
            confidence=0.5,
        )
    return PlannerDecision(
        decision_type="TOOL_PLAN",
        intent=plan.intent,
        user_goal=plan.user_goal,
        # Legacy Planner 的历史测试/计划可能漏列步骤 Skill；适配为新契约时补齐，
        # 但 Adaptive LLM 原生输出仍由 PlannerDecision 严格拒绝漏选。
        selected_skill_ids=list(
            dict.fromkeys(
                [
                    *plan.selected_skills,
                    *(step.skill for step in plan.steps),
                ]
            )
        ),
        scope=PlannerScope(
            document_ids=list(plan.slots.get("document_ids", [])),
            source=str(user_intent_plan.get("target_scope") or "unspecified"),
        ),
        tool_plan=ToolPlan(
            plan_id=f"plan-{planning_round}",
            steps=[
                ToolStep(
                    step_id=step.step_id,
                    skill_id=step.skill,
                    tool_name=step.tool_name,
                    literal_input=step.input,
                    bindings=step.bindings,
                    requires_confirmation=step.requires_confirmation,
                    expected_output_kind=(
                        step.expected_outputs[0] if step.expected_outputs else None
                    ),
                )
                for step in plan.steps
            ],
        ),
        capability_suggestions=suggestions,
        confidence=0.5,
    )


def record_capability_suggestions(
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
) -> Dict[str, Any]:
    """通过内部白名单 Tool 记录经 schema 校验的能力缺口建议。"""

    suggestions = list(state.get("capability_suggestions", []))
    if not suggestions:
        return {}
    catalog = state.get("catalog_snapshot", {})
    tool_input = {
            "suggestions": suggestions,
            "user_goal": str(
                state.get("planner_decision", {}).get("user_goal")
                or state.get("message")
                or ""
            ),
            "catalog_fingerprint": str(
                catalog.get("catalog_fingerprint") or "catalog-unavailable"
            ),
            "enabled_tool_names": list(catalog.get("enabled_tool_names", [])),
            "enabled_skill_ids": list(catalog.get("enabled_skill_ids", [])),
        }
    try:
        invocation = runtime.context.registry.invoke(
            "capability-suggestion-record",
            tool_input,
        )
    except Exception as exc:
        # 建议清单是辅助审计能力，写入失败不能阻断用户的安全回复，也不能盲目
        # 重试数据库副作用；失败仍作为 ToolInvocation 进入运行回执和日志。
        log_event(
            "agent.capability_suggestion.failed",
            level="ERROR",
            status="FAILED",
            error_code=exc.__class__.__name__,
            message=str(exc),
        )
        invocation = _dispatch_rejection_invocation(
            step={"tool_name": "capability-suggestion-record"},
            tool_input=tool_input,
            code="CAPABILITY_SUGGESTION_RECORD_FAILED",
            message="能力建议暂时无法保存，管理员可通过本次运行日志复核。",
        )
    return {
        "tool_invocations": [
            *state.get("tool_invocations", []),
            invocation.model_dump(),
        ],
        "tool_results": [
            *state.get("tool_results", []),
            invocation.output_json,
        ],
    }


def _is_direct_response_plan(*, plan, user_intent_plan: Dict[str, Any]) -> bool:
    """确认 Planner 已把请求判定为不需要文件事实的普通对话。"""

    return (
        plan.intent
        in {
            "GENERAL_CHAT",
            "CHAT",
            "UNKNOWN",
            "UNSPECIFIED",
            "CAPABILITY_UNAVAILABLE",
            "UNSUPPORTED_REQUEST",
        }
        and not plan.slots.get("document_ids")
        and not user_intent_plan.get("needs_file_context")
        and not user_intent_plan.get("required_capabilities")
        and not user_intent_plan.get("tool_plan_hint")
        and bool(plan.steps)
        and all(step.tool_name == "intent-summary" for step in plan.steps)
    )


def route_after_planning(state: AgentGraphState) -> str:
    """按受控决策选择 Tool、直接回复或澄清分支。"""

    decision_type = str(state.get("decision_type") or "TOOL_PLAN")
    if decision_type == "DIRECT_RESPONSE":
        return "direct_response"
    if decision_type == "CLARIFY":
        return "clarification_response"
    if decision_type == "FINISH":
        return "finalize"
    return "tool_dispatch"


def direct_response(state: AgentGraphState) -> Dict[str, Any]:
    """返回 LLM 已生成的普通对话文本，不调用文件 Tool。"""

    return {
        "status": "COMPLETED",
        "final_response": state.get("direct_response")
        or "我已收到。请继续说明你的需求。",
    }


def clarification_response(state: AgentGraphState) -> Dict[str, Any]:
    """返回一次最关键的补充信息请求，不猜测文件范围。"""

    fallback_question = _build_general_chat_response(
        {
            "intent": "MISSING_FILE_SCOPE",
            "user_goal": state.get("message", ""),
        }
    )
    return {
        "status": "NEEDS_REVIEW",
        "final_response": state.get("clarification_question")
        or fallback_question,
    }


def tool_dispatch(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """通过白名单 Registry 执行当前一个 ToolStep，并记录步骤级结果。"""

    registry = runtime.context.registry
    tool_results: List[Dict[str, Any]] = list(state.get("tool_results", []))
    tool_invocations: List[Dict[str, Any]] = list(state.get("tool_invocations", []))
    last_dispatch_results: List[Dict[str, Any]] = []
    executed_signatures = list(state.get("executed_tool_signatures", []))
    tool_call_count = int(state.get("tool_call_count", 0))
    errors = list(state.get("errors", []))
    operation_plan_id = state.get("operation_plan_id")
    changeset_id = state.get("changeset_id")
    steps = list(state.get("tool_plan", {}).get("steps", []))
    current_step_index = int(state.get("current_step_index", 0))
    step_results = dict(state.get("step_results", {}))
    completed_step_ids = list(state.get("completed_step_ids", []))
    failed_step_ids = list(state.get("failed_step_ids", []))
    if current_step_index >= len(steps):
        return {
            "last_dispatch_results": [],
            "last_dispatch_tool_name": None,
            "last_dispatch_step_id": None,
            "status": "SUMMARIZING",
        }

    step = steps[current_step_index]
    step_id = str(step.get("step_id") or f"step-{current_step_index + 1}")
    if step["requires_confirmation"]:
        # 高风险步骤只能由用户确认后的新请求恢复执行。本次运行保留当前位置，
        # 不伪造 OperationPlan ID，也不继续后续步骤，避免绕过确认边界。
        step_results[step_id] = {
            "status": "WAITING_FOR_CONFIRMATION",
            "output": {
                "operation_plan_id": operation_plan_id,
                "message": "该操作需要先确认操作计划。",
            },
        }
        return {
            "tool_results": tool_results,
            "tool_invocations": tool_invocations,
            "last_dispatch_results": [],
            "last_dispatch_tool_name": str(step.get("tool_name") or ""),
            "last_dispatch_step_id": step_id,
            "tool_call_count": tool_call_count,
            "executed_tool_signatures": executed_signatures,
            "errors": errors,
            "changeset_id": changeset_id,
            "operation_plan_id": operation_plan_id,
            "current_step_index": current_step_index,
            "step_results": step_results,
            "completed_step_ids": completed_step_ids,
            "failed_step_ids": failed_step_ids,
            "waiting_for_confirmation": True,
            "status": "WAITING_FOR_CONFIRMATION",
        }
    else:
        try:
            literal_input = dict(step.get("input") or step.get("literal_input") or {})
            bound_input = ToolResultBindingResolver().resolve(
                literal_input=literal_input,
                bindings=list(step.get("bindings") or []),
                step_results=step_results,
            )
            tool_input = _trusted_tool_input(
                step={**step, "input": bound_input},
                state=state,
            )
        except ToolBindingError as exc:
            invocation = _dispatch_rejection_invocation(
                step=step,
                tool_input=dict(step.get("input") or {}),
                code="BINDING_VALIDATION_FAILED",
                message=str(exc),
            )
            invocation_json = invocation.model_dump()
            tool_invocations.append(invocation_json)
            tool_results.append(invocation.output_json)
            last_dispatch_results.append(invocation.output_json)
            errors.append("BINDING_VALIDATION_FAILED")
            failed_step_ids.append(step_id)
            step_results[step_id] = _step_result(invocation)
            current_step_index += 1
        else:
            signature_input = _canonical_tool_input(
                registry=registry,
                tool_name=str(step["tool_name"]),
                tool_input=tool_input,
            )
            signature = _tool_call_signature(
                tool_name=str(step["tool_name"]),
                tool_input=signature_input,
            )
            structured_budget_error = _structured_extraction_budget_error(
                tool_name=str(step["tool_name"]),
                tool_input=signature_input,
                state=state,
            )
            if structured_budget_error is not None:
                code, message = structured_budget_error
                invocation = _dispatch_rejection_invocation(
                    step=step,
                    tool_input=tool_input,
                    code=code,
                    message=message,
                )
                errors.append(code)
            elif signature in executed_signatures:
                invocation = _dispatch_rejection_invocation(
                    step=step,
                    tool_input=tool_input,
                    code="DUPLICATE_TOOL_CALL",
                    message="Agent 已阻止重复执行相同的文件操作，请补充或调整请求。",
                )
                errors.append("DUPLICATE_TOOL_CALL")
            elif tool_call_count >= MAX_TOOL_CALLS:
                invocation = _dispatch_rejection_invocation(
                    step=step,
                    tool_input=tool_input,
                    code="TOOL_CALL_BUDGET_EXCEEDED",
                    message=f"本次任务最多执行 {MAX_TOOL_CALLS} 次文件操作，请缩小文件范围后重试。",
                )
                errors.append("TOOL_CALL_BUDGET_EXCEEDED")
            else:
                try:
                    # 预算统计的是实际调用尝试；允许降级的失败同样消耗一次额度。
                    tool_call_count += 1
                    executed_signatures.append(signature)
                    invocation = registry.invoke(step["tool_name"], tool_input)
                except (
                    ToolInputValidationError,
                    ToolOutputValidationError,
                    UnknownToolError,
                ) as exc:
                    # LLM 或旧计划的 schema 错误属于可审计的计划拒绝，不能冒泡成
                    # ASGI 500；Registry 仍是拒绝发生副作用的最终边界。
                    error_code = (
                        "TOOL_INPUT_VALIDATION_FAILED"
                        if isinstance(exc, ToolInputValidationError)
                        else (
                            "TOOL_OUTPUT_VALIDATION_FAILED"
                            if isinstance(exc, ToolOutputValidationError)
                            else "UNKNOWN_TOOL"
                        )
                    )
                    invocation = _dispatch_rejection_invocation(
                        step=step,
                        tool_input=tool_input,
                        code=error_code,
                        message=str(exc),
                    )
                except Exception as exc:
                    if step["tool_name"] not in {
                        "extract-document-text",
                        "extract-image-structured-data",
                        "analyze-spreadsheet",
                        "profile-spreadsheet",
                        "validate-spreadsheet",
                    }:
                        raise
                    invocation = _failed_tool_invocation(step=step, error=exc)
            invocation_json = invocation.model_dump()
            tool_invocations.append(invocation_json)
            tool_results.append(invocation.output_json)
            last_dispatch_results.append(invocation.output_json)
            step_results[step_id] = _step_result(invocation)
            if invocation.status in {"COMPLETED", "PARTIAL"}:
                completed_step_ids.append(step_id)
            else:
                failed_step_ids.append(step_id)
            current_step_index += 1
            changeset_id = invocation.changeset_id or changeset_id
            # 同名冲突卡中的明确“覆盖”回复本身就是用户确认。该链路仍会创建并执行
            # OperationPlan 供审计，但已执行计划不能再投影成第二张待确认卡。
            if not (
                invocation.output_json.get("kind") == "working_copy_operation_result"
                and invocation.output_json.get("status") == "EXECUTED"
            ):
                operation_plan_id = invocation.operation_plan_id or operation_plan_id

    return {
        "tool_results": tool_results,
        "tool_invocations": tool_invocations,
        "last_dispatch_results": last_dispatch_results,
        "last_dispatch_tool_name": str(step.get("tool_name") or ""),
        "last_dispatch_step_id": step_id,
        "tool_call_count": tool_call_count,
        "executed_tool_signatures": executed_signatures,
        "errors": errors,
        "changeset_id": changeset_id,
        "operation_plan_id": operation_plan_id,
        "current_step_index": current_step_index,
        "step_results": step_results,
        "completed_step_ids": completed_step_ids,
        "failed_step_ids": failed_step_ids,
        "status": "SUMMARIZING",
    }


def _step_result(invocation: ToolInvocationRecord) -> Dict[str, Any]:
    """把 ToolInvocation 投影为绑定解析器可消费的步骤级结果。"""

    return {
        "status": invocation.status,
        "output": invocation.output_json,
        "invocation_id": invocation.id,
        "changeset_id": invocation.changeset_id,
        "operation_plan_id": invocation.operation_plan_id,
    }


def _trusted_tool_input(
    *,
    step: Dict[str, Any],
    state: AgentGraphState,
) -> Dict[str, Any]:
    """把受信任运行标识注入 Tool 输入，禁止 LLM 自报会话和确认文本。"""

    tool_input = dict(step["input"])
    if step["tool_name"] in {
        "generate-rename-suggestions",
        "resolve-rename-reviews",
        "working-copy-action-plan-create",
        "classification-decision",
        "classify-managed-files",
    }:
        tool_input["conversation_id"] = state["conversation_id"]
        tool_input["agent_run_id"] = state["agent_run_id"]
    if step["tool_name"] in {
        "resolve-rename-reviews",
        "working-copy-action-plan-create",
        "classification-decision",
    }:
        tool_input["message"] = state["message"]
    return tool_input


def _tool_call_signature(*, tool_name: str, tool_input: Dict[str, Any]) -> str:
    """生成稳定 Tool 调用签名，阻止重规划重复执行相同输入。"""

    serialized = json.dumps(
        {"tool_name": tool_name, "input": tool_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _canonical_tool_input(
    *,
    registry,
    tool_name: str,
    tool_input: Dict[str, Any],
) -> Dict[str, Any]:
    """用 Registry schema 规范化签名输入；测试 fake 不提供目录时保持兼容。"""

    if not hasattr(registry, "get"):
        return tool_input
    try:
        tool = registry.get(tool_name)
        return tool.input_model.model_validate(tool_input).model_dump()
    except Exception:
        # 这里只生成幂等签名；真实 invoke 仍负责抛出标准未知 Tool 或 schema 校验错误。
        return tool_input


def _structured_extraction_budget_error(
    *,
    tool_name: str,
    tool_input: Dict[str, Any],
    state: AgentGraphState,
) -> tuple[str, str] | None:
    """强制图片抽取最多一次初始执行和一次 Observation 约束的局部增强。"""

    if tool_name != "extract-image-structured-data":
        return None
    previous_calls = [
        item
        for item in state.get("tool_invocations", [])
        if item.get("tool_name") == tool_name
    ]
    if len(previous_calls) >= 2:
        return (
            "STRUCTURED_EXTRACTION_BUDGET_EXCEEDED",
            "图片结构化抽取最多执行一次初始识别和一次局部增强。",
        )
    retry_strategy = str(tool_input.get("retry_strategy") or "INITIAL")
    if retry_strategy != "VISION_CROP":
        if previous_calls:
            return (
                "STRUCTURED_EXTRACTION_RETRY_INVALID",
                "图片结构化抽取的第二次调用只能是局部视觉增强。",
            )
        return None
    if len(previous_calls) != 1:
        return (
            "STRUCTURED_EXTRACTION_RETRY_INVALID",
            "局部视觉增强只能发生在一次初始结构化抽取之后。",
        )
    observation_results = list((state.get("observation") or {}).get("results") or [])
    structured = next(
        (
            item.get("structured_extraction")
            for item in reversed(observation_results)
            if item.get("tool_name") == tool_name
            and isinstance(item.get("structured_extraction"), dict)
        ),
        None,
    )
    allowed_targets = set((structured or {}).get("low_confidence_field_keys") or [])
    targets = set(tool_input.get("target_field_keys") or [])
    if (
        not structured
        or structured.get("retryable") is not True
        or structured.get("recommended_retry_strategy") != "VISION_CROP"
        or not targets
        or not targets.issubset(allowed_targets)
    ):
        return (
            "STRUCTURED_EXTRACTION_RETRY_SCOPE_REJECTED",
            "局部增强字段不在后端确认的低置信度范围内。",
        )
    return None


def _dispatch_rejection_invocation(
    *,
    step: Dict[str, Any],
    tool_input: Dict[str, Any],
    code: str,
    message: str,
) -> ToolInvocationRecord:
    """记录 Dispatcher 在调用前拒绝的计划步骤，保留安全审计事实。"""

    return ToolInvocationRecord(
        tool_name=str(step.get("tool_name") or "unknown-tool"),
        input_json=tool_input,
        output_json={
            "kind": "agent_dispatch_rejection",
            "ok": False,
            "status": "FAILED",
            "error": {
                "code": code,
                "message": message,
                "retryable": False,
                "user_action_required": True,
            },
        },
        status="FAILED",
    )


def observe_tool_result(
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext],
) -> Dict[str, Any]:
    """生成脱敏 Tool 观察，并按后端策略决定是否让 Adaptive Planner 继续判断。"""

    if state.get("waiting_for_confirmation"):
        return {
            "observation": {
                "planning_round": int(state.get("planning_round", 0)),
                "tool_call_count": int(state.get("tool_call_count", 0)),
                "results": [],
            },
            "replan_requested": False,
            "status": "WAITING_FOR_CONFIRMATION",
        }

    last_results = state.get("last_dispatch_results", [])
    tool_name = str(state.get("last_dispatch_tool_name") or "")
    observation_items = [
        _safe_tool_observation(item, tool_name=tool_name)
        for item in last_results
    ]
    has_failed_result = any(
        item.get("ok") is False or str(item.get("status") or "").upper() == "FAILED"
        for item in last_results
    )
    has_waiting_result = any(
        item.get("kind") == "filesystem_job"
        or str(item.get("status") or "").upper()
        in {"PENDING", "PROCESSING", "WAITING_FOR_ASYNC_JOB"}
        or str(item.get("result_status") or "") == "INDEX_PENDING"
        for item in last_results
    )
    has_user_decision = any(
        isinstance(item.get("search_clarification"), dict)
        or isinstance(item.get("trash_restore_selection"), dict)
        for item in last_results
    )
    # 计划创建结果是确认边界：Planner 可以在此前编排读取/分类/计划 Tool，但一旦存在待确认计划，
    # 不得继续生成新的动作计划或尝试 confirmed-file-action。后端确认 API 才能启动执行链路。
    has_pending_confirmation = any(
        item.get("has_operation_plan") is True
        for item in observation_items
    )
    # 完整文件名的正文请求在首轮已经由后端固化为硬对象范围。证据回答完成后直接进入回执，
    # 避免模型在后续观察中把该单文件请求重新扩张为搜索或重复读取。
    has_completed_hard_filename_answer = (
        tool_name == "evidence-answer"
        and str(state.get("intent") or "") == "EVIDENCE_ANSWER"
        and has_explicit_filename_content_request(state.get("message") or "")
    )
    explicit_replan = any(item.get("replan_required") is True for item in last_results)
    observation_policy = _tool_observation_policy(
        registry=runtime.context.registry,
        tool_name=tool_name,
    )
    planner_after_execution = (
        observation_policy == "PLANNER_AFTER_EXECUTION"
        and state.get("adaptive_planner_mode") == "enabled"
    )
    failed_result_can_replan = (
        has_failed_result
        and planner_after_execution
        and not _tool_has_side_effects(
            registry=runtime.context.registry,
            tool_name=tool_name,
        )
    )
    if has_failed_result and not failed_result_can_replan:
        # 写入型 Tool 失败后不能让模型继续创建其他副作用；观察仍可供审计，但下一步只允许结束或澄清。
        for observation_item in observation_items:
            observation_item["available_next_decisions"] = ["FINISH", "CLARIFY"]
    can_replan = (
        (explicit_replan or planner_after_execution)
        and (not has_failed_result or failed_result_can_replan)
        and not has_waiting_result
        and not has_user_decision
        and not has_pending_confirmation
        and not has_completed_hard_filename_answer
        and int(state.get("planning_round", 0)) < MAX_PLANNING_ROUNDS
        and int(state.get("tool_call_count", 0)) < MAX_TOOL_CALLS
    )
    search_attempts = list(state.get("search_attempts", []))
    observed_document_ids = list(state.get("observed_document_ids", []))
    effective_conditions = list(state.get("effective_conditions", []))
    for observation_item in observation_items:
        if tool_name != "hybrid-search":
            continue
        search_attempts.append(
            {
                "planning_round": int(state.get("planning_round", 0)),
                "query": observation_item.get("query", ""),
                "result_count": observation_item.get("result_count", 0),
                "result_status": observation_item.get("result_status", ""),
                "index_status": observation_item.get("index_status", ""),
                "effective_conditions": observation_item.get(
                    "effective_conditions", []
                ),
            }
        )
        for document_id in observation_item.get("document_ids", []):
            if document_id not in observed_document_ids:
                observed_document_ids.append(document_id)
        effective_conditions = list(
            observation_item.get("effective_conditions", [])
        )
    return {
        "observation": {
            "planning_round": int(state.get("planning_round", 0)),
            "tool_call_count": int(state.get("tool_call_count", 0)),
            "remaining_planning_rounds": max(
                0,
                MAX_PLANNING_ROUNDS
                - int(state.get("planning_round", 0)),
            ),
            "remaining_tool_calls": max(
                0,
                MAX_TOOL_CALLS - int(state.get("tool_call_count", 0)),
            ),
            "results": observation_items,
        },
        "search_attempts": search_attempts[-MAX_PLANNING_ROUNDS:],
        "effective_conditions": effective_conditions,
        "observed_document_ids": observed_document_ids[:50],
        "replan_requested": can_replan,
        "status": "PLANNING" if can_replan else "SUMMARIZING",
    }


def _safe_tool_observation(
    result: Dict[str, Any],
    *,
    tool_name: str,
) -> Dict[str, Any]:
    """只保留重规划必需的 Tool 状态，避免正文和内部路径进入 LLM 输入。"""

    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    status = str(result.get("status") or "")
    nested_extractions = [
        item for item in list(result.get("extraction_results") or []) if isinstance(item, dict)
    ]
    document_ids = _observation_document_ids(result, nested_extractions)
    operation_plan_present = bool(result.get("operation_plan_id"))
    waiting_for_async_job = (
        result.get("kind") == "filesystem_job"
        or status.upper() in {"PENDING", "PROCESSING", "WAITING_FOR_ASYNC_JOB"}
        or str(result.get("result_status") or "") == "INDEX_PENDING"
    )
    requires_user_confirmation = operation_plan_present or status.upper() in {
        "WAITING_FOR_CONFIRMATION",
        "WAITING_SELECTION",
    }
    base = {
        "tool_name": tool_name,
        "observation_kind": str(result.get("kind") or "generic"),
        "status": status,
        "ok": result.get("ok"),
        "error_code": str(error.get("code") or ""),
        "replan_required": result.get("replan_required") is True,
        "document_ids": document_ids,
        "result_count": _observation_result_count(result, nested_extractions),
        "completed_count": _safe_nonnegative_int(result.get("completed_count")),
        "failed_count": _safe_nonnegative_int(result.get("failed_count")),
        "evidence_count": _observation_evidence_count(result),
        "classification_count": _observation_classification_count(result),
        "has_operation_plan": operation_plan_present,
        "requires_user_confirmation": requires_user_confirmation,
        "waiting_for_async_job": waiting_for_async_job,
    }
    if tool_name == "extract-image-structured-data" and not waiting_for_async_job:
        quality_band = str(result.get("quality_band") or "LOW")
        retry_strategy = str(result.get("recommended_retry_strategy") or "NONE")
        if retry_strategy not in {"NONE", "REOCR", "VISION_CROP"}:
            retry_strategy = "NONE"
        structured = {
            "record_count": _safe_nonnegative_int(result.get("record_count")) or 0,
            "field_count": _safe_nonnegative_int(result.get("field_count")) or 0,
            "review_count": _safe_nonnegative_int(result.get("review_count")) or 0,
            "missing_required_field_count": _safe_nonnegative_int(
                result.get("missing_required_field_count")
            )
            or 0,
            "quality_band": quality_band if quality_band in {"HIGH", "MEDIUM", "LOW"} else "LOW",
            "retryable": result.get("retryable") is True,
            "recommended_retry_strategy": retry_strategy,
            "low_confidence_field_keys": [
                str(value)
                for value in list(result.get("low_confidence_field_keys") or [])
                if str(value)
            ][:20],
        }
        base["structured_extraction"] = structured
        base["available_next_decisions"] = (
            ["FINISH"]
            if structured["quality_band"] == "HIGH" and structured["review_count"] == 0
            else ["TOOL_PLAN", "CLARIFY", "FINISH"]
            if structured["retryable"]
            else ["CLARIFY", "FINISH"]
        )
        return ExecutionObservation.model_validate(base).model_dump()
    if tool_name != "hybrid-search":
        base["available_next_decisions"] = (
            ["FINISH", "CLARIFY"]
            if requires_user_confirmation or waiting_for_async_job
            else ["TOOL_PLAN", "CLARIFY", "FINISH"]
        )
        return ExecutionObservation.model_validate(base).model_dump()
    document_ids = [
        str(value)
        for value in list(result.get("document_ids") or [])
        if str(value)
    ][:50]
    conditions = [
        {
            "label": str(item.get("label") or "")[:40],
            "value": str(item.get("value") or "")[:300],
            "condition_type": str(
                item.get("condition_type") or "semantic"
            ),
            "status": str(item.get("status") or "UNSUPPORTED"),
            "source": str(item.get("source") or "backend"),
        }
        for item in list(result.get("effective_conditions") or [])
        if isinstance(item, dict)
    ][:30]
    return ExecutionObservation.model_validate({
        **base,
        "query": str(result.get("query") or "")[:500],
        "result_count": int(
            result.get("total_returned")
            if result.get("total_returned") is not None
            else len(document_ids)
        ),
        "document_ids": document_ids,
        "effective_conditions": conditions,
        "result_status": str(result.get("result_status") or ""),
        "index_status": str(result.get("index_status") or ""),
        "partial": bool(result.get("partial", False)),
        "available_next_decisions": (
            ["FINISH", "CLARIFY"]
            if waiting_for_async_job
            else ["TOOL_PLAN", "CLARIFY", "FINISH"]
        ),
    }).model_dump()


def _observation_document_ids(
    result: Dict[str, Any], nested_extractions: List[Dict[str, Any]],
) -> List[str]:
    """只从后端 Tool 已返回的受控 document_id 投影下一轮可用范围。"""

    values = [*list(result.get("document_ids") or [])]
    if result.get("document_id"):
        values.append(result["document_id"])
    values.extend(item.get("document_id") for item in nested_extractions)
    return list(dict.fromkeys(str(value) for value in values if str(value)))[:50]


def _safe_nonnegative_int(value: Any) -> int | None:
    """将 Tool 计数安全投影为观察字段，非法值不交给 Planner。"""

    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _observation_result_count(result: Dict[str, Any], nested_extractions: List[Dict[str, Any]]) -> int | None:
    """抽取已验证结果的数量，不让模型从正文或路径自行推断数量。"""

    explicit = result.get("total_returned", result.get("matched_count"))
    parsed = _safe_nonnegative_int(explicit)
    if parsed is not None:
        return parsed
    for key in ("files", "documents", "results", "suggestions"):
        if isinstance(result.get(key), list):
            return len(result[key])
    return len(nested_extractions) if nested_extractions else None


def _observation_evidence_count(result: Dict[str, Any]) -> int | None:
    """只传递证据条目数量，不把原文摘录放入规划输入。"""

    if isinstance(result.get("references"), list):
        return len(result["references"])
    return _safe_nonnegative_int(result.get("evidence_count"))


def _observation_classification_count(result: Dict[str, Any]) -> int | None:
    """汇总已有分类建议数量，不向 Planner 暴露分类证据全文。"""

    documents = result.get("documents")
    if isinstance(documents, list):
        return sum(
            len(item.get("categories") or [])
            for item in documents
            if isinstance(item, dict)
        )
    return _safe_nonnegative_int(result.get("classification_count"))


def _tool_observation_policy(*, registry: Any, tool_name: str) -> str:
    """从后端 Registry 读取观察策略；测试 fake 或未知 Tool 使用兼容信号模式。"""

    if not tool_name or not hasattr(registry, "get"):
        return "PLANNER_ON_SIGNAL"
    try:
        return str(registry.get(tool_name).observation_policy)
    except Exception:
        return "PLANNER_ON_SIGNAL"


def _tool_has_side_effects(*, registry: Any, tool_name: str) -> bool:
    """读取 Registry 的副作用声明；未知 Tool 按有副作用处理，保持关闭式安全。"""

    if not tool_name or not hasattr(registry, "get"):
        return True
    try:
        return bool(registry.get(tool_name).side_effects)
    except Exception:
        return True


def route_after_observation(state: AgentGraphState) -> str:
    """优先重规划，否则继续执行当前计划的下一步骤。"""

    if state.get("waiting_for_confirmation"):
        return "async_job_wait"
    if state.get("replan_requested"):
        return "planning"
    if int(state.get("current_step_index", 0)) < len(
        state.get("tool_plan", {}).get("steps", [])
    ):
        return "tool_dispatch"
    return "async_job_wait"


def _failed_tool_invocation(*, step: Dict[str, Any], error: Exception) -> ToolInvocationRecord:
    """把允许降级的单个 Tool 异常转成结构化失败记录。"""

    tool_name = str(step.get("tool_name") or "unknown-tool")
    tool_input = step.get("input", {})
    document_id = str(tool_input.get("document_id") or "")
    error_payload = {
        "code": "TOOL_EXECUTION_FAILED",
        "message": str(error),
        "retryable": False,
        "user_action_required": False,
    }

    if tool_name == "extract-image-structured-data":
        output_json: Dict[str, Any] = {
            "kind": "structured_image_extraction",
            "ok": False,
            "status": "FAILED",
            "document_id": document_id,
            "record_count": 0,
            "field_count": 0,
            "review_count": 0,
            "missing_required_field_count": 0,
            "retryable": False,
            "recommended_retry_strategy": "NONE",
            "low_confidence_field_keys": [],
            "field_schema": [],
            "records": [],
            "review_items": [],
            "original_unchanged": True,
            "error": error_payload,
        }
    elif tool_name == "analyze-spreadsheet":
        output_json: Dict[str, Any] = {
            "kind": "spreadsheet_analysis",
            "ok": False,
            "status": "FAILED",
            "document_id": document_id,
            "error": error_payload,
        }
    else:
        output_json = {
            "ok": False,
            "document_id": document_id,
            "extraction_run_id": f"failed-{step.get('step_id', 'unknown')}",
            "status": "FAILED",
            "extractor": tool_name,
            "pages": [],
            "error": error_payload,
        }

    return ToolInvocationRecord(
        tool_name=tool_name,
        input_json=tool_input,
        output_json=output_json,
        status="FAILED",
    )

def async_job_wait(state: AgentGraphState) -> Dict[str, Any]:
    """保留异步任务与确认暂停边界，不在同步请求中阻塞等待。"""

    if state.get("waiting_for_confirmation"):
        return {"status": "WAITING_FOR_CONFIRMATION"}
    return {"status": "SUMMARIZING"}


def evidence_or_change(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """聚合 Tool 结果、evidence、ChangeSet 和 OperationPlan，供 response 节点消费。"""

    result_summary = _aggregate_tool_results(
        state=state,
        tool_results=state.get("tool_results", []),
        context_documents=state.get("context_documents", []),
        classification_service=runtime.context.classification_service,
    )
    filesystem_job = result_summary.get("filesystem_job", {})
    return {
        "changeset_id": state.get("changeset_id"),
        "operation_plan_id": state.get("operation_plan_id"),
        "result_summary": result_summary,
        "document_results": result_summary.get("document_results", []),
        "async_job_ids": [
            str(job_id)
            for job_id in (
                filesystem_job.get("job_ids")
                or (
                    [filesystem_job.get("job_id")]
                    if filesystem_job.get("job_id")
                    else []
                )
            )
            if job_id
        ],
    }


def _aggregate_tool_results(
    *,
    state: AgentGraphState,
    tool_results: List[Dict[str, Any]],
    context_documents: List[Dict[str, Any]],
    classification_service,
) -> Dict[str, Any]:
    """把所有 Tool 输出聚合为 response 可直接消费的通用结果结构。"""

    requested_document_ids = {
        str(document_id)
        for document_id in state.get("slots", {}).get("document_ids", [])
        if str(document_id)
    }
    extraction_results = _extraction_results_from_results(
        tool_results,
        allowed_document_ids=requested_document_ids or None,
    )
    insight_documents = _insight_documents_from_results(tool_results)
    classification_documents = _classification_documents_from_results(tool_results)
    return {
        "evidence_answer": _evidence_answer_from_results(tool_results),
        "spreadsheet_workbench_results": _spreadsheet_workbench_results_from_results(tool_results),
        "spreadsheet_analysis_results": _spreadsheet_analysis_results_from_results(tool_results),
        "document_results": build_document_results_from_extraction_results(
            extraction_results=extraction_results,
            context_documents=context_documents,
            classification_service=classification_service,
            include_categories=_should_classify_documents(state),
        ),
        "extraction_results": extraction_results,
        "insight_documents": insight_documents,
        "classification_documents": classification_documents,
        "capability_catalog": _capability_catalog_from_results(tool_results),
        "classification_taxonomy": _classification_taxonomy_from_results(tool_results),
        "classification_decision": _classification_decision_from_results(tool_results),
        "original_file_metadata": _original_file_metadata_from_results(tool_results),
        "working_copy_operation_plan": _working_copy_operation_plan_from_results(
            tool_results
        ),
        "working_copy_operation": _working_copy_operation_from_results(tool_results),
        "managed_file_list": _managed_file_list_from_results(tool_results),
        "workspace_file_search": _workspace_file_search_from_results(tool_results),
        "mcp_filesystem_result": _mcp_filesystem_result_from_results(tool_results),
        "rename_plan": _rename_plan_from_results(tool_results),
        "rename_review_resolution": _rename_review_resolution_from_results(tool_results),
        "filename_conflict": _filename_conflict_from_results(tool_results),
        "intent_summary": _intent_summary_from_results(tool_results),
        "filesystem_job": _filesystem_job_from_results(tool_results),
        "tool_errors": _tool_errors_from_invocations(state.get("tool_invocations", [])),
    }


def _tool_errors_from_invocations(tool_invocations: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """提取未形成业务结果的 Tool 失败原因，避免 response 返回无意义的通用兜底。

    这里只保留面向用户的错误消息，不把 Tool 名称、输入参数或内部异常细节暴露到普通消息接口。
    正文解析失败由逐文件回执负责展示；这里处理受管目录读取等没有逐文件结果的失败。
    """

    errors: List[Dict[str, str]] = []
    for invocation in tool_invocations:
        if str(invocation.get("status") or "").upper() != "FAILED":
            continue
        result = invocation.get("output_json")
        if not isinstance(result, dict):
            continue
        if (
            invocation.get("tool_name") == "extract-document-text"
            and result.get("kind") != "agent_dispatch_rejection"
        ):
            continue
        error = result.get("error")
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "").strip()
        if not message:
            continue
        errors.append(
            {
                "code": str(error.get("code") or "TOOL_EXECUTION_FAILED"),
                "message": message,
            }
        )
    return errors


def _filesystem_job_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """提取受管目录异步任务回执。"""

    for result in reversed(tool_results):
        if (
            result.get("kind") == "filesystem_job"
            and (result.get("job_id") or result.get("job_ids"))
        ):
            return result
    return {}


def _should_classify_documents(state: AgentGraphState) -> bool:
    """判断本次文件读取是否需要执行和展示分类建议。"""

    requested_outputs = set(state.get("slots", {}).get("requested_outputs", []))
    intent = str(state.get("intent") or "").upper()
    return "classification" in requested_outputs or "CLASSIFY" in intent


def _spreadsheet_analysis_results_from_results(
    tool_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """提取所有表格分析结果，支持多附件逐个展示。"""

    return [
        result
        for result in tool_results
        if result.get("kind") == "spreadsheet_analysis"
    ]


def _evidence_answer_from_results(
    tool_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取阶段五回答、文件选择或回收站恢复卡的安全 Tool 结果。"""

    for result in reversed(tool_results):
        if result.get("kind") in {
            "evidence_answer",
            "file_selection",
            "trash_restore_selection",
        }:
            return result
    return {}


def _spreadsheet_workbench_results_from_results(
    tool_results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """提取表格 Profile 和校验 Tool 结果。"""

    return [
        result
        for result in tool_results
        if result.get("kind") in {"spreadsheet_profile", "spreadsheet_validation"}
    ]


def response(state: AgentGraphState, runtime: Runtime[AgentRuntimeContext]) -> Dict[str, Any]:
    """生成最终回执，并可附加不承载事实的 LLM 自然语言说明。

    业务事实先由确定性分支生成；LLM 只能为同一份已验证结果补充任务语气，不能改写数值、路径、证据
    定位或 OperationPlan。这样不改变现有前端回答卡片的数据结构和视觉样式。
    """

    deterministic_result = _deterministic_response(state, runtime)
    # 证据回答本身已经由 EvidenceAnswerService 使用原文证据和引用约束生成；再由通用回执模型改写会
    # 损失“回答—引用”对应关系，因此它是统一回执节点的受控例外。
    if state.get("result_summary", {}).get("evidence_answer"):
        return deterministic_result
    final_response = deterministic_result.get("final_response")
    if not final_response or runtime is None:
        return deterministic_result
    summary_service = getattr(runtime.context, "receipt_summary_service", None)
    if summary_service is None:
        return deterministic_result
    safe_input = _build_receipt_summary_input(
        state=state,
        result_summary=state.get("result_summary", {}),
        status=str(deterministic_result.get("status") or "COMPLETED"),
    )
    try:
        natural_summary = summary_service.summarize_receipt(
            verified_summary=safe_input
        )
    except Exception as exc:
        # 回执表述层不是事实生成器；任何异常都必须保留确定性结果，不能使成功任务失败。
        log_event(
            "agent.receipt_summary.failed",
            level="WARNING",
            status="DEGRADED",
            error_code=exc.__class__.__name__,
            message="最终回执 LLM 表述不可用，已保留确定性回执",
        )
        return deterministic_result
    if not natural_summary:
        return deterministic_result
    log_event(
        "agent.receipt_summary.completed",
        status="COMPLETED",
        message="最终回执已使用验证摘要生成自然语言说明",
    )
    return {
        **deterministic_result,
        "final_response": f"{natural_summary}\n\n{final_response}",
    }


def _build_receipt_summary_input(
    *,
    state: AgentGraphState,
    result_summary: Dict[str, Any],
    status: str,
) -> Dict[str, Any]:
    """构造最终 LLM 可读取的脱敏事实摘要。

    这里只允许结果类型、是否有证据/待确认项和已由后端汇总的文件状态数量。具体文件名、路径、数值、
    quote、页码和 Tool 原始输出继续只由确定性回执及前端卡片展示。
    """

    document_results = [
        item
        for item in list(result_summary.get("document_results") or [])
        if isinstance(item, dict)
    ]
    evidence_answer = result_summary.get("evidence_answer")
    return {
        "intent": str(state.get("intent") or ""),
        "status": status,
        "has_document_results": bool(document_results),
        "has_evidence_answer": isinstance(evidence_answer, dict),
        "has_classification_result": bool(result_summary.get("classification_documents")),
        "has_operation_plan": bool(
            state.get("operation_plan_id")
            or result_summary.get("rename_plan")
            or result_summary.get("working_copy_operation")
        ),
        "requires_confirmation": status == "WAITING_FOR_CONFIRMATION" or bool(
            result_summary.get("rename_plan")
        ),
        "has_async_work": bool(result_summary.get("filesystem_job")),
        "has_errors": bool(result_summary.get("tool_errors")),
    }


def _deterministic_response(
    state: AgentGraphState,
    runtime: Runtime[AgentRuntimeContext] | None,
) -> Dict[str, Any]:
    """生成面向用户的最终运行摘要。"""

    if state.get("waiting_for_confirmation"):
        return {
            "status": "WAITING_FOR_CONFIRMATION",
            "final_response": (
                "该操作需要先确认操作计划，当前尚未执行。"
                if state.get("operation_plan_id")
                else "当前高风险操作尚未生成可确认的操作计划，请先生成计划后再确认执行。"
            ),
        }

    result_summary = state.get("result_summary", {})
    dispatch_rejections = [
        item
        for item in result_summary.get("tool_errors", [])
        if item.get("code") in {"DUPLICATE_TOOL_CALL", "TOOL_CALL_BUDGET_EXCEEDED"}
    ]
    if dispatch_rejections:
        # 调度限制属于安全边界，必须向用户明确说明；已经完成的文件仍保留逐文件回执。
        document_results = result_summary.get("document_results", [])
        completed_receipt = (
            _build_document_results_response(document_results)
            if document_results
            else ""
        )
        rejection_messages = "\n".join(
            dict.fromkeys(
                str(item.get("message") or "").strip()
                for item in dispatch_rejections
                if str(item.get("message") or "").strip()
            )
        )
        return {
            "status": "NEEDS_REVIEW",
            "final_response": "\n\n".join(
                item for item in [completed_receipt, rejection_messages] if item
            ),
        }

    classification_decision = result_summary.get("classification_decision", {})
    if classification_decision:
        waiting = (
            classification_decision.get("kind") == "classification_clarification"
            or classification_decision.get("status") == "WAITING_SELECTION"
        )
        return {
            "status": "NEEDS_REVIEW" if waiting else (
                "COMPLETED" if classification_decision.get("ok") else "FAILED"
            ),
            "final_response": str(
                classification_decision.get("message")
                or (
                    "请选择要确认或纠正的具体文件分类。"
                    if waiting
                    else "分类决定已保存，文件位置未改变。"
                )
            ),
        }

    working_copy_operation_plan = result_summary.get(
        "working_copy_operation_plan", {}
    )
    if working_copy_operation_plan:
        return {
            "status": "WAITING_FOR_CONFIRMATION",
            "final_response": str(
                working_copy_operation_plan.get("message")
                or "文件操作计划已生成，请核对后确认执行。"
            ),
        }

    working_copy_operation = result_summary.get("working_copy_operation", {})
    if working_copy_operation:
        return {
            "status": (
                "COMPLETED"
                if working_copy_operation.get("status") == "EXECUTED"
                else "NEEDS_REVIEW"
            ),
            "final_response": str(
                working_copy_operation.get("message")
                or "共享工作副本操作已完成。"
            ),
        }

    evidence_answer = result_summary.get("evidence_answer", {})
    if evidence_answer:
        status = str(evidence_answer.get("status") or "")
        return {
            "status": (
                "NEEDS_REVIEW"
                if status in {"NEEDS_CLARIFICATION", "NEEDS_CONFIRMATION", "NO_EVIDENCE", "PARTIAL"}
                else "COMPLETED"
                if evidence_answer.get("ok")
                else "FAILED"
            ),
            "final_response": str(
                evidence_answer.get("answer")
                or evidence_answer.get("message")
                or "当前没有可用于回答的原文证据。"
            ),
        }

    workbench_results = result_summary.get("spreadsheet_workbench_results", [])
    if workbench_results:
        return {
            "status": "COMPLETED",
            "final_response": format_spreadsheet_workbench_response(workbench_results),
        }

    analysis_results = result_summary.get("spreadsheet_analysis_results", [])
    if analysis_results:
        return {
            "status": "COMPLETED",
            "final_response": format_spreadsheet_analysis_response(analysis_results),
        }

    rename_plan = result_summary.get("rename_plan", {})
    if rename_plan:
        return {
            "status": "COMPLETED" if rename_plan.get("ok") else "NEEDS_REVIEW",
            "final_response": _build_rename_plan_response(rename_plan),
        }

    rename_review_resolution = result_summary.get("rename_review_resolution", {})
    if rename_review_resolution:
        return {
            "status": (
                "COMPLETED"
                if rename_review_resolution.get("status") in {"EXECUTED", "COMPLETED"}
                else "NEEDS_REVIEW"
            ),
            "final_response": _build_rename_review_resolution_response(rename_review_resolution),
        }

    filename_conflict = result_summary.get("filename_conflict", {})
    if filename_conflict:
        return {
            "status": "NEEDS_REVIEW",
            "final_response": str(
                filename_conflict.get("message")
                or "共享工作目录中已存在同名文件，请选择处理方式。"
            ),
        }

    filesystem_job = result_summary.get("filesystem_job", {})
    if filesystem_job:
        return {
            "status": "WAITING_FOR_ASYNC_JOB",
            # 普通对话只展示前端统一的 processing 反馈；任务编号、队列类型和
            # “待准备”状态仅保留在审计投影中，不能成为聊天气泡正文。
            "final_response": None,
        }

    document_results = result_summary.get("document_results", [])
    if document_results:
        requested_outputs = set(state.get("slots", {}).get("requested_outputs", []))
        is_summary_intent = "SUMMAR" in str(state.get("intent") or "").upper()
        is_answer_intent = "ANSWER" in str(state.get("intent") or "").upper()
        if "summary" in requested_outputs or "answer" in requested_outputs or is_summary_intent or is_answer_intent:
            llm_summary = runtime.context.document_summary_service.summarize_documents(
                document_results=document_results,
                tool_results=result_summary.get("extraction_results", []),
                user_message=state.get("message", ""),
            )
            return {
                "status": "COMPLETED",
                "final_response": llm_summary
                or _build_document_summary_response(
                    document_results=document_results,
                    extraction_results=result_summary.get("extraction_results", []),
                ),
            }
        return {
            "status": "COMPLETED",
            "final_response": _build_document_results_response(document_results),
        }

    extraction_results = result_summary.get("extraction_results", [])
    if extraction_results:
        return {
            "status": "COMPLETED",
            "final_response": _build_extraction_response(extraction_results),
        }

    original_file_metadata = result_summary.get("original_file_metadata", {})
    if original_file_metadata:
        filename = str(original_file_metadata.get("filename") or "当前文件")
        size_bytes = int(original_file_metadata.get("size_bytes") or 0)
        availability = (
            "原始文件可用"
            if original_file_metadata.get("exists") is True
            else "原始文件当前不可用"
        )
        return {
            "status": "COMPLETED",
            "final_response": (
                f"已读取文件信息：{filename}，大小 {size_bytes} 字节，{availability}。"
            ),
        }

    insight_documents = result_summary.get("insight_documents", [])
    classification_documents = result_summary.get("classification_documents", [])
    if classification_documents:
        return {
            "status": "COMPLETED",
            "final_response": _build_classification_summary_response(classification_documents),
        }

    if insight_documents:
        filenames = [
            item.get("filename") or item.get("document_id")
            for item in insight_documents
        ]
        return {
            "status": "COMPLETED",
            "final_response": f"已读取 {len(insight_documents)} 个文件的基础洞察：{', '.join(filenames)}。",
        }

    capability_catalog = result_summary.get("capability_catalog", {})
    if capability_catalog:
        return {
            "status": "COMPLETED",
            "final_response": _build_capability_help_response(capability_catalog),
        }

    taxonomy_catalog = result_summary.get("classification_taxonomy", {})
    if taxonomy_catalog:
        return {
            "status": "COMPLETED",
            "final_response": _build_classification_taxonomy_response(taxonomy_catalog),
        }

    managed_file_list = result_summary.get("managed_file_list", {})
    if managed_file_list:
        return {
            "status": "COMPLETED",
            "final_response": _build_managed_file_list_response(managed_file_list),
        }

    workspace_file_search = result_summary.get("workspace_file_search", {})
    if workspace_file_search:
        if workspace_file_search.get("search_clarification"):
            return {
                "status": "NEEDS_REVIEW",
                "final_response": _build_workspace_file_search_response(
                    workspace_file_search
                ),
            }
        return {
            "status": "COMPLETED",
            "final_response": _build_workspace_file_search_response(workspace_file_search),
        }

    mcp_filesystem_result = result_summary.get("mcp_filesystem_result", {})
    if mcp_filesystem_result:
        return {
            "status": "COMPLETED",
            "final_response": _build_mcp_filesystem_response(mcp_filesystem_result),
        }

    intent_summary = result_summary.get("intent_summary", {})
    if intent_summary:
        return {
            "status": (
                "NEEDS_REVIEW"
                if intent_summary.get("intent") == "MISSING_FILE_SCOPE"
                else "COMPLETED"
            ),
            "final_response": _build_general_chat_response(intent_summary),
        }

    tool_errors = result_summary.get("tool_errors", [])
    if tool_errors:
        messages = list(
            dict.fromkeys(
                str(item.get("message") or "").strip()
                for item in tool_errors
                if str(item.get("message") or "").strip()
            )
        )
        return {
            "status": "NEEDS_REVIEW",
            "final_response": "\n".join(messages),
        }

    return {
        "status": "COMPLETED",
        "final_response": "本次任务已执行完成，但暂未生成可展示的业务结果。请补充要读取、汇总或处理的文件范围。",
    }


def _extraction_results_from_results(
    tool_results: List[Dict[str, Any]],
    *,
    allowed_document_ids: set[str] | None = None,
) -> List[Dict[str, Any]]:
    """提取面向用户文件范围的解析结果，并按 document_id 去重。

    重命名 Tool 会在内部解析对应工作副本以生成名称建议，这些解析记录属于审计事实，
    不能再次投影成用户上传的额外文件卡。只要 Planner 已固化附件范围，就仅返回该范围；
    同一文档被多个 Tool 复用时也只展示一次。
    """

    extraction_results: List[Dict[str, Any]] = []
    seen_document_ids: set[str] = set()

    def append_visible(item: Any) -> None:
        """追加一个有效且属于用户请求范围的解析结果。"""

        if not isinstance(item, dict):
            return
        if not item.get("extraction_run_id") or item.get("status") not in {"COMPLETED", "FAILED"}:
            return
        document_id = str(item.get("document_id") or "")
        if allowed_document_ids is not None and document_id not in allowed_document_ids:
            return
        if document_id and document_id in seen_document_ids:
            return
        extraction_results.append(item)
        if document_id:
            seen_document_ids.add(document_id)

    for result in tool_results:
        batch_results = result.get("extraction_results")
        if isinstance(batch_results, list):
            for item in batch_results:
                append_visible(item)
            continue
        append_visible(result)
    return extraction_results


def _build_extraction_response(extraction_results: List[Dict[str, Any]]) -> str:
    """生成文件解析 Tool 的用户回执。"""

    failed_messages = [
        (result.get("error") or {}).get("message") or "未知错误"
        for result in extraction_results
        if result.get("status") == "FAILED"
    ]
    completed_results = [result for result in extraction_results if result.get("status") == "COMPLETED"]
    if not completed_results and failed_messages:
        return f"文件解析失败：{failed_messages[0]}。"

    page_count = sum(len(result.get("pages", [])) for result in completed_results)
    char_count = sum(
        int(page.get("char_count", 0))
        for result in completed_results
        for page in result.get("pages", [])
    )
    response_text = f"已解析 {len(completed_results)} 个文件，提取 {page_count} 页/Sheet，共 {char_count} 个字符。"
    if failed_messages:
        response_text += f" 另有 {len(failed_messages)} 个文件解析失败：{failed_messages[0]}。"
    return response_text


def _insight_documents_from_results(tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 Tool 结果中提取 read-document-insights 返回的文件列表。"""

    documents: List[Dict[str, Any]] = []
    for result in tool_results:
        result_documents = result.get("documents")
        if isinstance(result_documents, list):
            documents.extend([item for item in result_documents if isinstance(item, dict)])
    return documents


def _classification_documents_from_results(tool_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从 Tool 结果中提取历史分类建议文件列表。"""

    for result in tool_results:
        result_documents = result.get("documents")
        if result.get("ok") and isinstance(result_documents, list):
            if any(isinstance(item, dict) and "categories" in item for item in result_documents):
                return [item for item in result_documents if isinstance(item, dict)]
    return []


def _build_classification_summary_response(documents: List[Dict[str, Any]]) -> str:
    """把当前版本分类建议、置信度和可定位依据汇总为用户可读文本。"""

    blocks = [f"已汇总当前 {len(documents)} 个文件的分类建议及依据："]
    for index, document in enumerate(documents, start=1):
        filename = document.get("filename") or document.get("document_id") or "未知文件"
        categories = [item for item in document.get("categories", []) if isinstance(item, dict)]
        if not categories:
            blocks.append(
                f"{index}. {filename}\n"
                "暂无当前版本的分类证据，不能说明该文件为什么属于某个类别。"
            )
            continue
        category_lines = []
        for category in categories[:5]:
            confidence = float(category.get("confidence") or 0)
            path = [
                str(value)
                for value in list(category.get("category_path") or [])
                if str(value)
            ]
            label = " / ".join(path) or str(category.get("name") or "其他")
            category_lines.append(f"- {label}\n  置信度：{confidence:.2f}")
            evidence_items = [
                item
                for item in list(category.get("evidence_items") or [])
                if isinstance(item, dict)
            ]
            evidence_line = _classification_evidence_line(evidence_items)
            category_lines.append(
                f"  依据：{evidence_line}"
                if evidence_line
                else "  依据：没有找到可定位的原文引用，该建议需要人工复核。"
            )
        blocks.append(f"{index}. {filename}\n" + "\n".join(category_lines))
    return "\n\n".join(blocks)


def _classification_evidence_line(evidence_items: List[Dict[str, Any]]) -> str:
    """格式化首条分类原文依据，不向普通用户暴露分类器内部信号。"""

    for item in evidence_items:
        quote = " ".join(str(item.get("quote") or "").split()).strip()
        if not quote:
            continue
        location = ""
        if item.get("sheet_name"):
            location = f"工作表“{item['sheet_name']}”"
            if item.get("cell_range"):
                location += f" {item['cell_range']}"
        elif item.get("page_number"):
            location = f"第 {item['page_number']} 页"
        clipped_quote = quote[:180] + ("…" if len(quote) > 180 else "")
        return f"{location + '：' if location else ''}“{clipped_quote}”"
    return ""


def _capability_catalog_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取固定能力清单。"""

    for result in tool_results:
        if result.get("ok") and isinstance(result.get("capabilities"), list):
            return result
    return {}


def _build_capability_help_response(catalog: Dict[str, Any]) -> str:
    """把固定能力清单格式化成用户可读回答。"""

    capabilities = [
        item
        for item in catalog.get("capabilities", [])
        if isinstance(item, dict)
    ]
    if not capabilities:
        return "我可以围绕文件上传、读取、总结、分类和高风险操作计划提供帮助。"
    lines = ["我可以帮你完成这些文件工作："]
    for index, capability in enumerate(capabilities, start=1):
        name = capability.get("name") or capability.get("id") or "未命名能力"
        description = capability.get("description") or ""
        lines.append(f"{index}. {name}：{description}")
    examples = [
        example
        for capability in capabilities
        for example in capability.get("examples", [])[:1]
        if isinstance(example, str)
    ][:3]
    if examples:
        lines.append("\n你可以直接这样说：")
        lines.extend(f"- {example}" for example in examples)
    return "\n".join(lines)


def _classification_taxonomy_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取系统固定分类目录。"""

    for result in tool_results:
        if result.get("ok") and isinstance(result.get("taxonomy"), dict):
            return result["taxonomy"]
    return {}


def _classification_decision_from_results(
    tool_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取正式分类决定或分类选择卡结果。"""

    for result in tool_results:
        if result.get("kind") in {
            "classification_decision",
            "classification_clarification",
        }:
            return result
    return {}


def _original_file_metadata_from_results(
    tool_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取已通过权限校验的原始文件元信息，不投影存储路径或内容哈希。"""

    for result in reversed(tool_results):
        if result.get("kind") != "original_file_metadata" or not result.get("ok"):
            continue
        return {
            "filename": result.get("filename"),
            "content_type": result.get("content_type"),
            "size_bytes": result.get("size_bytes"),
            "exists": result.get("exists"),
        }
    return {}


def _working_copy_operation_plan_from_results(
    tool_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取尚未执行的工作副本 OperationPlan，供回执进入等待确认状态。"""

    for result in reversed(tool_results):
        if (
            result.get("kind") == "working_copy_operation_plan"
            and result.get("operation_plan_id")
        ):
            return result
    return {}


def _working_copy_operation_from_results(
    tool_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取已经由当前用户回复直接确认并执行的工作副本结果。"""

    for result in tool_results:
        if result.get("kind") in {
            "working_copy_operation_result",
            "working_copy_conflict_cancelled",
        }:
            return result
    return {}


def _managed_file_list_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取受管目录文件列表载荷。

    这里返回完整载荷而不是仅返回 files，是为了让空目录也能生成明确回复，
    避免 files=[] 被误判为没有业务结果。
    """

    for result in tool_results:
        result_files = result.get("files")
        if result.get("ok") and isinstance(result_files, list):
            return {
                "query": result.get("query") if isinstance(result.get("query"), dict) else {},
                "files": [item for item in result_files if isinstance(item, dict)],
            }
    return {}


def _workspace_file_search_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """提取工作副本摘要优先检索载荷，空结果也必须生成明确回复。"""

    # 自适应 Planner 可能执行多轮检索；最终回复必须采用最新一轮观察，
    # 不能因为第一轮零结果而掩盖后续放宽条件后的命中。
    for result in reversed(tool_results):
        if result.get("kind") != "workspace_file_search":
            continue
        return {
            "ok": bool(result.get("ok")),
            "query": str(result.get("query") or ""),
            "supported_count": result.get("supported_count"),
            "possible_count": result.get("possible_count"),
            "search_completeness": (
                result.get("search_completeness")
                if isinstance(result.get("search_completeness"), dict)
                else {}
            ),
            "results": [
                item for item in result.get("results", []) if isinstance(item, dict)
            ],
            "trash_restore_selection": (
                result.get("trash_restore_selection")
                if isinstance(result.get("trash_restore_selection"), dict)
                else {}
            ),
            "search_clarification": (
                result.get("search_clarification")
                if isinstance(result.get("search_clarification"), dict)
                else {}
            ),
            "error": result.get("error") if isinstance(result.get("error"), dict) else {},
        }
    return {}


def _mcp_filesystem_result_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取 Filesystem MCP 只读结果。"""

    for result in tool_results:
        if result.get("ok") and str(result.get("tool_name") or "").startswith("mcp-filesystem-"):
            return {
                "tool_name": result.get("tool_name"),
                "query": result.get("query") if isinstance(result.get("query"), dict) else {},
                "result": result.get("result"),
            }
    return {}


def _rename_plan_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """提取文件重命名建议与待确认计划。"""

    for result in tool_results:
        if result.get("kind") == "rename_plan":
            return result
    return {}


def _rename_review_resolution_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """提取用户更正名称后的逐文件执行结果。"""

    for result in tool_results:
        if result.get("kind") == "rename_review_resolution":
            return result
    return {}


def _filename_conflict_from_results(
    tool_results: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """提取工作副本全局文件名冲突，供响应节点和普通回执共同使用。"""

    for result in tool_results:
        if result.get("kind") == "filename_conflict":
            return result
    return {}


def _build_rename_plan_response(payload: Dict[str, Any]) -> str:
    """生成重命名建议回执；明确提示确认前原文件未修改。"""

    if not payload.get("ok"):
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        return str(error.get("message") or "暂时无法生成文件重命名建议。")
    matched_count = int(payload.get("matched_count") or 0)
    ready_count = int(payload.get("ready_count") or 0)
    review_count = int(payload.get("needs_review_count") or 0)
    lines = [f"已检查 {matched_count} 个文件，生成 {ready_count} 个可执行的重命名建议。"]
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    if query.get("path_prefix"):
        lines.insert(0, f"处理范围：{query['path_prefix']}")
    if review_count:
        lines.extend([
            f"另有 {review_count} 个文件待确认，不进入当前重命名计划，也不阻止其他文件执行。",
            "以下文件未能可靠识别正文标题，暂未处理。",
        ])
    else:
        lines.append("当前仅生成操作计划，原文件尚未修改。")
    if ready_count and payload.get("operation_plan_id"):
        lines.append("请核对下方计划，确认后才会执行重命名。")
    suggestions = [
        item for item in payload.get("suggestions", []) if isinstance(item, dict)
    ]
    for index, suggestion in enumerate(suggestions[:20], start=1):
        original_name = str(suggestion.get("filename") or "未知文件")
        proposed_name = suggestion.get("proposed_filename")
        if proposed_name:
            lines.append(f"{index}. {original_name} -> {proposed_name}")
            continue
        lines.append(f"{index}. {original_name}")
    if review_count:
        first_pending = next(
            (
                item
                for item in suggestions
                if item.get("status") == "NEEDS_REVIEW"
            ),
            {},
        )
        pending_filename = str(first_pending.get("filename") or "当前文件")
        pending_suffix = Path(pending_filename).suffix
        lines.extend(
            [
                (
                    "如需改名，请把尖括号内容替换为实际名称后回复："
                    f"文件“{pending_filename}”更正为“<请填写实际名称>{pending_suffix}”"
                ),
                "请勿原样发送“原文件名/新文件名”等占位文字；不需要改名可回复“不需要”。",
            ]
        )
    return "\n".join(lines)


def _build_rename_review_resolution_response(payload: Dict[str, Any]) -> str:
    """生成用户更正后的成功、失败和重名候选回执。"""

    if payload.get("dismissed_count"):
        lines = [f"已跳过 {int(payload.get('dismissed_count') or 0)} 个待复核文件。"]
        lines.append("其他已经生成名称的文件仍按原计划等待用户勾选确认。")
        return "\n".join(lines)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    lines: List[str] = []
    completed = [item for item in payload.get("completed_items", []) if isinstance(item, dict)]
    if completed:
        lines.append(f"已完成 {len(completed)} 个文件重命名：")
        for index, item in enumerate(completed, start=1):
            lines.append(
                f"{index}. {item.get('before_relative_path') or '未知文件'} -> "
                f"{item.get('after_relative_path') or '未知文件'}"
            )
    ambiguous = [item for item in payload.get("ambiguous_items", []) if isinstance(item, dict)]
    for item in ambiguous:
        source = str(item.get("source") or "该文件名")
        candidates = [candidate for candidate in item.get("candidates", []) if isinstance(candidate, dict)]
        lines.append(f"“{source}”匹配到多个待复核文件，请使用完整相对路径确认：")
        for index, candidate in enumerate(candidates, start=1):
            lines.append(f"{index}. {candidate.get('relative_path') or candidate.get('filename') or '未知文件'}")
        if candidates:
            example = candidates[0].get("relative_path") or candidates[0].get("filename")
            lines.append(f"例如：文件{example}更正为新文件名")
    failed = [item for item in payload.get("failed_items", []) if isinstance(item, dict)]
    if failed:
        lines.append(f"另有 {len(failed)} 个文件未完成：")
        for item in failed:
            source = item.get("before_relative_path") or item.get("source") or "未知文件"
            error_code = str(item.get("error_code") or "")
            if error_code in {"TARGET_ALREADY_EXISTS", "TARGET_ALREADY_INDEXED", "DUPLICATE_TARGET"}:
                message = "目标文件名重复，请确认并提供其他名称"
            else:
                message = item.get("error_message") or error_code or "处理失败"
            lines.append(f"- {source}：{message}")
    accepted_count = int(payload.get("accepted_count") or 0)
    remaining_count = int(payload.get("remaining_review_count") or 0)
    if accepted_count and not completed:
        lines.append(f"已记录 {accepted_count} 个文件的新名称，仍有 {remaining_count} 个文件待复核。")
    if not lines:
        lines.append(str(error.get("message") or "没有找到可处理的待复核文件。"))
    return "\n".join(lines)


def _build_mcp_filesystem_response(payload: Dict[str, Any]) -> str:
    """把 MCP 文件系统结果格式化为用户可读文本。"""

    tool_name = str(payload.get("tool_name") or "mcp-filesystem")
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    path_prefix = str(query.get("path_prefix") or query.get("path") or "服务器工作目录")
    result = payload.get("result")
    if result in (None, "", [], {}):
        return f"{path_prefix} 下暂未找到可展示的文件系统结果。"
    return "\n".join(
        [
            f"{path_prefix} 的实时文件系统结果：",
            _format_mcp_filesystem_result(result),
            f"来源工具：{tool_name}",
        ]
    )


def _format_mcp_filesystem_result(result: Any) -> str:
    """稳定格式化 MCP 返回的文本、列表或结构化内容。"""

    if isinstance(result, str):
        return result
    if isinstance(result, list):
        lines: List[str] = []
        for item in result[:50]:
            if isinstance(item, dict) and "text" in item:
                lines.append(str(item.get("text") or ""))
            else:
                lines.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(line for line in lines if line)
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


def _build_managed_file_list_response(payload: Dict[str, Any]) -> str:
    """把受管目录文件列表格式化为用户可读文本。"""

    files = [item for item in payload.get("files", []) if isinstance(item, dict)]
    query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
    root_key = str(query.get("root_key") or (files[0].get("root_key") if files else "") or "受管目录")
    path_prefix = str(query.get("path_prefix") or "").strip("/")
    scope_label = f"{root_key}/{path_prefix}" if path_prefix else root_key
    if not files:
        # 受管目录列表与主题检索含义不同；空结果必须保留用户选择的目录范围，
        # 不能使用“未找到相关文件”这种会让用户误以为执行了正文检索的文案。
        return f"{scope_label} 下暂未找到文件。"
    lines = [f"{scope_label} 下共有 {len(files)} 个文件："]
    lines.extend(_format_managed_file_tree(files[:50]))
    if len(files) > 50:
        lines.append(f"仅展示前 50 个文件，其余 {len(files) - 50} 个可继续筛选查看。")
    return "\n".join(lines)


def _build_workspace_file_search_response(payload: Dict[str, Any]) -> str:
    """格式化对话文件检索结果，不展示原文件名、内部状态或 Tool 信息。"""

    if not payload.get("ok"):
        message = str((payload.get("error") or {}).get("message") or "文件检索暂不可用")
        return f"暂时无法查找文件：{message}。"
    trash_restore_selection = payload.get("trash_restore_selection")
    if isinstance(trash_restore_selection, dict) and trash_restore_selection:
        return str(trash_restore_selection.get("message") or "找到了已删除文件，请选择是否恢复。")
    search_clarification = payload.get("search_clarification")
    if isinstance(search_clarification, dict) and search_clarification:
        return str(
            search_clarification.get("prompt")
            or "这个查找条件存在不同范围，请选择后继续。"
        )
    results = [item for item in payload.get("results", []) if isinstance(item, dict)]
    completeness = payload.get("search_completeness")
    completeness_message = (
        str(completeness.get("message") or "")
        if isinstance(completeness, dict)
        else ""
    )
    if not results:
        base_message = "没有找到与这段描述明确相关的已整理文件。你可以补充主题、年份、单位或文件类型后再找。"
        return "\n".join(item for item in (base_message, completeness_message) if item)
    supported_count = int(payload.get("supported_count") or 0)
    possible_count = int(payload.get("possible_count") or 0)
    if supported_count or possible_count:
        if supported_count and possible_count:
            lines = [
                f"找到 {supported_count} 个已验证相关文件，另有 {possible_count} 个可能相关文件："
            ]
        elif supported_count:
            lines = [f"找到 {supported_count} 个已验证相关文件："]
        else:
            lines = [
                f"未找到可确认的相关文件，以下 {possible_count} 个仅供继续查看："
            ]
    else:
        lines = [f"找到 {len(results)} 个相关文件："]
    for index, item in enumerate(results, start=1):
        filename = str(item.get("filename") or "未命名文件")
        category_path = "/".join(str(value) for value in item.get("category_path", []) if value)
        summary = str(item.get("summary") or "").strip()
        reasons = [str(value) for value in item.get("match_reasons", []) if value]
        line = f"{index}. {filename}"
        if category_path:
            line += f"（{category_path}）"
        if str(item.get("relevance_tier") or "") == "POSSIBLE":
            line += "【可能相关】"
        lines.append(line)
        if summary:
            lines.append(f"   {summary}")
        if reasons:
            lines.append(f"   推荐依据：{reasons[0]}")
    if completeness_message:
        lines.append(completeness_message)
    return "\n".join(lines)


def _format_managed_file_tree(files: List[Dict[str, Any]]) -> List[str]:
    """把受管文件相对路径压缩成目录树文本，避免深层路径平铺难读。"""

    tree: Dict[str, Any] = {}
    file_metadata: Dict[tuple[str, ...], Dict[str, Any]] = {}
    for file in files:
        filename = str(file.get("filename") or file.get("relative_path") or "未知文件")
        relative_path = str(file.get("relative_path") or filename)
        parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
        if not parts:
            parts = [filename]
        cursor = tree
        for directory in parts[:-1]:
            cursor = cursor.setdefault(directory, {})
        cursor.setdefault("__files__", []).append(parts[-1])
        file_metadata[tuple(parts)] = file
    return _render_managed_file_tree(tree=tree, file_metadata=file_metadata, prefix=(), depth=0)


def _render_managed_file_tree(
    *,
    tree: Dict[str, Any],
    file_metadata: Dict[tuple[str, ...], Dict[str, Any]],
    prefix: tuple[str, ...],
    depth: int,
) -> List[str]:
    """递归渲染目录树；目录和文件都按名称稳定排序。"""

    lines: List[str] = []
    indent = "  " * depth
    for directory in sorted(key for key in tree if key != "__files__"):
        lines.append(f"{indent}{directory}/")
        lines.extend(
            _render_managed_file_tree(
                tree=tree[directory],
                file_metadata=file_metadata,
                prefix=(*prefix, directory),
                depth=depth + 1,
            )
        )
    for filename in sorted(tree.get("__files__", [])):
        metadata = file_metadata.get((*prefix, filename), {})
        size_bytes = int(metadata.get("size_bytes") or 0)
        category_path = metadata.get("category_path")
        suffix = f"；分类：{category_path}" if category_path else ""
        lines.append(f"{indent}{filename}（{_format_size(size_bytes)}{suffix}）")
    return lines


def _build_classification_taxonomy_response(taxonomy: Dict[str, Any]) -> str:
    """把系统固定分类目录格式化为用户可读文本。"""

    name = taxonomy.get("name") or "文件分类目录"
    version = taxonomy.get("version") or "unknown"
    lines = [f"当前系统支持的文件分类目录：{name}（版本：{version}）"]
    for category in taxonomy.get("categories", []):
        if not isinstance(category, dict):
            continue
        lines.append(f"- {category.get('name') or '未命名分类'}")
        for child in category.get("children", []) or []:
            if isinstance(child, dict):
                lines.append(f"  - {child.get('name') or '未命名子类'}")
    return "\n".join(lines)


def _format_size(size_bytes: int) -> str:
    """格式化文件大小，避免直接展示原始字节数。"""

    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"


def _intent_summary_from_results(tool_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 Tool 结果中提取普通对话意图摘要。"""

    for result in tool_results:
        if result.get("ok") and result.get("intent"):
            return result
    return {}


def _build_general_chat_response(intent_summary: Dict[str, Any]) -> str:
    """为普通对话生成自然回复，避免泄露内部 Tool 审计信息。"""

    user_goal = str(intent_summary.get("user_goal") or "").strip()
    if intent_summary.get("intent") == "MISSING_FILE_SCOPE":
        if any(keyword in user_goal.lower() for keyword in ["重命名", "改名", "更名", "rename"]):
            return (
                "我还不能确定要重命名哪一个文件。请明确回复：把“原文件名.ext”重命名为“新文件名.ext”。"
            )
        return "我还不能确定要处理哪一个文件。请使用完整文件名。"
    if user_goal in {"你好", "您好", "hello", "hi", "Hello", "Hi"}:
        return "你好，我在。请告诉我你想聊什么。"
    return "我已收到。请继续说明你的需求。"


def _build_document_results_response(document_results: List[Dict[str, Any]]) -> str:
    """根据 document_results 生成逐文件处理回执。"""

    blocks = [f"已处理 {len(document_results)} 个文件："]
    for index, result in enumerate(document_results, start=1):
        filename = result.get("filename") or result.get("document_id") or "未知文件"
        if result.get("extraction_status") == "FAILED":
            error = (result.get("errors") or [{}])[0]
            message = error.get("message") if isinstance(error, dict) else "未知错误"
            blocks.append(
                f"{index}. {filename}\n"
                "解析结果：失败\n"
                f"失败原因：{message}"
            )
            continue

        categories = result.get("categories") or []
        block = (
            f"{index}. {filename}\n"
            f"解析结果：成功，提取 {result.get('page_count', 0)} 页/Sheet，共 {result.get('char_count', 0)} 个字符"
        )
        if categories:
            block += "\n分类建议：\n" + _format_category_receipt(categories)
        blocks.append(block)
    return "\n\n".join(blocks)


def _build_document_summary_response(*, document_results: List[Dict[str, Any]], extraction_results: List[Dict[str, Any]]) -> str:
    """生成摘要不可用回执，禁止把 Tool 的短预览伪装成文档总结。

    真正的总结必须由文档阅读服务读取完整 ``document_pages`` 后生成；此函数只处理正文为空或摘要服务
    未取得完整正文的降级场景。
    """

    blocks = [f"已读取 {len(document_results)} 个文件，但暂时无法生成完整内容总结："]
    for index, result in enumerate(document_results, start=1):
        filename = result.get("filename") or result.get("document_id") or "未知文件"
        if result.get("extraction_status") == "FAILED":
            error = (result.get("errors") or [{}])[0]
            message = error.get("message") if isinstance(error, dict) else "未知错误"
            blocks.append(f"{index}. {filename}\n无法总结：{message}")
            continue

        blocks.append(
            f"{index}. {filename}\n"
            "未取得可供总结的完整正文，请等待文件解析完成后重试。"
        )
    return "\n\n".join(blocks)


def _format_category_receipt(categories: List[Dict[str, Any]]) -> str:
    """把多个分类建议格式化为带置信度和证据的回执片段。"""

    if not categories:
        return "- 其他（暂无明确关键词依据）"
    formatted_items: list[str] = []
    visible_categories = categories[:3]
    for category in visible_categories:
        evidence = category.get("evidence") or []
        evidence_items = [item for item in category.get("evidence_items", []) if isinstance(item, dict)]
        name = category.get("name") or "其他"
        if name == "其他" and not evidence:
            formatted_items.append("- 其他（暂无明确关键词依据）")
            continue
        evidence_text = _format_evidence_item(evidence_items[0]) if evidence_items else ""
        if not evidence_text:
            evidence_text = "、".join(str(item) for item in evidence[:3]) or "暂无明确关键词依据"
        confidence = float(category.get("confidence", 0))
        formatted_items.append(
            f"- {name}\n"
            f"  置信度：{confidence:.2f}\n"
            f"  依据：{evidence_text}"
        )
    hidden_count = len(categories) - len(visible_categories)
    if hidden_count > 0:
        formatted_items.append(f"另有 {hidden_count} 个低置信度候选未展示。")
    return "\n".join(formatted_items)


def _format_evidence_item(evidence_item: Dict[str, Any]) -> str:
    """把结构化证据格式化为用户可读的页码/Sheet + 原文片段。"""

    quote = str(evidence_item.get("quote") or "")
    if not quote:
        return ""
    page_number = evidence_item.get("page_number")
    sheet_name = evidence_item.get("sheet_name")
    if sheet_name:
        return f"Sheet {sheet_name}：“{quote}”"
    if page_number:
        return f"第 {page_number} 页：“{quote}”"
    return f"“{quote}”"
