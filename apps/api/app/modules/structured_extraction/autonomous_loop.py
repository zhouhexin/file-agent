"""结构化抽取后台阶段的受预算 Adaptive Planner 局部增强循环。"""

from __future__ import annotations

from typing import Any

from app.db.models import AgentRun, Message, StructuredExtractionRun, ToolInvocation, new_uuid, utcnow
from app.modules.agent.adaptive_planner import AdaptivePlannerService
from app.modules.agent.tool_schemas import StructuredFieldSpec, StructuredImageExtractionInput
from app.modules.structured_extraction.service import (
    StructuredExtractionService,
    structured_extraction_fingerprint,
)


class StructuredExtractionAutonomousLoop:
    """允许 Planner 根据安全观察选择一次 VISION_CROP 增强。"""

    def __init__(self, *, db: Any, planner: Any | None = None) -> None:
        """保存 worker 数据库会话，并允许测试注入 deterministic fake Planner。"""

        self.db = db
        self.planner = planner or AdaptivePlannerService()

    def maybe_enhance(
        self,
        *,
        run: StructuredExtractionRun,
        initial_output: dict[str, Any],
        service: StructuredExtractionService,
    ) -> tuple[dict[str, Any], str | None]:
        """返回初始或合并结果，以及可选澄清问题。"""

        if not self._eligible(run=run, output=initial_output):
            return initial_output, None
        agent_run = self.db.get(AgentRun, run.agent_run_id) if run.agent_run_id else None
        if agent_run is None:
            return initial_output, None
        graph_state = dict(agent_run.graph_state_json or {})
        if int(graph_state.get("tool_call_count", 0)) >= 5:
            return initial_output, None
        structured_calls = (
            self.db.query(ToolInvocation)
            .filter(
                ToolInvocation.agent_run_id == agent_run.id,
                ToolInvocation.tool_name == "extract-image-structured-data",
            )
            .count()
        )
        if structured_calls >= 2:
            return initial_output, None
        message = self.db.get(Message, agent_run.message_id)
        try:
            decision = self.planner.decide(
                message=str(message.content if message is not None else graph_state.get("message") or ""),
                attachments=list(graph_state.get("attachments") or []),
                context_documents=list(graph_state.get("context_documents") or []),
                observation=_safe_observation(initial_output),
                catalog_snapshot=dict(graph_state.get("catalog_snapshot") or {}),
            )
        except Exception:
            # Planner 不可用不能抹掉已经完成的初始抽取事实。
            return initial_output, None
        if decision.decision_type == "CLARIFY":
            return initial_output, str(decision.clarification.question)
        if decision.decision_type != "TOOL_PLAN" or decision.tool_plan is None:
            return initial_output, None
        retry_input = self._validated_retry_input(
            run=run,
            output=initial_output,
            steps=decision.tool_plan.steps,
        )
        if retry_input is None:
            return initial_output, None
        fingerprint = structured_extraction_fingerprint(retry_input)
        reusable = service.repository.find_reusable_run(
            document_version_id=run.document_version_id,
            schema_fingerprint=fingerprint,
            provider=service.layout_provider.name,
            model_name=service.model_identity,
            prompt_version=service.settings.structured_extraction_prompt_version,
            retry_strategy="VISION_CROP",
        )
        child = reusable or service.repository.create_run(
            tool_input=retry_input,
            document_version_id=run.document_version_id,
            schema_fingerprint=fingerprint,
            provider=service.layout_provider.name,
            model_name=service.model_identity,
            prompt_version=service.settings.structured_extraction_prompt_version,
            agent_run_id=agent_run.id,
            parent_run_id=run.id,
        )
        try:
            child_output = (
                service._tool_output(
                    run=child,
                    result=service.result_for_run(child),
                    reused=True,
                )
                if reusable is not None
                else service.execute_run(child)
            )
        except Exception:
            if reusable is None:
                service.repository.fail_run(
                    run=child,
                    code="VISION_CROP_ENHANCEMENT_FAILED",
                    message="局部视觉增强失败，已保留初始结构化抽取结果。",
                )
            self.db.add(
                ToolInvocation(
                    id=new_uuid(),
                    agent_run_id=agent_run.id,
                    tool_name="extract-image-structured-data",
                    input_json=retry_input.model_dump(),
                    output_json={
                        "kind": "structured_image_extraction",
                        "ok": False,
                        "status": "FAILED",
                        "document_id": run.document_id,
                        "error": {
                            "code": "VISION_CROP_ENHANCEMENT_FAILED",
                            "message": "局部视觉增强失败，已保留初始结果。",
                            "retryable": False,
                            "user_action_required": False,
                        },
                    },
                    status="FAILED",
                    finished_at=utcnow(),
                )
            )
            self.db.flush()
            return initial_output, None
        merged = merge_structured_outputs(
            initial=initial_output,
            enhanced=child_output,
            target_field_keys=retry_input.target_field_keys,
        )
        invocation = ToolInvocation(
            id=new_uuid(),
            agent_run_id=agent_run.id,
            tool_name="extract-image-structured-data",
            input_json=retry_input.model_dump(),
            output_json=child_output,
            status=str(child_output.get("status") or "COMPLETED"),
            changeset_id=child_output.get("changeset_id"),
            finished_at=utcnow(),
        )
        self.db.add(invocation)
        self.db.flush()
        graph_state["planning_round"] = min(3, int(graph_state.get("planning_round", 0)) + 1)
        graph_state["tool_call_count"] = min(5, int(graph_state.get("tool_call_count", 0)) + 1)
        agent_run.graph_state_json = graph_state
        agent_run.updated_at = utcnow()
        return merged, None

    def _eligible(self, *, run: StructuredExtractionRun, output: dict[str, Any]) -> bool:
        """只有初次中等质量且后端明确可重试时进入 Planner。"""

        return bool(
            getattr(self.planner, "enabled", True)
            and run.retry_strategy == "INITIAL"
            and output.get("retryable") is True
            and output.get("recommended_retry_strategy") == "VISION_CROP"
            and output.get("low_confidence_field_keys")
        )

    def _validated_retry_input(
        self,
        *,
        run: StructuredExtractionRun,
        output: dict[str, Any],
        steps: list[Any],
    ) -> StructuredImageExtractionInput | None:
        """关闭式校验 Planner 只请求一次允许字段的局部增强。"""

        if len(steps) != 1:
            return None
        step = steps[0]
        if (
            step.skill_id != "image-structured-extraction"
            or step.tool_name != "extract-image-structured-data"
            or step.bindings
            or step.requires_confirmation
        ):
            return None
        try:
            candidate = StructuredImageExtractionInput.model_validate(step.literal_input)
        except Exception:
            return None
        allowed_targets = set(output.get("low_confidence_field_keys") or [])
        if (
            candidate.document_id != run.document_id
            or candidate.retry_strategy != "VISION_CROP"
            or candidate.schema_mode != "EXPLICIT_FIELDS"
            or not set(candidate.target_field_keys).issubset(allowed_targets)
        ):
            return None
        original_schema = {
            item.key: item
            for item in (
                StructuredFieldSpec.model_validate(value)
                for value in output.get("field_schema") or run.field_schema_json or []
            )
        }
        if any(
            field.key not in original_schema
            or field.model_dump() != original_schema[field.key].model_dump()
            for field in candidate.fields
        ):
            return None
        if set(candidate.target_field_keys) != {field.key for field in candidate.fields}:
            return None
        return candidate


