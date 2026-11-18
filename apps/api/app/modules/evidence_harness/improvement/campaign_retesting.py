"""有希望的候选开发复测：研究资格与推荐资格分离，锁定前K份后共享额度重复测量。"""

import time
from typing import Annotated

from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import campaign, adaptive_iteration as iteration, adaptive_policy as policy
from app.modules.evidence_harness.improvement.campaign_confirmation import ConfirmationAuthorization
from app.modules.evidence_harness.improvement.isolation import file_manifest, trusted_code_ref
from app.modules.evidence_harness.routing.runtime import _document_ids

VERSION = "campaign-retest-batch-v1"
RESEARCH_LOCK = "campaign-retest-lock-v1"
RETEST_POLICY = {"version": "development-retest-policy-v1", "positive_paired_correct_count_signal": True,
    "allow_quality_or_execution_uncertainty": True, "allow_adaptive_permission_or_scope_violation": False,
    "purpose": "DEVELOPMENT_RETEST_ONLY"}


class RetestAuthorization(ConfirmationAuthorization):
    """所有研究候选共用显式总额度；增加重复数不能被模型解释成自动续费。"""

    repetitions: int = Field(default=5, ge=2, le=10)
    top_k: int = Field(default=2, ge=1, le=3)
    excluded_policy_refs: list[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(default_factory=list, max_length=100,
        json_schema_extra={"description": "控制器明确排除已经复测的完整策略身份"})


def measured(directory, number, meta, origin):
    """从真实模型响应重建分数，禁止用初测缓存中的漂亮数字决定追加测试。"""
    from app.modules.evidence_harness.improvement.iteration_comparator import score_round
    root = directory / "rounds" / f"{number:04d}"
    retest = meta["schema_version"] == VERSION
    call_root = root if retest else directory
    calls = [store._read(p)[0] for p in sorted((call_root / "calls").glob("*.finished.json"))
             if store._read(p)[0]["round_id"] == (1 if retest else number) and store._read(p)[0]["role"] == "PLANNER"]
    records = [store._read(p)[0] for p in sorted((root / "trials").glob("*.json"))]
    candidate = campaign.round_candidate(directory, number, origin)
    score = score_round(origin["cases"], origin["gold"], records, calls, origin["inputs"]["catalog"],
        policy.policy_context((call_root / "baseline" / policy.TARGET).read_text())["original_prompt"], candidate["prompt"],
        meta["authorization"]["repetitions"], checks_ok=(root / "checks.json").exists() and store._read(root / "checks.json")[0] == policy.expected_checks())
    return score, records


def pool(observations, profile, *, origin=None):
    """依据改善迹象筛研究对象，参数/意图问题可继续观察，真正越权工具及范围错误仍拒绝。"""
    rows = []
    for item in observations:
        directory = _directory(store.TEST_ROOT / item["source"])
        meta = store._read(directory / "experiment.json")[0]
        reasons, control_correct = [], None
        if item["profile"] != profile:
            reasons.append("MODEL_PROFILE_NOT_COMPARABLE")
        else:
            current = origin or campaign.load_origin(store.TEST_ROOT / meta["source_experiment"])
            score, records = measured(directory, item["round_id"], meta, current)
            control_correct = score["arms"]["control"]["correct"]
            if item["correct"] <= 0 or item["correct"] <= control_correct:
                reasons.append("NO_POSITIVE_DEVELOPMENT_SIGNAL")
            for record in records:
                if record["arm"] != "candidate":
                    continue
                result = record["result"]
                case = next(c for c in current["cases"] if c.case_id == record["case_id"])
                explicit = {d.document_id for d in case.attachments}
                available = explicit | {d.document_id for d in case.context_documents}
                # 被拒绝后转legacy的危险建议也须检查，不能借回退的好分数取得研究资格。
                responses = (result.get("planning_audit") or {}).get("responses") or []
                for response in responses:
                    raw = response.get("raw")
                    if response.get("phase") != "ADAPTIVE" or not isinstance(raw, dict):
                        continue
                    # 被后端拒绝的未知ID仍代表越界尝试；不能借legacy回退后消失的违规取得研究资格。
                    try:
                        if not _document_ids(raw) <= available:
                            reasons.append("UNSAFE_ADAPTIVE_PLAN")
                    except ValueError:
                        # ID类型错误属于协议质量问题，仍由最终严格评分判失败，不猜测其含义。
                        pass
                    if raw.get("decision_type") != "TOOL_PLAN":
                        continue
                    raw_plan = raw.get("tool_plan")
                    raw_steps = raw_plan.get("steps") if isinstance(raw_plan, dict) else None
                    if isinstance(raw_steps, list) and any(isinstance(s, dict) and (
                        s.get("tool_name") not in {"hybrid-search", "managed-file-list", "evidence-answer"}
                        or s.get("requires_confirmation") is True) for s in raw_steps):
                        reasons.append("UNSAFE_ADAPTIVE_PLAN")
                if (result.get("planning_audit") or {}).get("effective_mode") != "ADAPTIVE":
                    continue  # 回退不是候选直接提出的计划，仍在严格分数中计失败，不冒充成功。
                plan = result.get("plan") or {}
                steps = (plan.get("tool_plan") or {}).get("steps") or []
                # 澄清/直接回复里的intent-summary是后端审计步骤，不是模型请求执行的Tool。
                forbidden = plan.get("decision_type") == "TOOL_PLAN" and any(
                    s.get("tool_name") not in {"hybrid-search", "managed-file-list", "evidence-answer"}
                    or s.get("requires_confirmation") for s in steps)
                if forbidden or set(result.get("violation_codes", [])) - {"TOOL_SCHEMA_INVALID"}:
                    reasons.append("UNSAFE_ADAPTIVE_PLAN")
        rows.append({**item, "control_correct": control_correct, "eligible_for_retest": not reasons,
            "research_only": True, "retest_exclusion_reasons": sorted(set(reasons))})
    rows.sort(key=lambda r: (-r["correct"] / r["planned"], -r["stable_correct_cases"], r["id"]))
    return {"policy": RETEST_POLICY, "ranking": rows,
        "shortlist": [r["id"] for r in rows if r["eligible_for_retest"]]}


def candidates(directory, meta, report, origin):
    """旧批次用新研究协议另行筛选，旧排名、错误和推荐结论不改写。"""
    if meta["schema_version"] not in {campaign.LEGACY_VERSION, campaign.EXPLORATION_V2, campaign.VERSION, VERSION}:
        raise ValueError("RETEST_SEARCH_SOURCE_INVALID")
    return pool(report["selection"]["ranking"], meta["profile"], origin=origin)


def research_lock(directory, meta, entry, output):
    """研究锁有独立schema，只授权固定候选的开发复测，不能借此进入保留验收。"""
    output = policy.new_output(output)
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_new_json(output / "lock.json", {"schema_version": RESEARCH_LOCK, "campaign_source": str(directory.relative_to(store.TEST_ROOT)),
        "campaign_ref": digest(store._read(directory / "integrity.json")[0]), "selected": entry,
        "profile": meta["profile"], "purpose": "DEVELOPMENT_RETEST_ONLY", "policy": RETEST_POLICY,
        "holdout_used": False, "automatically_adopted": False})
    write_new_json(output / "integrity.json", file_manifest(output))


def load_research_lock(directory, raw):
    """从原搜索重算研究资格，不信缓存映射或模型自称优胜，不把研究锁升级为推荐。"""
    source, meta, report = campaign._verified(store.TEST_ROOT / raw["campaign_source"])
    origin = campaign.load_origin(store.TEST_ROOT / meta["source_experiment"])
    ranking = candidates(source, meta, report, origin)["ranking"]
    expected = next((r for r in ranking if r["id"] == raw["selected"]["id"] and r["eligible_for_retest"]), None)
    if (expected is None or raw["selected"] != expected or raw["profile"] != meta["profile"]
        or raw["campaign_ref"] != digest(store._read(source / "integrity.json")[0]) or raw["policy"] != RETEST_POLICY
        or raw["purpose"] != "DEVELOPMENT_RETEST_ONLY" or raw["holdout_used"] is not False or raw["automatically_adopted"] is not False):
        raise ValueError("RETEST_LOCK_BINDING_INVALID")
    return {"directory": directory, "raw": raw, "metadata": meta, "origin": origin,
        "candidate": campaign.round_candidate(store.TEST_ROOT / expected["source"], expected["round_id"], origin),
        "lock_ref": digest(store._read(directory / "integrity.json")[0]), "research_only": True}


def check(*, campaign_dir, output, settings, authorization, source="live", provider="openai-compatible", codex_model=None, codex_transport=None):
    """零调用筛选前K份；同一个冻结开发集、Profile和新增预算，不自动触及保留题。"""
    from app.modules.evidence_harness.improvement import campaign_confirmation as confirmation
    directory, meta, report = campaign._verified(campaign_dir)
    if report["stage"] != "COMPLETED":
        raise ValueError("RETEST_SOURCE_NOT_COMPLETED")
    origin = campaign.load_origin(store.TEST_ROOT / meta["source_experiment"])
    ranking = candidates(directory, meta, report, origin)
    excluded = set(authorization.excluded_policy_refs)
    if any(len(ref) != 64 or any(c not in '0123456789abcdef' for c in ref) for ref in excluded):
        raise ValueError("RETEST_EXCLUDED_POLICY_REF_INVALID")
    chosen = [r for r in ranking["ranking"] if r["eligible_for_retest"] and r["policy_ref"] not in excluded][:authorization.top_k]
    if not chosen:
        raise ValueError("RETEST_NO_PROMISING_CANDIDATE")
    spec, profile = confirmation.execution_profile(settings, authorization, provider, codex_model, codex_transport)
    if profile != meta["profile"] or provider != meta["provider"] or source != meta["source"] or source == "live" and not settings.llm_enabled:
        raise ValueError("RETEST_PROFILE_OR_SOURCE_CHANGED")
    return {"directory": directory, "metadata": meta, "origin": origin, "chosen": chosen,
        "cases": origin["cases"], "profile": profile, "backend_spec": spec, "output": policy.new_output(output),
        "planned_request_upper_bound": len(chosen) * 2 * len(origin["cases"]) * authorization.repetitions * 3}


def run(*, campaign_dir, output, settings, authorization, source="live", provider="openai-compatible",
        codex_model=None, codex_transport=None, backend_factory=None):
    """先固定清单，再顺序复测，各子测试只获得批次剩余额度；原始子调用记录不改写。"""
    from app.modules.evidence_harness.improvement import campaign_confirmation as confirmation
    started = time.monotonic()
    settings = settings.model_copy(deep=True)
    checked = check(campaign_dir=campaign_dir, output=output, settings=settings, authorization=authorization, source=source,
        provider=provider, codex_model=codex_model, codex_transport=codex_transport)
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("RETEST_BACKEND_SOURCE_INVALID")
    output = checked["output"]
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    parent = checked["metadata"]
    meta = {"schema_version": VERSION, "source": source, "provider": provider,
        "source_campaign": str(checked["directory"].relative_to(store.TEST_ROOT)),
        "search_ref": digest(store._read(checked["directory"] / "integrity.json")[0]),
        **{k: parent[k] for k in ("source_experiment", "source_ref", "development_inputs_ref", "development_gold_ref", "baseline_manifest", "profile")},
        "history_sources": [{"path": str(checked["directory"].relative_to(store.TEST_ROOT))}],
        "authorization": authorization.model_dump(), "candidates": checked["chosen"], "policy": RETEST_POLICY,
        "trusted_code_ref": trusted_code_ref(), "advisor_calls": 0, "holdout_usage": "EXCLUDED_FROM_RETEST"}
    # 临时batch.json标明尚未封存，子目录由可信控制器产生；最终实验入口和完整性清单在结束时发布。
    write_new_json(output / "batch.json", meta)
    used, children, stage, reason = 0, [], "COMPLETED", "RETEST_COMPLETED"
    for number, entry in enumerate(checked["chosen"], 1):
        remaining = int(authorization.max_active_seconds - (time.monotonic() - started))
        if used >= authorization.max_total_requests or remaining < 62:
            stage, reason = "STOPPED", "REQUEST_BUDGET_EXHAUSTED" if used >= authorization.max_total_requests else "ACTIVE_TIME_BUDGET_EXHAUSTED"
            break
        lock_dir = output / "locks" / f"{number:04d}"
        research_lock(checked["directory"], parent, entry, lock_dir)
        child_auth = confirmation.ConfirmationAuthorization(repetitions=authorization.repetitions,
            max_total_requests=authorization.max_total_requests-used, max_active_seconds=remaining,
            planner_max_output_tokens=authorization.planner_max_output_tokens)
        child_dir = output / "rounds" / f"{number:04d}"
        child = confirmation.run(lock_dir=lock_dir, split="DEVELOPMENT", output=child_dir, settings=settings,
            authorization=child_auth, source=source, provider=provider, codex_model=codex_model, codex_transport=codex_transport,
            backend_factory=(lambda arm, case, n: backend_factory(number, arm, case, n)) if backend_factory else None)
        used += len(list((child_dir / "calls").glob("*.started.json")))
        children.append(number)
        if child.get("stage") != "COMPLETED":
            stage, reason = child.get("stage", "FAILED"), child.get("stop_reason", "RETEST_CHILD_INVALID")
            break
    elapsed = max(0, int((time.monotonic()-started)*1000))
    if elapsed > authorization.max_active_seconds*1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    write_new_json(output / "experiment.json", meta)
    write_new_json(output / "run-summary.json", {"stage": stage, "stop_reason": reason, "children": children,
        "requests_used": used, "active_duration_ms": elapsed, "model_calls": used if source == "live" else 0})
    write_new_json(output / "integrity.json", file_manifest(output))
    result = compare(output)
    write_new_json(output / "comparison.json", result)
    return result


def _compare(output):
    from app.modules.evidence_harness.improvement import campaign_confirmation as confirmation
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", "automatically_adopted": False}
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json"):
        manifest.pop(name, None)
    if manifest != store._read(output / "integrity.json")[0]:
        raise ValueError("RETEST_ARTIFACT_CHANGED")
    meta = store._read(output / "experiment.json")[0]
    auth = RetestAuthorization.model_validate(meta["authorization"])
    directory, parent, source_report = campaign._verified(store.TEST_ROOT / meta["source_campaign"])
    origin = campaign.load_origin(store.TEST_ROOT / parent["source_experiment"])
    chosen = [r for r in candidates(directory, parent, source_report, origin)["ranking"] if r["eligible_for_retest"] and r["policy_ref"] not in auth.excluded_policy_refs][:auth.top_k]
    if (meta["schema_version"] != VERSION or meta["trusted_code_ref"] not in {trusted_code_ref(), campaign.PRE_LOOP_CONTROLLER_REF} or meta["policy"] != RETEST_POLICY
        or meta["candidates"] != chosen or meta["search_ref"] != digest(store._read(directory / "integrity.json")[0])
        or meta != store._read(output / "batch.json")[0] or meta["advisor_calls"] != 0 or meta["holdout_usage"] != "EXCLUDED_FROM_RETEST"
        or meta["history_sources"] != [{"path": str(directory.relative_to(store.TEST_ROOT))}]
        or any(meta[k] != parent[k] for k in ("source", "provider", "source_experiment", "source_ref", "development_inputs_ref", "development_gold_ref", "baseline_manifest", "profile"))):
        raise ValueError("RETEST_BINDING_INVALID")
    if meta["trusted_code_ref"] == campaign.PRE_LOOP_CONTROLLER_REF and auth.excluded_policy_refs:
        raise ValueError("RETEST_LEGACY_EXCLUSIONS_INVALID")
    summary = store._read(output / "run-summary.json")[0]
    roots = sorted((output / "rounds").glob("*"))
    if summary["children"] != list(range(1, len(roots)+1)) or len(roots)>len(chosen):
        raise ValueError("RETEST_CHILD_SEQUENCE_INVALID")
    rows, observations, used, known, unknown = [], [], 0, {}, 0
    active = 0
    for number, root in enumerate(roots, 1):
        if root.name != f"{number:04d}":
            raise ValueError("RETEST_CHILD_PATH_INVALID")
        report = confirmation._compare(root)
        child_meta = store._read(root / "experiment.json")[0]
        locked = campaign.load_lock(store.TEST_ROOT / child_meta["lock_source"])
        child_auth = child_meta["authorization"]
        if (report["split"] != "DEVELOPMENT" or locked["raw"]["selected"] != chosen[number-1] or not locked.get("research_only")
            or child_auth["repetitions"] != auth.repetitions or child_auth["max_total_requests"] != auth.max_total_requests-used
            or child_auth["max_active_seconds"] > auth.max_active_seconds-active/1000):
            raise ValueError("RETEST_CHILD_AUTHORIZATION_CHANGED")
        used += report["requests_used"]
        active += report["active_duration_ms"]
        unknown += report["unknown_token_calls"]
        for k,v in report["known_tokens"].items():known[k]=known.get(k,0)+v
        score=report["score"]
        rows.append({"round_id":number,"proposal_stage":"VALIDATED","score":score,"policy_ref":report["policy_ref"]})
        observations.append(campaign.observation(f"V{number:03d}",output,number,meta,score,report["policy_ref"]))
    if (used != summary["requests_used"] or used>auth.max_total_requests or summary["model_calls"] != (used if meta["source"]=="live" else 0)
        or summary["stage"] not in {"COMPLETED","FAILED","INTERRUPTED","STOPPED"}
        or type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"]<active
        or summary["stage"]=="COMPLETED" and (len(roots)!=len(chosen) or summary["active_duration_ms"]>auth.max_active_seconds*1000)):
        raise ValueError("RETEST_SUMMARY_INVALID")
    selection=campaign.select(observations,meta["profile"],auth.repetitions)
    if summary["stage"]!="COMPLETED":selection.update(selected=None,selection_decision="INCONCLUSIVE")
    return {"decision":selection["selection_decision"],"stage":summary["stage"],"stop_reason":summary["stop_reason"],
        "selection":selection,"rounds":rows,"requests_used":used,"model_calls":summary["model_calls"],"advisor_calls":0,
        "known_tokens":known,"unknown_token_calls":unknown,"token_usage_complete":unknown==0,
        "active_duration_ms":summary["active_duration_ms"],"evaluation_scope":"DEVELOPMENT_RETEST_ONLY",
        "automatically_adopted":False}


def compare(output):
    """复测核验失败关闭，不改写源实验、不回显私有输入，也不追加调用。"""
    try:
        return _compare(output)
    except Exception:
        return {"decision":"INVALID_EXPERIMENT","reason_code":"RETEST_INTEGRITY_FAILED","automatically_adopted":False}
