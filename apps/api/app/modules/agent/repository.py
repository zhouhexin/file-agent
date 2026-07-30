"""AgentRun 和 ToolInvocation 的持久化仓库。

LangGraph 节点仍只负责状态流转，运行审计数据通过仓库统一写入数据库。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import (
    AgentRun,
    PlannerShadowComparison,
    ToolInvocation,
    utcnow,
)
from app.modules.agent.state import AgentRunResult, ToolInvocationRecord
from app.modules.changesets.service import persist_changeset_from_document_results
from app.modules.classification.service import persist_document_results_classifications


class AgentRunRepository:
    """封装 AgentRun 和 ToolInvocation 的数据库操作。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def create_run(self, conversation_id: str, message_id: str, user_id: str) -> AgentRun:
        """创建 RECEIVED 状态的 AgentRun。"""

        run = AgentRun(
            conversation_id=conversation_id,
            message_id=message_id,
            user_id=user_id,
            status="RECEIVED",
        )
        self.db.add(run)
        self.db.flush()
        return run

    def update_run_from_state(self, run: AgentRun, state: dict[str, Any]) -> AgentRun:
        """用 LangGraph 最终状态更新 AgentRun 审计字段。"""

        document_results = state.get("document_results", [])
        changeset = persist_changeset_from_document_results(
            db=self.db,
            run=run,
            document_results=document_results,
        )
        if changeset is not None:
            state["changeset_id"] = changeset.id
        persist_document_results_classifications(
            db=self.db,
            agent_run_id=run.id,
            document_results=document_results,
        )

        run.intent = state.get("intent")
        run.status = state.get("status", run.status)
        run.selected_skills_json = state.get("selected_skills", [])
        run.plan_json = state.get("tool_plan", {})
        run.planner_mode = state.get("adaptive_planner_mode", "legacy")
        run.planner_schema_version = str(
            state.get("planner_schema_version")
            or "planner-decision-v1"
        )
        catalog = state.get("catalog_snapshot", {})
        run.catalog_version = str(catalog.get("catalog_version") or "")
        run.catalog_fingerprint = str(catalog.get("catalog_fingerprint") or "")
        run.graph_state_json = _safe_graph_state_snapshot(state)
        run.final_response = state.get("final_response")
        run.error_message = "; ".join(state.get("errors", [])) or None
        run.updated_at = utcnow()
        self.db.flush()
        return run

    def mark_failed(self, run: AgentRun, error_message: str) -> AgentRun:
        """运行失败时记录 FAILED 状态和错误信息。"""

        run.status = "FAILED"
        run.error_message = error_message
        run.updated_at = utcnow()
        self.db.flush()
        return run

    def record_shadow_comparison(
        self,
        *,
        run: AgentRun,
        state: dict[str, Any],
    ) -> PlannerShadowComparison | None:
        """持久化新旧 Planner 的脱敏结构对比，不保存 Prompt 或文件正文。"""

        shadow = state.get("shadow_planner_decision", {})
        if (
            not isinstance(shadow, dict)
            or not shadow.get("validation_status")
        ):
            return None
        adaptive_payload = shadow.get("decision")
        has_adaptive_decision = isinstance(adaptive_payload, dict)
        shadow_valid = (
            has_adaptive_decision
            and shadow.get("validation_status") == "COMPLETED"
        )
        adaptive = adaptive_payload if has_adaptive_decision else {}
        legacy = state.get("planner_decision", {})
        legacy_plan = legacy.get("tool_plan") or {}
        adaptive_plan = adaptive.get("tool_plan") or {}
        normalized_adaptive_plan = shadow.get("normalized_tool_plan") or {}
        legacy_steps = list(legacy_plan.get("steps") or [])
        adaptive_steps = list(adaptive_plan.get("steps") or [])
        normalized_adaptive_steps = list(
            normalized_adaptive_plan.get("steps") or []
        )
        legacy_tools = [
            str(item.get("tool_name") or "") for item in legacy_steps
        ]
        adaptive_tools = [
            str(item.get("tool_name") or "") for item in adaptive_steps
        ]
        comparison = PlannerShadowComparison(
            agent_run_id=run.id,
            legacy_decision_type=str(legacy.get("decision_type") or "TOOL_PLAN"),
            adaptive_decision_type=str(
                adaptive.get("decision_type")
                if shadow_valid
                else (
                    "INVALID"
                    if has_adaptive_decision
                    else "UNAVAILABLE"
                )
            ),
            legacy_intent=str(legacy.get("intent") or ""),
            adaptive_intent=str(adaptive.get("intent") or ""),
            legacy_skill_ids_json=list(
                legacy.get("selected_skill_ids") or []
            ),
            adaptive_skill_ids_json=list(
                adaptive.get("selected_skill_ids") or []
            ),
            legacy_tool_names_json=legacy_tools,
            adaptive_tool_names_json=adaptive_tools,
            # Shadow 生成或校验失败时必须记为不匹配，不能让两个空列表把
            # 失败样本误算成 scope/risk/confirmation 一致。
            scope_match=shadow_valid
            and (
                list((legacy.get("scope") or {}).get("document_ids") or [])
                == list((adaptive.get("scope") or {}).get("document_ids") or [])
            ),
            risk_match=shadow_valid
            and _step_field_values(
                state.get("tool_plan", {}).get("steps", []),
                "risk_level",
            )
            == _step_field_values(normalized_adaptive_steps, "risk_level"),
            confirmation_match=shadow_valid
            and _step_field_values(
                state.get("tool_plan", {}).get("steps", []),
                "requires_confirmation",
            )
            == _step_field_values(
                normalized_adaptive_steps,
                "requires_confirmation",
            ),
            adaptive_validation_status=str(
                shadow.get("validation_status") or "FAILED"
            ),
            adaptive_error_code=shadow.get("error_code"),
            catalog_fingerprint=str(
                state.get("catalog_snapshot", {}).get(
                    "catalog_fingerprint"
                )
                or ""
            ),
            schema_version=str(
                state.get("planner_schema_version")
                or "planner-decision-v1"
            ),
        )
        self.db.add(comparison)
        self.db.flush()
        return comparison

    def create_tool_invocation(self, agent_run_id: str, record: ToolInvocationRecord) -> ToolInvocation:
        """把一次 Tool 调用记录写入数据库。"""

        invocation = ToolInvocation(
            id=record.id,
            agent_run_id=agent_run_id,
            tool_name=record.tool_name,
            input_json=record.input_json,
            output_json=record.output_json,
            status=record.status,
            changeset_id=record.changeset_id,
            operation_plan_id=record.operation_plan_id,
            finished_at=utcnow(),
        )
        self.db.add(invocation)
        self.db.flush()
        return invocation

    def get_run(self, agent_run_id: str) -> AgentRun | None:
        """按 id 查询 AgentRun。"""

        return self.db.get(AgentRun, agent_run_id)

    def list_tool_invocations(self, agent_run_id: str) -> list[ToolInvocation]:
        """查询某次 AgentRun 的 Tool 调用记录。"""

        return (
            self.db.query(ToolInvocation)
            .filter(ToolInvocation.agent_run_id == agent_run_id)
            .order_by(ToolInvocation.created_at.asc())
            .all()
        )

    def to_result(self, run: AgentRun, invocations: list[ToolInvocation] | None = None) -> AgentRunResult:
        """把 ORM AgentRun 转为 API 返回模型。"""

        invocation_models = [
            ToolInvocationRecord(
                id=item.id,
                tool_name=item.tool_name,
                input_json=item.input_json,
                output_json=item.output_json,
                status=item.status,
                changeset_id=item.changeset_id,
                operation_plan_id=item.operation_plan_id,
            )
            for item in (invocations if invocations is not None else self.list_tool_invocations(run.id))
        ]
        graph_state = run.graph_state_json or {}
        return AgentRunResult(
            agent_run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.user_id,
            message_id=run.message_id,
            intent=run.intent,
            status=run.status,
            selected_skills=run.selected_skills_json,
            tool_plan=run.plan_json,
            tool_results=[item.output_json for item in invocation_models],
            tool_invocations=invocation_models,
            document_results=graph_state.get("document_results", []),
            search_context={
                "effective_conditions": graph_state.get(
                    "effective_conditions", []
                ),
                "attempts": graph_state.get("search_attempts", []),
            },
            async_job_ids=graph_state.get("async_job_ids", []),
            changeset_id=_first_valid_uuid([run.changeset_id, *[item.changeset_id for item in invocation_models]]),
            operation_plan_id=_last_non_empty(
                [
                    item.operation_plan_id
                    for item in invocation_models
                    if not (
                        item.output_json.get("kind")
                        == "working_copy_operation_result"
                        and item.output_json.get("status") == "EXECUTED"
                    )
                ]
            ),
            final_response=run.final_response,
            errors=[run.error_message] if run.error_message else [],
        )