def _safe_observation(output: dict[str, Any]) -> dict[str, Any]:
    """构造不含字段值、路径、bbox 和 OCR 正文的 Planner 观察。"""

    return {
        "planning_round": 1,
        "tool_call_count": 1,
        "remaining_planning_rounds": 2,
        "remaining_tool_calls": 4,
        "results": [
            {
                "tool_name": "extract-image-structured-data",
                "observation_kind": "structured_image_extraction",
                "status": str(output.get("status") or ""),
                "ok": bool(output.get("ok")),
                "document_ids": [str(output.get("document_id") or "")],
                "result_count": int(output.get("record_count") or 0),
                "structured_extraction": {
                    "record_count": int(output.get("record_count") or 0),
                    "field_count": int(output.get("field_count") or 0),
                    "review_count": int(output.get("review_count") or 0),
                    "missing_required_field_count": int(
                        output.get("missing_required_field_count") or 0
                    ),
                    "quality_band": str(output.get("quality_band") or "LOW"),
                    "retryable": bool(output.get("retryable")),
                    "recommended_retry_strategy": str(
                        output.get("recommended_retry_strategy") or "NONE"
                    ),
                    "low_confidence_field_keys": list(
                        output.get("low_confidence_field_keys") or []
                    )[:20],
                },
                "available_next_decisions": ["TOOL_PLAN", "CLARIFY", "FINISH"],
            }
        ],
    }


