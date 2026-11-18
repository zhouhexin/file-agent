"""仅用于离线路由实验的安全诊断，不改变规划结果或生产分支。"""

import ast
from copy import deepcopy
import json
from pathlib import Path
import sys


def normalized_intent(intent):
    """合并同义意图，避免把枚举名称差异误当作业务覆盖。"""
    if intent in {"ANSWER_QUESTION_FROM_FILES", "ANSWER_DOCUMENTS", "ANSWER_FROM_FILES", "ANSWER_MANAGED_FILE",
                  "EVIDENCE_DISCOVERY", "EVIDENCE_ANSWER"}:
        return "CONTENT_ANSWER"
    if intent in {"SEARCH_FILES", "LIST_MANAGED_FILES"}:
        return "FILE_LIST"
    return "OTHER" if intent else "UNKNOWN"


class PlanningTrace:
    """观察离线规划；默认仅脱敏摘要，显式capture才保存私有响应审计。"""

    def __init__(self, client, *, capture=False):
        """包装原客户端，真实请求预算仍由原客户端负责。"""
        self.client = client
        self.calls = []
        self.rules = []
        self.capture, self.responses, self.validations = capture, [], []

    def complete_json(self, **kwargs):
        """公共诊断只保留枚举；capture响应仅供私有产物，异常正文始终不保存。"""
        detail = None
        if self.capture:
            from app.modules.evidence_evaluation.frozen_inputs import digest
            from app.modules.agent.adaptive_planner import ADAPTIVE_PLANNER_SYSTEM_PROMPT
            phase = "ADAPTIVE" if kwargs["system_prompt"] == ADAPTIVE_PLANNER_SYSTEM_PROMPT else "LEGACY"
            detail = {"phase": phase, "request_ref": digest([kwargs["system_prompt"], kwargs["user_payload"]]),
                      "status": "FAILED", "raw": None, "diagnostic": None, "schema_valid": False}
            self.responses.append(detail)
        try:
            result = self.client.complete_json(**kwargs)
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostic", None)
            if detail is not None:
                detail["diagnostic"] = diagnostic.model_dump(mode="json") if diagnostic else None
            self.calls.append({"status": "FAILED", "intent": None,
                               "error_category": diagnostic.category if diagnostic else "UNKNOWN"})
            raise
        if detail is not None:
            from app.modules.agent.planner_contracts import PlannerDecision
            from app.modules.llm.schemas import UserIntentPlan
            if len(json.dumps(result, ensure_ascii=False)) > 400000:
                raise ValueError("PLANNING_RESPONSE_TOO_LARGE")
            detail.update(status="COMPLETED", raw=deepcopy(result))
            try:
                (PlannerDecision if detail["phase"] == "ADAPTIVE" else UserIntentPlan).model_validate(result)
                detail["schema_valid"] = True
            except Exception:
                pass  # schema失败仍须交给原规划服务处理，审计不改变回退行为。
        intent = result.get("intent") if isinstance(result, dict) else None
        # 模型任意字符串不成为可外发诊断，只有契约内枚举才保留。
        allowed = {"ANSWER_QUESTION_FROM_FILES", "EVIDENCE_DISCOVERY", "EVIDENCE_ANSWER",
                   "SEARCH_FILES", "LIST_MANAGED_FILES"}
        self.calls.append({"status": "COMPLETED", "intent": intent if intent in allowed else None,
                           "error_category": None})
        return result

    def observe(self, frame, event, arg):
        """记录实际进入的前置搜索分支；源码位置由AST确定，不猜测调用原因。"""
        if self.capture and event == "return" and frame.f_code.co_name in {"validate_and_convert_decision", "_run_legacy_llm_planner"}:
            from app.modules.agent import graph, adaptive_planner
            valid = (frame.f_code is adaptive_planner.validate_and_convert_decision.__code__
                     or frame.f_code is graph._run_legacy_llm_planner.__code__)
            if valid:
                self.validations.append({"phase": "ADAPTIVE" if frame.f_code.co_name == "validate_and_convert_decision" else "LEGACY",
                    "response_sequence": len(self.responses), "status": "ACCEPTED" if arg is not None else "REJECTED"})
        if event != "call" or frame.f_code.co_name != "_file_search_plan":
            return
        parent = frame.f_back
        if not parent or parent.f_code.co_name != "build_plan_from_user_intent":
            return
        from app.modules.agent import planner
        path = Path(planner.__file__).resolve()
        if Path(parent.f_code.co_filename).resolve() != path:
            return
        tree = ast.parse(path.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                        and n.name == "build_plan_from_user_intent")
        for node in function.body:
            if (isinstance(node, ast.If) and isinstance(node.test, ast.BoolOp)
                and isinstance(node.test.op, ast.And)
                and "_has_file_search_intent" in ast.unparse(node.test.values[0])):
                if node.lineno <= parent.f_lineno <= node.end_lineno:
                    self.rules.append("LEGACY_FILE_SEARCH_PREEMPTION")

    def __enter__(self):
        """保留调用线程原有追踪器，结束后恢复。"""
        self.previous = sys.getprofile()
        sys.setprofile(self.observe)
        return self

    def __exit__(self, *args):
        """诊断不能把追踪器留在其他任务中。"""
        sys.setprofile(self.previous)

    def summary(self, final_intent, user_intent):
        """明确模型输出、最终分支和降级；未知状态不等于成功。"""
        intent = (user_intent or {}).get("intent") or (user_intent or {}).get("original_intent")
        model_route = normalized_intent(intent)
        return {"schema_version": "routing-trace-v1", "calls": self.calls,
                "model_route": model_route, "final_route": normalized_intent(final_intent),
                "fallback": bool(self.calls and not intent),
                "rule_ids": sorted(set(self.rules))}

    def audit(self, requested_mode, user_intent):
        """保存私有模型结构与真实接受轨迹，回退成功不能冒充Adaptive独立成功。"""
        user_intent = user_intent or {}
        reason = user_intent.get("fallback_reason")
        if user_intent.get("source") == "adaptive_planner":
            effective = "ADAPTIVE"
        elif reason == "LLM_INTENT_FAILED":
            effective = "RULE"
        elif any(v["phase"] == "LEGACY" and v["status"] == "ACCEPTED" for v in self.validations):
            effective = "LEGACY"
        else:
            effective = "PREFLIGHT"
        fallback = ("ADAPTIVE_TO_" + effective if requested_mode == "enabled" and effective in {"LEGACY", "RULE"}
                    else "LEGACY_TO_RULE" if requested_mode == "legacy" and effective == "RULE" else "NONE")
        return {"schema_version": "planning-audit-v1", "requested_mode": requested_mode,
                "effective_mode": effective, "fallback_kind": fallback,
                # legacy由模型判断意图、程序组装步骤，不能冒充LLM生成完整计划。
                "model_generated_plan": effective == "ADAPTIVE",
                "model_generated_intent": effective in {"ADAPTIVE", "LEGACY"},
                "responses": self.responses, "validation_events": self.validations}
