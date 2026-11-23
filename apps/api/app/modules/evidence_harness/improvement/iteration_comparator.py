"""自主迭代的独立裁判：从原始响应重放各轮，重建反馈，不采信候选或缓存分数。"""

from collections import Counter

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import adaptive_validation as validation
from app.modules.evidence_harness.improvement import codex_backend
from app.modules.evidence_harness.improvement.isolation import file_manifest, trusted_code_ref, compatible_trusted_refs
from app.modules.evidence_harness.improvement.patches import source_hash, rejection_code
from app.modules.evidence_harness.improvement.planning_feedback import task_diagnostic
from app.modules.evidence_harness.improvement.planning_diagnosis import feedback_packet, tool_contracts
from app.modules.evidence_harness.improvement import iteration_proposals as proposals
from app.modules.evidence_harness.improvement import advisor_memory
from app.modules.evidence_harness.executor import ExperimentStopped

# 只读兼容已核验的v1，按旧提示和长哈希协议重建，不把旧拒绝改判为成功。
KNOWN_ITERATION_V1_REF = "80c2ac2775e69a65dcdaa04065513ed09ee13a14a52034d33f65d18f2a79bfb6"
# 实验管理接线不改变已完成v2的提示、短引用和评分，旧记录仍按原协议逐次重放。
KNOWN_ITERATION_V2_REF = "804b581ace79c354ffe66e03f8376db6af6631e13db9d1f8449fab1c771838b1"
KNOWN_CAMPAIGN_V1_REF = "0977a1a07e5329b781b6f3245af358d9068038feb797398cff4330e957edbb39"
# 旧v2在第三轮因历史过长停止，仍须按原完整输入重建，不能用新摘要替它改判。
KNOWN_CAMPAIGN_V2_REF = "6e91f9175a389b309ab5b54a8950168ad56f508ab720fb2bb04e96fdcde15f25"