def merge_structured_outputs(
    *,
    initial: dict[str, Any],
    enhanced: dict[str, Any],
    target_field_keys: list[str],
) -> dict[str, Any]:
    """仅用证据有效且置信度更高的增强字段替换初始字段。"""

    merged = dict(initial)
    records = {
        int(item.get("record_index") or 0): {
            **item,
            "fields": dict(item.get("fields") or {}),
        }
        for item in initial.get("records") or []
        if isinstance(item, dict)
    }
    for enhanced_record in enhanced.get("records") or []:
        if not isinstance(enhanced_record, dict):
            continue
        index = int(enhanced_record.get("record_index") or 0)
        target = records.get(index)
        if target is None:
            # 局部增强只能修正初始记录，不能借第二次模型调用扩张记录范围。
            continue
        for key, candidate in dict(enhanced_record.get("fields") or {}).items():
            if key not in target_field_keys or not isinstance(candidate, dict):
                continue
            current = target["fields"].get(key)
            if (
                candidate.get("status") in {"NORMALIZED", "EXTRACTED"}
                and (
                    current is None
                    or float(candidate.get("confidence") or 0)
                    > float(current.get("confidence") or 0)
                )
            ):
                target["fields"][key] = candidate
    merged["records"] = [records[index] for index in sorted(records)]
    review_items = []
    low_keys: list[str] = []
    schema_by_key = {
        str(item.get("key") or ""): item
        for item in merged.get("field_schema") or []
        if isinstance(item, dict)
    }
    accepted_confidences: list[float] = []
    missing_required_count = 0
    for record in merged["records"]:
        for key, value in dict(record.get("fields") or {}).items():
            if value.get("status") not in {"NEEDS_REVIEW", "MISSING", "CONFLICTED"}:
                accepted_confidences.append(float(value.get("confidence") or 0))
                continue
            low_keys.append(key)
            if value.get("status") == "MISSING" and bool(
                (schema_by_key.get(key) or {}).get("required")
            ):
                missing_required_count += 1
            review_items.append(
                {
                    "record_index": record.get("record_index"),
                    "field_key": key,
                    "field_label": str((schema_by_key.get(key) or {}).get("label") or key),
                    "status": value.get("status"),
                    "raw_text": value.get("raw_text"),
                    "reason_codes": value.get("warnings") or [],
                    "page_number": (value.get("evidence") or {}).get("page_number"),
                }
            )
    merged["review_items"] = review_items
    merged["review_count"] = len(review_items)
    merged["missing_required_field_count"] = missing_required_count
    merged["quality_score"] = round(
        sum(accepted_confidences) / len(accepted_confidences), 4
    ) if accepted_confidences else 0.0
    merged["low_confidence_field_keys"] = list(dict.fromkeys(low_keys))[:20]
    merged["retryable"] = False
    merged["recommended_retry_strategy"] = "NONE"
    merged["quality_band"] = (
        "HIGH"
        if not review_items
        else "MEDIUM"
        if accepted_confidences
        else "LOW"
    )
    merged["status"] = (
        "COMPLETED"
        if not review_items
        else "PARTIAL"
        if accepted_confidences
        else "NEEDS_REVIEW"
    )
    return merged
