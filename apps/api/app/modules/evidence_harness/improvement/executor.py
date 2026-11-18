"""宿主可信执行层：固定回归、首轮规划代理及整个多轮实验的统一模型预算。"""

from collections import Counter
import time

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.improvement.isolation import isolated_run
from app.modules.evidence_harness.improvement.codex_backend import CodexLoginTransport
from app.modules.evidence_harness.routing.runtime import inspect_plan
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic


class Journal:
    """逐请求先记账，费用未知不等于零，跨阶段和跨轮次不重置。"""

    def __init__(self, output, authorization, clock=time.monotonic, *, profile_ref=None):
        """运行依赖不进入可持久化业务State。"""
        self.output, self.authorization, self.clock = output, authorization, clock
        self.started, self.used = clock(), 0
        # 新C实验绑定同一冻结Provider配置；旧A/B和旧自改流程保持原记录格式。
        self.profile_ref = profile_ref

    def elapsed(self):
        """墙钟预算包括快照、生成、隔离和比较准备。"""
        return max(0, int((self.clock() - self.started) * 1000))

    def remaining(self):
        """本机检查也受整轮剩余时限约束。"""
        remaining = self.authorization.max_active_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
        return remaining

    def call(self, backend, *, role, round_id, arm=None, case_id=None, repetition=None, system_prompt, user_payload):
        """只有宿主能调用Provider，模型生成补丁也消耗同一总授权。"""
        if self.used >= self.authorization.max_total_requests:
            raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
        if self.remaining() < 62:
            raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
        if len(system_prompt) + len(str(user_payload)) > 400000:
            raise ExperimentStopped("SELF_IMPROVEMENT_REQUEST_TOO_LARGE")
        self.used += 1
        limit = (self.authorization.advisor_max_output_tokens if role == "ADVISOR"
                 else self.authorization.planner_max_output_tokens)
        record = {"sequence": self.used, "round_id": round_id, "role": role, "arm": arm,
                  "case_id": case_id, "repetition": repetition, "request_ref": digest([system_prompt, user_payload]),
                  "max_output_tokens": limit, "timeout_seconds": 60, "active_ms_before": self.elapsed()}
        if self.profile_ref is not None:
            record["profile_ref"] = self.profile_ref
        path = self.output / "calls" / f"{self.used:04d}"
        write_new_json(path.with_suffix(".started.json"), record)
        started, usage, diagnostic, status = self.clock(), {}, None, "FAILED"
        try:
            raw, usage = backend.complete_json_with_usage(system_prompt=system_prompt,
                user_payload=user_payload, max_output_tokens=limit)
            status = "COMPLETED"
            return raw
        except Exception as exc:
            diagnostic = (exc.diagnostic if isinstance(exc, LLMResponseError) and exc.diagnostic
                          else make_diagnostic(category="INTERNAL_ERROR", stage="LLM_REQUEST", exc=exc))
            raise LLMResponseError("SELF_MODEL_CALL_FAILED", diagnostic=diagnostic) from None
        finally:
            # 仅采信本机受控适配器生成的固定诊断结构；原API不添加CLI字段。
            codex_details = {"codex_event_diagnostic": backend.diagnostic_summary()} if isinstance(backend, CodexLoginTransport) else {}
            known = {k: usage[k] for k in ("input_tokens", "output_tokens")
                     if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
            write_new_json(path.with_suffix(".finished.json"), {**record, "status": status,
                "duration_ms": max(0, int((self.clock() - started) * 1000)),
                "input_tokens": known.get("input_tokens"), "output_tokens": known.get("output_tokens"),
                "diagnostic": diagnostic.model_dump(mode="json") if diagnostic else None, **codex_details})


def validate_planner_request(item, case, catalog, *, phase=None, adaptive_prompt=None):
    """候选进程不能借模型代理外发任意内容，只能发送既有规划服务的固定请求。"""
    from app.modules.llm.prompts import USER_INTENT_SYSTEM_PROMPT
    from app.modules.llm.schemas import UserIntentPlan
    from app.modules.agent.adaptive_planner import ADAPTIVE_PLANNER_SYSTEM_PROMPT
    from app.modules.agent.planner_contracts import PlannerDecision
    from app.modules.agent.graph import _planning_attachments
    # 只有可信宿主可以指定已校验的候选提示；子进程不能自行选择任意Prompt。
    if adaptive_prompt is not None and (not isinstance(adaptive_prompt, str)
        or not adaptive_prompt.startswith(ADAPTIVE_PLANNER_SYSTEM_PROMPT + "\n\n")
        or len(adaptive_prompt) > len(ADAPTIVE_PLANNER_SYSTEM_PROMPT) + 3100):
        raise ValueError("BROKER_POLICY_PROMPT_INVALID")
    prompts = {"LEGACY": USER_INTENT_SYSTEM_PROMPT, "ADAPTIVE": adaptive_prompt or ADAPTIVE_PLANNER_SYSTEM_PROMPT}
    phase = phase or "LEGACY"
    if phase not in prompts or set(item) != {"type", "system_prompt", "user_payload"} or item["system_prompt"] != prompts[phase]:
        raise ValueError("BROKER_PROMPT_NOT_ALLOWED")
    # Planning会用后端context覆盖附件元数据（无context则为空），代理须复用同一纯投影。
    # 仍固定原document_id，不能为适配请求而放行前端自报文件名或任意附件内容。
    planning_attachments = _planning_attachments({"attachments": [d.model_dump() for d in case.attachments],
        "context_documents": [d.model_dump() for d in case.context_documents]})
    expected = {"message": case.question, "attachments": planning_attachments,
                "context_documents": [d.model_dump() for d in case.context_documents],
                "catalog_snapshot": catalog,
                "output_schema": (PlannerDecision if phase == "ADAPTIVE" else UserIntentPlan).model_json_schema()}
    payload = item["user_payload"]
    if set(payload) != set(expected) | {"observation"} or any(payload.get(k) != v for k, v in expected.items()):
        raise ValueError("BROKER_INPUT_NOT_ALLOWED")
    observation = payload["observation"]
    if phase == "ADAPTIVE" and observation:
        raise ValueError("BROKER_INITIAL_OBSERVATION_NOT_ALLOWED")
    if observation:
        repair = observation.get("source_plan_validation", {})
        if (set(observation) != {"source_plan_validation"}
            or set(repair) != {"error_code", "locked_intent", "repair_attempt", "max_repairs"}
            or repair["repair_attempt"] != 1 or repair["max_repairs"] != 1
            or repair["locked_intent"] not in {"ANSWER_QUESTION_FROM_FILES", "ANSWER_FROM_FILES", "EVIDENCE_ANSWER"}
            or repair["error_code"] not in {"SOURCE_EXACT_PHRASE_NOT_GROUNDED", "SOURCE_QUERY_REQUIRED",
                                          "SOURCE_TIME_FILTER_MISMATCH", "SOURCE_QUERY_NOT_GROUNDED"}):
            raise ValueError("BROKER_REPAIR_NOT_ALLOWED")


def run_trial(*, code, scratch, case, catalog, journal, backend, round_id, arm, repetition, capture_audit=False, adaptive_prompt=None):
    """最多三个规划请求，状态和计划在宿主重新检查，不采信候选自评分。"""
    calls, failed, adaptive_seen = 0, 0, False

    def broker(item):
        """宿主核对原规划请求后才代理调用，不能成为候选的任意外发通道。"""
        nonlocal calls, failed, adaptive_seen
        from app.modules.agent.adaptive_planner import ADAPTIVE_PLANNER_SYSTEM_PROMPT
        is_adaptive = item.get("system_prompt") == (adaptive_prompt or ADAPTIVE_PLANNER_SYSTEM_PROMPT)
        if is_adaptive:
            if case.adaptive_mode != "enabled" or calls or adaptive_seen:
                raise ValueError("BROKER_ADAPTIVE_SEQUENCE_INVALID")
            adaptive_seen = True
        elif case.adaptive_mode == "enabled" and not adaptive_seen:
            raise ValueError("BROKER_FALLBACK_WITHOUT_ADAPTIVE")
        validate_planner_request(item, case, catalog, phase="ADAPTIVE" if is_adaptive else "LEGACY", adaptive_prompt=adaptive_prompt)
        if calls >= 3:
            raise ExperimentStopped("TRIAL_REQUEST_LIMIT")
        calls += 1
        try:
            raw = journal.call(backend, role="PLANNER", round_id=round_id, arm=arm,
                case_id=case.case_id, repetition=repetition,
                system_prompt=item["system_prompt"], user_payload=item["user_payload"])
            return {"status": "COMPLETED", "raw": raw}
        except LLMResponseError:
            failed += 1
            return {"status": "FAILED"}

    result = isolated_run(code, scratch, {"mode": "planning", "case": case.model_dump(mode="json"),
                          "catalog": catalog, "capture_audit": capture_audit}, broker=broker,
                          timeout=min(240, journal.remaining()), planner_mode=case.adaptive_mode)
    observed = inspect_plan(case, result.get("plan") or {})
    result.update(observed)
    target_mode = "ADAPTIVE" if case.adaptive_mode == "enabled" else "LEGACY"
    actual_mode_matches = not capture_audit or result.get("planning_audit", {}).get("effective_mode") == target_mode
    result["strategy_evaluable"] = bool(calls and not failed and not result.get("diagnostics", {}).get("fallback") and actual_mode_matches)
    return result


def regression_inputs():
    """固定受保护行为：普通查找、显式附件、缺源改名及分类，不能由模型删改。"""
    attachment = {"document_id": "test-document", "filename": "培训通知.pdf"}
    examples = [
        ("查找培训通知，列出文件名即可。", "SEARCH_FILES", []),
        ("只根据附件说明培训对象。", "ANSWER_QUESTION_FROM_FILES", [attachment]),
        ("把它改名为工作总结.pdf", "RENAME_FILES", []),
        ("请读取并分类附件。", "CLASSIFY_FILES", [attachment]),
        ("查找2025年的工作总结文件。", "SEARCH_FILES", []),
    ]
    return [{"mode": "regression", "message": message, "attachments": attachments,
             "intent": {"intent": intent, "user_goal": message, "managed_query": "培训通知"}}
            for message, intent, attachments in examples]


def run_checks(root, journal):
    """固定编译和回归清单，两组一致性由可信宿主比较。"""
    rows = []
    for arm in ("control", "candidate"):
        isolated_run(root / arm, root / f"check-{arm}-compile", {"mode": "compile"},
                     timeout=min(90, journal.remaining()))
    for index, request in enumerate(regression_inputs()):
        results = {arm: isolated_run(root / arm, root / f"check-{index}-{arm}", request,
                                    timeout=min(30, journal.remaining())) for arm in ("control", "candidate")}
        rows.append({"check_id": index, "input_ref": digest(request), **results})
    return {"compiled": True, "rows": rows,
            "passed": all(r["control"] == r["candidate"] for r in rows)}