def score_round(cases, gold, records, calls, catalog, original_prompt, candidate_prompt, repetitions, *, checks_ok):
    """所有预定开发试次计入分母；原始响应必须经真实规划节点和权限检查重放一致。"""
    order = validation.schedule(cases, repetitions)
    positions = {(s["arm"], s["case_id"], s["repetition"]) for s in order}
    records_by_slot = {}
    for record in records:
        key = record["arm"], record["case_id"], record["repetition"]
        if key not in positions or key in records_by_slot:
            raise ValueError("ITERATION_TRIAL_SLOT_INVALID")
        records_by_slot[key] = record
    labels, rows = {a["case_id"]: a for a in gold}, []
    for case in cases:
        label = labels[case.case_id]
        for n in range(1, repetitions + 1):
            for arm in ("control", "candidate"):
                record = records_by_slot.get((arm, case.case_id, n))
                result = record["result"] if record else {"status": "NOT_RUN"}
                selected = [c for c in calls if (c["arm"], c["case_id"], c["repetition"]) == (arm, case.case_id, n)]
                if record:
                    if (record["case_input_ref"] != digest(case.model_dump(mode="json"))
                        or type(record["duration_ms"]) is not int or record["duration_ms"] < 0 or result["status"] != "COMPLETED"):
                        raise ValueError("ITERATION_TRIAL_INPUT_INVALID")
                    policy.replay_trial(case, result, selected, catalog, candidate_prompt if arm == "candidate" else original_prompt)
                predicted = result.get("route") if result.get("scope_valid") else None
                decision = (result.get("plan") or {}).get("decision_type")
                route_correct = result["status"] == "COMPLETED" and predicted == label["expected_route"]
                type_correct = result["status"] == "COMPLETED" and (label["expected_decision_type"] is None or decision == label["expected_decision_type"])
                audit = result.get("planning_audit") or {}
                diagnostic = task_diagnostic(case, result)
                rows.append({"arm": arm, "case_id": case.case_id, "repetition": n, "status": result["status"],
                    "expected_route": label["expected_route"], "predicted_route": predicted,
                    "expected_decision_type": label["expected_decision_type"], "predicted_decision_type": decision,
                    "route_correct": bool(route_correct), "decision_type_correct": bool(type_correct), "correct": bool(route_correct and type_correct),
                    "strategy_evaluable": result.get("strategy_evaluable", False), "effective_mode": audit.get("effective_mode", "UNAVAILABLE"),
                    "fallback_kind": audit.get("fallback_kind", "UNKNOWN"),
                    "contract_mapping_status": diagnostic["intent_contract"]["mapping_status"], "violation_codes": result.get("violation_codes", []),
                    "duration_ms": record["duration_ms"] if record else None,
                    "input_tokens": sum(c.get("input_tokens") or 0 for c in selected), "output_tokens": sum(c.get("output_tokens") or 0 for c in selected),
                    "unknown_token_calls": sum(c.get("input_tokens") is None or c.get("output_tokens") is None for c in selected)})
    arms = {arm: validation.aggregate([r for r in rows if r["arm"] == arm]) for arm in ("control", "candidate")}
    pairs = [(a, b) for a, b in zip(arms["control"]["rows"], arms["candidate"]["rows"])
             if a["strategy_evaluable"] and b["strategy_evaluable"]]
    regressions = sorted({a["case_id"] for a, b in pairs if a["correct"] and not b["correct"]})
    contracts = sorted({a["case_id"] for a, b in pairs if a["contract_mapping_status"] == "RECOGNIZED" and b["contract_mapping_status"] == "UNRECOGNIZED"})
    safety = sum(bool(r["violation_codes"]) and r["effective_mode"] == "ADAPTIVE" for r in arms["candidate"]["rows"])
    fully = checks_ok and all(r["strategy_evaluable"] for r in rows)
    decision = ("REGRESSION" if safety or regressions or contracts else "INCONCLUSIVE" if not fully else
                "IMPROVED" if arms["candidate"]["correct"] > arms["control"]["correct"] else "NO_GAIN")
    return {"decision": decision, "arms": arms, "rows": rows, "regression_case_ids": regressions,
        "contract_regression_case_ids": contracts, "candidate_safety_failures": safety, "strategy_evaluable": fully,
        "non_adaptive_case_ids": sorted({r["case_id"] for r in rows if not r["strategy_evaluable"]}),
        "evaluation_scope": "DEVELOPMENT_ONLY", "holdout_evaluated": False}


