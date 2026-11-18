"""离线规划反馈的事实投影：分开任务目标、首步动作和应用意图契约，不推断Gold。"""

from app.modules.evidence_harness.routing.diagnostics import normalized_intent


def application_intent_contract():
    """列出既有路由映射的受支持别名，只用于解释契约，不改变线上规划或评分。"""
    content = ["ANSWER_QUESTION_FROM_FILES", "ANSWER_DOCUMENTS", "ANSWER_FROM_FILES", "ANSWER_MANAGED_FILE",
               "EVIDENCE_DISCOVERY", "EVIDENCE_ANSWER"]
    files = ["SEARCH_FILES", "LIST_MANAGED_FILES"]
    if any(normalized_intent(i) != "CONTENT_ANSWER" for i in content) or any(normalized_intent(i) != "FILE_LIST" for i in files):
        raise ValueError("PLANNING_FEEDBACK_CONTRACT_CHANGED")
    return {"content_intents": content, "file_intents": files,
            "goal_action_boundary": "intent保留最终业务目标；hybrid-search仅表示本轮发现文件，正文任务和文件清单都可以先发现文件。",
            "schema_boundary": "PlannerDecision.intent是字符串；schema通过不代表它已映射到应用支持的任务意图。",
            "evidence_boundary": "未执行业务Tool，不能从首轮计划断言检索或最终答案成功失败；目标语义仍需结合原始请求分析。"}


def task_diagnostic(case, result):
    """仅消费已核验case及真实结果，不读取Gold，不用关键词猜测最终任务类别。"""
    plan = result.get("plan") or {}
    steps = (plan.get("tool_plan") or {}).get("steps") or []
    first_tool = steps[0].get("tool_name") if steps else None
    responses = (result.get("planning_audit") or {}).get("responses") or []
    adaptive = [r for r in responses if r.get("phase") == "ADAPTIVE" and r.get("status") == "COMPLETED"]
    response = adaptive[-1] if adaptive else {}
    raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
    model_intent, backend_intent = raw.get("intent"), plan.get("intent")
    model_mapping, backend_mapping = normalized_intent(model_intent), normalized_intent(backend_intent)
    supported = {"CONTENT_ANSWER", "FILE_LIST"}
    recognized = model_mapping in supported and backend_mapping in supported
    discovery = first_tool in {"hybrid-search", "managed-file-list"}
    issues = []
    # 只证明字符串无法映射，不能据此给原始目标贴标签或宣称真实检索失败。
    if discovery and response.get("schema_valid") is True and not recognized:
        issues.append({"code": "UNRECOGNIZED_TASK_INTENT_FOR_DISCOVERY", "certainty": "OBSERVED",
                       "source": "ACCEPTED_PLAN_AND_APPLICATION_INTENT_MAPPING"})
    return {"schema_version": "planning-task-diagnostic-v1",
        "final_task_goal": {"original_request": case.question, "model_declared_goal": raw.get("user_goal"),
            "source": "ORIGINAL_REQUEST_AND_MODEL_DECLARATION", "semantic_alignment": "NOT_INDEPENDENTLY_VERIFIED"},
        "current_tool_action": {"tool_name": first_tool,
            "purpose": {"hybrid-search": "FILE_DISCOVERY", "managed-file-list": "FILE_LISTING", "evidence-answer": "EVIDENCE_ANSWER"}.get(first_tool, "OTHER" if first_tool else "NO_TOOL_STEP"),
            "status": "PLANNED_NOT_EXECUTED" if first_tool else "NOT_PLANNED",
            "determines_final_task_goal": False},
        "intent_contract": {"model_intent": model_intent, "backend_intent": backend_intent,
            "planner_schema_valid": response.get("schema_valid"), "model_application_mapping": model_mapping,
            "backend_application_mapping": backend_mapping,
            "mapping_status": "RECOGNIZED" if recognized else "UNRECOGNIZED" if discovery else "NOT_APPLICABLE",
            "observed_initial_route": result.get("route"), "source": "EXISTING_INITIAL_ROUTE_CLASSIFIER"},
        "mismatch_observations": issues,
        "unverified": {"goal_intent_semantic_alignment": True, "retrieval_effectiveness": "NOT_EXECUTED",
                       "answer_quality": "NOT_EXECUTED", "causal_explanation": "NOT_VERIFIED"}}


def feedback_summary(packet):
    """默认检查只显示版本与观察计数，不把用户请求或模型返回打印到普通终端。"""
    diagnostics = [i["task_diagnostic"] for i in packet["items"] if "task_diagnostic" in i]
    return {"schema_version": packet["schema_version"], "task_diagnostic_count": len(diagnostics),
            "intent_contract_issue_count": sum(bool(d["mismatch_observations"]) for d in diagnostics)}