def _safe_graph_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """保存可 JSON 序列化的图状态快照，避免把 registry/planner 对象写入数据库。"""

    return {
        "agent_run_id": state.get("agent_run_id"),
        "conversation_id": state.get("conversation_id"),
        "message_id": state.get("message_id"),
        "planner_mode": state.get("planner_mode"),
        "adaptive_planner_mode": state.get("adaptive_planner_mode", "legacy"),
        "planner_schema_version": state.get(
            "planner_schema_version",
            "planner-decision-v1",
        ),
        "shadow_planner_decision": state.get("shadow_planner_decision", {}),
        "decision_type": state.get("decision_type", "TOOL_PLAN"),
        "planning_round": state.get("planning_round", 0),
        "tool_call_count": state.get("tool_call_count", 0),
        "executed_tool_signatures": state.get("executed_tool_signatures", []),
        "observation": state.get("observation", {}),
        "search_attempts": state.get("search_attempts", []),
        "effective_conditions": state.get("effective_conditions", []),
        "observed_document_ids": state.get("observed_document_ids", []),
        "replan_requested": state.get("replan_requested", False),
        "waiting_for_confirmation": state.get(
            "waiting_for_confirmation",
            False,
        ),
        "status": state.get("status"),
        "intent": state.get("intent"),
        "slots": state.get("slots", {}),
        "selected_skills": state.get("selected_skills", []),
        "context_documents": state.get("context_documents", []),
        "catalog_snapshot": _safe_catalog_snapshot(state.get("catalog_snapshot", {})),
        "planner_decision": state.get("planner_decision", {}),
        "user_intent_plan": state.get("user_intent_plan", {}),
        "current_step_index": state.get("current_step_index", 0),
        "step_results": state.get("step_results", {}),
        "completed_step_ids": state.get("completed_step_ids", []),
        "failed_step_ids": state.get("failed_step_ids", []),
        "capability_suggestions": state.get("capability_suggestions", []),
        "tool_results": state.get("tool_results", []),
        "result_summary": state.get("result_summary", {}),
        "document_results": state.get("document_results", []),
        "async_job_ids": state.get("async_job_ids", []),
        "changeset_id": state.get("changeset_id"),
        "operation_plan_id": state.get("operation_plan_id"),
        "final_response": state.get("final_response"),
        "errors": state.get("errors", []),
    }


def _safe_catalog_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """只持久化 Catalog 身份和启用名称，不重复保存完整 schema。"""

    return {
        "catalog_version": snapshot.get("catalog_version"),
        "catalog_fingerprint": snapshot.get("catalog_fingerprint"),
        "enabled_tool_names": snapshot.get("enabled_tool_names", []),
        "enabled_skill_ids": snapshot.get("enabled_skill_ids", []),
    }


def _last_non_empty(values: list[str | None]) -> str | None:
    """从列表中取最后一个非空标识。"""

    for value in reversed(values):
        if value:
            return value
    return None


def _first_valid_uuid(values: list[str | None]) -> str | None:
    """只返回真实 UUID 标识，过滤 changeset-memory 等历史占位值。"""

    for value in values:
        if not value:
            continue
        try:
            UUID(str(value))
        except ValueError:
            continue
        return str(value)
    return None


def _step_field_values(steps: list[dict[str, Any]], field: str) -> list[Any]:
    """提取步骤安全元数据用于 Shadow 对比。"""

    return [step.get(field) for step in steps]