def _calls(output, meta, auth, cases):
    """全局先记账记录逐次核验，轮次不重置额度；允许中断后的未知费用但不能当作零。"""
    calls, known, unknown = [], Counter(), 0
    positions = {(s["arm"], s["case_id"], s["repetition"]): i
                 for i, s in enumerate(validation.schedule(cases, auth.repetitions))}
    round_roles, slot_counts, observed = {}, Counter(), []
    starts = sorted((output / "calls").glob("*.started.json"))
    finish_names = {p.name for p in (output / "calls").glob("*.finished.json")}
    if not finish_names <= {p.name.replace("started", "finished") for p in starts}:
        raise ValueError("ITERATION_ORPHAN_CALL")
    previous_ms = -1
    for number, path in enumerate(starts, 1):
        start = store._read(path)[0]
        role, rid = start["role"], start["round_id"]
        if (start["sequence"] != number or path.name != f"{number:04d}.started.json"
            or type(rid) is not int or not 1 <= rid <= auth.max_rounds or role not in {"ADVISOR", "PLANNER"}
            or start["timeout_seconds"] != 60 or start.get("profile_ref") != digest(meta["profile"])
            or start["max_output_tokens"] != (auth.advisor_max_output_tokens if role == "ADVISOR" else auth.planner_max_output_tokens)
            or type(start["active_ms_before"]) is not int
            or not previous_ms <= start["active_ms_before"] <= auth.max_active_seconds * 1000 - 62000):
            raise ValueError("ITERATION_CALL_BINDING_INVALID")
        previous_ms = start["active_ms_before"]
        roles = round_roles.setdefault(rid, [])
        if role == "ADVISOR":
            if roles or any(start[k] is not None for k in ("arm", "case_id", "repetition")):
                raise ValueError("ITERATION_ADVISOR_SEQUENCE_INVALID")
            position = -1
        else:
            slot = start["arm"], start["case_id"], start["repetition"]
            if not roles or roles[0] != "ADVISOR" or slot not in positions:
                raise ValueError("ITERATION_PLANNER_SLOT_INVALID")
            position = positions[slot]
            slot_counts[(rid, *slot)] += 1
        roles.append(role)
        observed.append((rid, position))
        finish_path = path.with_name(path.name.replace("started", "finished"))
        finish = store._read(finish_path)[0] if finish_path.exists() else {**start, "status": "UNKNOWN"}
        if any(finish.get(k) != v for k, v in start.items()) or finish["status"] not in {"COMPLETED", "FAILED", "UNKNOWN"}:
            raise ValueError("ITERATION_CALL_FINISH_INVALID")
        if "duration_ms" in finish and (type(finish["duration_ms"]) is not int or finish["duration_ms"] < 0):
            raise ValueError("ITERATION_CALL_DURATION_INVALID")
        usage = [finish.get(k) for k in ("input_tokens", "output_tokens")]
        unknown += any(v is None for v in usage)
        for key, value in zip(("input_tokens", "output_tokens"), usage):
            if value is not None:
                if type(value) is not int or value < 0:
                    raise ValueError("ITERATION_TOKEN_USAGE_INVALID")
                known[key] += value
        calls.append(finish)
    if len(calls) > auth.max_total_requests or any(v > 3 for v in slot_counts.values()) or observed != sorted(observed):
        raise ValueError("ITERATION_GLOBAL_BUDGET_OR_ORDER_INVALID")
    if calls:
        probes = store._read(output / "sandbox-checks.json")[0]
        if any(probes.get(k) is not True for k in ("compile", "read_blocked", "write_blocked", "network_blocked", "exec_blocked")):
            raise ValueError("ITERATION_SANDBOX_UNVERIFIED")
    return calls, dict(known), unknown


def _profile(meta, origin, auth):
    """只用脱敏快照重建Provider能力，不重新登录、查env或请求模型。"""
    profile = meta["profile"]
    if meta["provider"] == "codex-login":
        expected = codex_backend.profile({"model": "replay-only", "version": profile["codex_version"],
            "binary_ref": profile["codex_binary_ref"], "transport": "https" if profile.get("transport") == "HTTPS_SSE" else "auto",
            **({"isolation": profile["isolation"]} if "isolation" in profile else {})},
            auth.planner_max_output_tokens)
        expected["model_ref"] = profile["model_ref"]
    elif meta["provider"] == "openai-compatible":
        expected = {"model_ref": profile["model_ref"], "endpoint_ref": profile["endpoint_ref"],
            "planner_max_output_tokens": auth.planner_max_output_tokens, "timeout_seconds": 60,
            "max_retries": 0, "generation_settings": "EXISTING_PROVIDER_DEFAULTS"}
    else:
        raise ValueError("ITERATION_PROVIDER_INVALID")
    details = validation._profile_details(origin["profile"], profile, meta["allow_model_profile_change"])
    if profile != expected or any(meta.get(k) != v for k, v in details.items()):
        raise ValueError("ITERATION_PROFILE_CHANGED")


def _compare(output):
    from app.modules.evidence_harness.improvement import adaptive_iteration as iteration
    from app.modules.evidence_harness.improvement import campaign

    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", "automatically_adopted": False}
    actual = file_manifest(output)
    for name in ("integrity.json", "comparison.json", "summary.md"):
        actual.pop(name, None)
    if actual != store._read(output / "integrity.json")[0]:
        raise ValueError("ITERATION_ARTIFACT_CHANGED")
    meta = store._read(output / "experiment.json")[0]
    legacy = meta["schema_version"] == "adaptive-iteration-experiment-v1" and meta["trusted_code_ref"] == KNOWN_ITERATION_V1_REF
    old_campaign = meta["schema_version"] == campaign.LEGACY_VERSION and meta["trusted_code_ref"] in {*compatible_trusted_refs(), KNOWN_CAMPAIGN_V1_REF}
    bounded = meta["schema_version"] == campaign.VERSION and meta["trusted_code_ref"] in {*compatible_trusted_refs(), campaign.PRE_LOOP_CONTROLLER_REF}
    extended = bounded or meta["schema_version"] == campaign.EXPLORATION_V2 and meta["trusted_code_ref"] in {*compatible_trusted_refs(), KNOWN_CAMPAIGN_V2_REF}
    campaign_mode = old_campaign or extended
    current = campaign_mode or meta["schema_version"] == "adaptive-iteration-experiment-v2" and meta["trusted_code_ref"] in {*compatible_trusted_refs(), KNOWN_ITERATION_V2_REF}
    if (not (legacy or current)
        or meta["holdout_usage"] != "EXCLUDED_FROM_ITERATION" or meta["evaluation_level"] != "INITIAL_ROUTING"):
        raise ValueError("ITERATION_PROTOCOL_INVALID")
    if bounded and meta.get("advisor_memory_policy") != advisor_memory.POLICY:
        raise ValueError("ADVISOR_MEMORY_POLICY_CHANGED")
    origin = campaign.load_origin(store.TEST_ROOT / meta["source_experiment"])
    imported = None
    if campaign_mode:
        _, sources = campaign.resolve_source(store.TEST_ROOT / meta["source_experiment"], [store.TEST_ROOT / h["path"] for h in meta["history_sources"]])
        imported = campaign.import_history(origin, sources)
        if imported["history_sources"] != meta["history_sources"] or meta["selection"] != campaign.SELECTION:
            raise ValueError("CAMPAIGN_HISTORY_BINDING_CHANGED")
        origin.update(imported)
    auth_class = campaign.CampaignAuthorization if extended else campaign.LegacyCampaignAuthorization if old_campaign else iteration.IterationAuthorization
    auth = auth_class.model_validate(meta["authorization"])
    if (meta["source_ref"] != origin["source_ref"] or meta["source"] != origin["metadata"]["source"]
        or meta["development_inputs_ref"] != digest(origin["inputs"]) or meta["development_gold_ref"] != digest(origin["gold"])
        or store._read(output / "development-inputs.json")[0] != origin["inputs"]
        or store._read(output / "development-gold.json")[0] != {"labels": origin["gold"]}
        or store._read(output / "initial-feedback.json")[0] != origin["feedback"]
        or meta["baseline_manifest"] != origin["metadata"]["baseline_manifest"]
        or file_manifest(output / "baseline") != meta["baseline_manifest"]):
        raise ValueError("ITERATION_SOURCE_OR_DEVELOPMENT_CHANGED")
    _profile(meta, origin["metadata"], auth)
    calls, known, unknown = _calls(output, meta, auth, origin["cases"])
    summary = store._read(output / "run-summary.json")[0]
    if (summary["requests_used"] != len(calls) or summary["model_calls"] != (len(calls) if meta["source"] == "live" else 0)
        or summary["stage"] not in {"COMPLETED", "FAILED", "STOPPED", "INTERRUPTED"}
        or summary["automatically_adopted"] is not False or type(summary["active_duration_ms"]) is not int
        or summary["active_duration_ms"] < 0
        or summary["stage"] == "COMPLETED" and summary["active_duration_ms"] > auth.max_active_seconds * 1000):
        raise ValueError("ITERATION_SUMMARY_INVALID")
    source = (output / "baseline" / policy.TARGET).read_text()
    context = policy.policy_context(source)
    context["tool_input_schemas"] = tool_contracts()
    roots = sorted((output / "rounds").glob("*"))
    if len(roots) != summary["rounds_started"] or len(roots) > auth.max_rounds:
        raise ValueError("ITERATION_ROUND_COUNT_INVALID")
    packet, history, seen = origin["feedback"], origin["history"], set(origin.get("seen_policies", []))
    observations = list(origin.get("historical_candidates", []))
    reports, terminal_reason, no_progress = [], None, 0
    for number, root in enumerate(roots, 1):
        if root.name != f"{number:04d}" or terminal_reason is not None:
            raise ValueError("ITERATION_ROUND_SEQUENCE_INVALID")
        if store._read(root / "feedback.json")[0] != packet or store._read(root / "history.json")[0] != {"attempts": history}:
            raise ValueError("ITERATION_FEEDBACK_CONNECTION_INVALID")
        selected = [c for c in calls if c["round_id"] == number]
        advisors = [c for c in selected if c["role"] == "ADVISOR"]
        planners = [c for c in selected if c["role"] == "PLANNER"]
        prompt = iteration.LEGACY_ADVISOR_PROMPT if legacy else campaign.advisor_prompt(meta) if campaign_mode else iteration.ADVISOR_PROMPT
        if current and store._read(root / "reference-map.json")[0] != proposals.reference_map(packet, number, source, extended=extended):
            raise ValueError("ITERATION_REFERENCE_MAP_CHANGED")
        try:
            payload = iteration.advisor_payload(packet, context, history,
                version="v4" if bounded else "v1" if legacy else "v3" if extended else "v2", round_id=number, prompt=prompt)
        except ExperimentStopped as exc:
            # 必需上下文或汇总自身超限时不发出请求，独立重建须确认同一停止，而非篡改成成功。
            if (not bounded or selected or number != len(roots) or summary["stage"] != "STOPPED"
                or summary["stop_reason"] != str(exc) or any((root / name).exists()
                    for name in ("advisor-memory.json", "proposal.json", "validation.json", "trials", "candidate"))):
                raise ValueError("ADVISOR_MEMORY_STOP_UNVERIFIED") from None
            terminal_reason = str(exc)
            reports.append({"round_id": number, "proposal_stage": "NOT_RUN", "requests_used": 0,
                "advisor_calls": 0, "score": None, "policy_ref": None})
            continue
        if bounded:
            if (root / "advisor-memory.json").exists():
                if store._read(root / "advisor-memory.json")[0] != advisor_memory.audit(prompt, payload):
                    raise ValueError("ADVISOR_MEMORY_CHANGED")
            elif advisors or summary["stage"] == "COMPLETED":
                raise ValueError("ADVISOR_MEMORY_MISSING")
        if len(advisors) > 1 or advisors and advisors[0]["request_ref"] != digest([prompt, payload]):
            raise ValueError("ITERATION_ADVISOR_PAYLOAD_CHANGED")
        score, candidate, expected = None, None, {"stage": "NOT_RUN"}
        if (root / "proposal.json").exists():
            if len(advisors) != 1 or advisors[0]["status"] != "COMPLETED":
                raise ValueError("ITERATION_PROPOSAL_CALL_MISSING")
            try:
                raw = store._read(root / "proposal.json")[0]
                candidate = (policy.materialize(source, raw, packet, origin["cases"]) if legacy else
                    proposals.materialize(source, raw, packet, origin["cases"], number, extended=extended))
            except Exception as exc:
                expected = {"stage": "REJECTED", "reason_code": rejection_code(exc)} if legacy else proposals.rejection(exc)
                terminal_reason = expected["reason_code"]
            else:
                if current and store._read(root / "resolved-proposal.json")[0] != candidate["resolved_proposal"]:
                    raise ValueError("ITERATION_RESOLVED_PROPOSAL_CHANGED")
                if candidate["stage"] == "NO_CHANGE":
                    expected = {"stage": "NO_CHANGE", "reason_code": "MODEL_SELECTED_NO_CHANGE"}
                    terminal_reason = expected["reason_code"]
                elif candidate["prompt_ref"] in seen:
                    expected = {"stage": "REPEATED", "reason_code": "REPEATED_CANDIDATE"}
                    terminal_reason = expected["reason_code"]
                else:
                    expected = {"stage": "VALIDATED", "reason_code": None}
                    seen.add(candidate["prompt_ref"])
            if store._read(root / "validation.json")[0] != expected:
                raise ValueError("ITERATION_CANDIDATE_VALIDATION_CHANGED")
            if expected["stage"] == "VALIDATED":
                if ((root / "candidate.patch").read_text() != candidate["patch"]
                    or store._read(root / "policy.json")[0] != {k: candidate[k] for k in ("prompt_ref", "patch_ref")}):
                    raise ValueError("ITERATION_CANDIDATE_IDENTITY_CHANGED")
                for arm in ("baseline", "control", "candidate"):
                    manifest = {**meta["baseline_manifest"], **({policy.TARGET: source_hash(candidate["source"])} if arm == "candidate" else {})}
                    if file_manifest(root / arm) != manifest:
                        raise ValueError("ITERATION_EXECUTED_SOURCE_CHANGED")
                if campaign_mode:
                    campaign.verify_frozen(root, campaign.freeze(meta, candidate, number, origin["cases"]))
                checks_ok = (root / "checks.json").exists() and store._read(root / "checks.json")[0] == policy.expected_checks()
                if planners and not checks_ok:
                    raise ValueError("ITERATION_PROTECTED_CHECK_UNVERIFIED")
                records = []
                for path in sorted((root / "trials").glob("*.json")):
                    record = store._read(path)[0]
                    if path.name != f"{record['arm']}-{record['case_id']}-{record['repetition']}.json":
                        raise ValueError("ITERATION_TRIAL_FILENAME_CHANGED")
                    records.append(record)
                score = score_round(origin["cases"], origin["gold"], records, planners, origin["inputs"]["catalog"],
                    context["original_prompt"], candidate["prompt"], auth.repetitions, checks_ok=checks_ok)
                if summary["stage"] == "COMPLETED" and len(records) != 2 * len(origin["cases"]) * auth.repetitions:
                    raise ValueError("ITERATION_COMPLETED_TRIALS_MISSING")
                if (root / "score.json").exists():
                    if campaign_mode:
                        # 封存应在下一轮建议前完成；试次和评分全部从原响应重建。
                        ordered = {(r["arm"], r["case_id"], r["repetition"]): r for r in records}
                        slots = campaign.freeze(meta, candidate, number, origin["cases"])["schedule"]
                        seal = {"freeze_ref": digest(campaign.freeze(meta, candidate, number, origin["cases"])), "score_ref": digest(score),
                            "trial_refs": [digest(ordered[(s["arm"], s["case_id"], s["repetition"])]) for s in slots],
                            "requests_used_at_seal": max(c["sequence"] for c in calls if c["round_id"] <= number)}
                        if store._read(root / "round-sealed.json")[0] != seal:
                            raise ValueError("CAMPAIGN_ROUND_SEAL_CHANGED")
                        observations.append(campaign.observation(f"C{number:03d}", output, number, meta, score, candidate["prompt_ref"]))
                    # 即使缓存被重封装，下一轮输入和结论仍只使用独立重算结果。
                    packet = feedback_packet(origin["cases"], records, score["rows"], planners)
                    entry = (campaign.history_entry(number, candidate, score, packet, profile=meta["profile"]) if campaign_mode else
                        iteration.history_entry(number, candidate, score))
                    history = [*history, entry]
                    no_progress = 0 if campaign_mode and score["decision"] == "IMPROVED" else no_progress + (score["decision"] != "IMPROVED")
                    if score["decision"] == "IMPROVED":
                        if not campaign_mode:
                            terminal_reason = "DEVELOPMENT_GAIN_VERIFIED"
                    elif no_progress >= auth.max_no_progress:
                        terminal_reason = "NO_PROGRESS_LIMIT"
            elif planners or (root / "trials").exists() or (root / "candidate").exists():
                raise ValueError("ITERATION_REJECTED_CANDIDATE_EXECUTED")
        elif planners or summary["stage"] == "COMPLETED":
            raise ValueError("ITERATION_ADVISOR_RESULT_MISSING")
        # 控制台展示各轮汇总，逐题原始重算结果保存在私有score.json，避免重复打印全部行。
        public_score = ({**{k: v for k, v in score.items() if k not in {"rows", "arms"}},
            "arms": {arm: {k: v for k, v in values.items() if k != "rows"} for arm, values in score["arms"].items()}}
            if score else None)
        reports.append({"round_id": number, "proposal_stage": expected["stage"] if candidate or (root / "validation.json").exists() else "NOT_RUN",
            "requests_used": len(selected), "advisor_calls": len(advisors), "score": public_score,
            "policy_ref": candidate.get("prompt_ref") if candidate else None,
            **({"proposal_diagnostic": expected["diagnostic"]} if current and "diagnostic" in expected else {})})
    if any(c["round_id"] > len(roots) for c in calls):
        raise ValueError("ITERATION_CALL_ROUND_MISSING")
    if summary["stage"] == "COMPLETED" and summary["stop_reason"] != (terminal_reason or "MAX_ROUNDS_REACHED"):
        raise ValueError("ITERATION_STOP_REASON_CHANGED")
    if summary["stage"] == "COMPLETED" and terminal_reason is None and len(roots) != auth.max_rounds:
        raise ValueError("ITERATION_STOPPED_EARLY_WITHOUT_REASON")
    retained = summary["stage"] == "COMPLETED" and terminal_reason == "DEVELOPMENT_GAIN_VERIFIED"
    decision = "DEVELOPMENT_IMPROVED" if retained else (reports[-1]["score"]["decision"] if reports and reports[-1]["score"] else "NOT_RUN")
    if not retained and decision == "IMPROVED":
        decision = "INCONCLUSIVE"  # 总时限或中断仍有优先级，局部得分不能越过全局停止验收。
    result = {"decision": decision, "stage": summary["stage"], "stop_reason": summary["stop_reason"], "rounds": reports,
        "requests_used": len(calls), "model_calls": summary["model_calls"], "advisor_calls": sum(c["role"] == "ADVISOR" for c in calls),
        "failed_model_calls": sum(c["status"] != "COMPLETED" for c in calls), "known_tokens": known,
        "unknown_token_calls": unknown, "token_usage_complete": unknown == 0, "active_duration_ms": summary["active_duration_ms"],
        "provider": meta["provider"], "profile": meta["profile"], "authorization": auth.model_dump(),
        "candidate_retained": retained, "retained_round": len(roots) if retained else None,
        "evaluation_level": "INITIAL_ROUTING", "evaluation_scope": "DEVELOPMENT_ONLY", "holdout_evaluated": False,
        "target_file_retrieval": "NOT_EXECUTED", "answer_quality": "NOT_EXECUTED", "automatically_adopted": False}
    if campaign_mode:
        selection = campaign.select(observations, meta["profile"], auth.repetitions)
        # 尚未正常封存的批次只展示观察，不输出可锁定推荐。
        if summary["stage"] != "COMPLETED":
            selection.update(selected=None, selection_decision="INCONCLUSIVE")
        historical_requests = sum(h["requests_used"] for h in meta["history_sources"])
        result.update(decision=selection["selection_decision"], selection=selection,
            candidate_retained=selection["selected"] is not None, retained_round=None,
            historical_requests_used=historical_requests, lifetime_requests_used=historical_requests + len(calls),
            historical_active_duration_ms=sum(h["active_duration_ms"] for h in meta["history_sources"]))
        if extended:
            from app.modules.evidence_harness.improvement import campaign_retesting
            result["research_candidates"] = campaign_retesting.pool(observations, meta["profile"], origin=origin)
    return result


def compare(output):
    """只读核验失败即拒绝，不展开包含用户内容的异常或触发模型重试。"""
    try:
        return _compare(output)
    except Exception:
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "ITERATION_INTEGRITY_FAILED", "automatically_adopted": False}
