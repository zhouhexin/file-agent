"""开发集自动闭环：统一授权探索、固定复测及反馈回流，不触碰生产策略或保留集。"""

import hashlib
import time
from collections import Counter
from pathlib import Path

from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import campaign, adaptive_validation as validation
from app.modules.evidence_harness.improvement import campaign_retesting as retest, iteration_comparator
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement.isolation import file_manifest, trusted_code_ref, compatible_trusted_refs, canonical_code

VERSION = "campaign-auto-loop-v1"


class Authorization(campaign.CampaignAuthorization):
    """一次授权涵盖全部阶段，模型不能增加循环、调用或时间。"""

    max_cycles: int = Field(default=2, ge=1, le=3)
    max_no_progress_cycles: int = Field(default=2, ge=1, le=3)
    top_k: int = Field(default=2, ge=1, le=3)
    retest_repetitions: int = Field(default=5, ge=2, le=10)
    max_active_seconds: int = Field(default=14400, ge=1, le=28800)


PRE_WINDOWS_CONTROLLER_REF = "7b69725248b5df78170b81120c8e36832ebfcd75f4eaeb8959939c518945a5f3"


def controller_ref():
    """闭环控制器在候选目录之外单独绑定源码身份，不能由模型替换裁判。"""
    return hashlib.sha256(canonical_code(Path(__file__))).hexdigest()


def seed(directory):
    """只允许原C及可核验开发历史，独立确认和保留结果不能成为优化源。"""
    directory = _directory(directory)
    meta = store._read(directory / "experiment.json")[0]
    if meta["schema_version"].startswith("adaptive-validation-experiment-"):
        origin = campaign.load_origin(directory)
        report = validation.compare(directory)
    else:
        directory, meta, report = campaign._verified(directory)
        origin = campaign.load_origin(store.TEST_ROOT / meta["source_experiment"])
    return directory, meta, report, origin


def tested(directory, meta, report):
    """仅排除已经完整复测的策略，失败或中断的半份记录不冒充完成验证。"""
    if meta["schema_version"] != retest.VERSION:
        return set()
    return {r["policy_ref"] for r in report["rounds"]
        if store._read(directory / "rounds" / f"{r['round_id']:04d}" / "run-summary.json")[0]["stage"] == "COMPLETED"}


def best_margin(report):
    """研究进展只看开发配对正确率差，不据此宣布严格验收通过。"""
    values = []
    for row in report.get("rounds", []):
        score = row.get("score") or {}
        arms = score.get("arms") or {}
        if not arms or any(arms[a]["recorded"] != arms[a]["planned"] for a in ("control", "candidate")):
            continue
        values.append(arms["candidate"]["correct"] / arms["candidate"]["planned"]
            - arms["control"]["correct"] / arms["control"]["planned"])
    return max(values, default=0.0)


def eligible(directory, excluded, origin):
    """从独立重算分数筛选未复测的研究对象，不接受模型自报的优胜名单。"""
    _, meta, report = campaign._verified(directory)
    return [r for r in retest.candidates(directory, meta, report, origin)["ranking"]
        if r["eligible_for_retest"] and r["policy_ref"] not in excluded]


def phase_auth(auth, kind, requests, seconds, excluded):
    """每个子阶段只能领取剩余额度，不能重新获得一次完整总预算。"""
    common = dict(max_total_requests=requests, max_active_seconds=seconds,
        planner_max_output_tokens=auth.planner_max_output_tokens)
    if kind == "EXPLORATION":
        return campaign.CampaignAuthorization(**common, max_rounds=auth.max_rounds, repetitions=auth.repetitions,
            max_no_progress=auth.max_no_progress, advisor_max_output_tokens=auth.advisor_max_output_tokens)
    return retest.RetestAuthorization(**common, top_k=auth.top_k, repetitions=auth.retest_repetitions,
        excluded_policy_refs=sorted(excluded))


def check(*, source_dir, output, settings, authorization, source="live", provider="openai-compatible",
          codex_model=None, codex_transport=None):
    """完整检查来源、预算与第一轮输入，不建目录，不请求模型。"""
    auth = Authorization.model_validate(authorization.model_dump())
    checked = campaign.check(source_dir=source_dir, output=output, settings=settings, source=source,
        provider=provider, codex_model=codex_model, codex_transport=codex_transport,
        allow_model_profile_change=False,
        authorization=phase_auth(auth, "EXPLORATION", auth.max_total_requests, auth.max_active_seconds, set()))
    directory, meta, report, origin = seed(source_dir)
    return {**checked, "seed": (directory, meta, report, origin), "authorization": auth,
        "excluded": tested(directory, meta, report),
        "planned_request_upper_bound": auth.max_cycles * (checked["planned_request_upper_bound"]
            + auth.top_k * 2 * len(checked["cases"]) * auth.retest_repetitions * 3)}


def ledger(output):
    """只数本任务真实已开始的调用；历史调用不重复收费，中断后的未知调用也计入。"""
    starts = sorted(output.rglob("calls/*.started.json"))
    known, unknown, failed, advisors = Counter(), 0, 0, 0
    for path in starts:
        advisors += store._read(path)[0].get("role") == "ADVISOR"
        finish = path.with_name(path.name.replace("started", "finished"))
        row = store._read(finish)[0] if finish.exists() else {}
        usage = [row.get(k) for k in ("input_tokens", "output_tokens")]
        unknown += any(type(n) is not int or n < 0 for n in usage)
        failed += row.get("status") != "COMPLETED"
        for key, n in zip(("input_tokens", "output_tokens"), usage):
            if type(n) is int and n >= 0:
                known[key] += n
    return {"requests_used": len(starts), "advisor_calls": advisors, "known_tokens": dict(known), "unknown_token_calls": unknown,
        "failed_model_calls": failed, "token_usage_complete": unknown == 0}


def run(*, backend_factory=None, **options):
    """自动在已封存阶段之间回流反馈，异常及中断停止，不重试或续费。"""
    started = time.monotonic()
    options["settings"] = options["settings"].model_copy(deep=True)
    checked = check(**options)
    auth, output = checked["authorization"], checked["output"]
    source = options.get("source", "live")
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("LOOP_BACKEND_SOURCE_INVALID")
    directory, meta, initial_report, origin = checked["seed"]
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    metadata = {"schema_version": VERSION, "source": source, "provider": options.get("provider", "openai-compatible"),
        "source_experiment": directory.relative_to(store.TEST_ROOT).as_posix(), "source_ref": digest(store._read(directory / "integrity.json")[0]),
        "profile": checked["profile"], "authorization": auth.model_dump(), "controller_ref": controller_ref(),
        "development_inputs_ref": digest(origin["inputs"]), "development_gold_ref": digest(origin["gold"]),
        "baseline_manifest": origin["metadata"]["baseline_manifest"],
        "trusted_code_ref": trusted_code_ref(), "holdout_evaluated": False, "automatically_adopted": False}
    write_new_json(output / "loop.json", metadata)
    common = {k: options[k] for k in ("settings", "source", "provider", "codex_model", "codex_transport") if k in options}
    excluded = set(checked["excluded"])
    current, best, no_progress = directory, best_margin(initial_report), 0
    stage, reason, sequence = "COMPLETED", "MAX_CYCLES_REACHED", 0

    def elapsed():
        """初始检查也消耗整任务时限，结束后的只读重算不再发起模型调用。"""
        return max(0, int((time.monotonic() - started) * 1000))

    try:
        for cycle in range(1, auth.max_cycles + 1):
            for kind in ("EXPLORATION", "RETEST"):
                used = ledger(output)["requests_used"]
                if used >= auth.max_total_requests:
                    stage, reason = "STOPPED", "REQUEST_BUDGET_EXHAUSTED"
                    break
                skip = kind == "RETEST" and not eligible(current, excluded, origin)
                before = elapsed()
                remaining = (auth.max_active_seconds * 1000 - before) // 1000
                if used >= auth.max_total_requests or remaining < 62:
                    stage, reason = "STOPPED", "REQUEST_BUDGET_EXHAUSTED" if used >= auth.max_total_requests else "ACTIVE_TIME_BUDGET_EXHAUSTED"
                    break
                child_auth = phase_auth(auth, kind, auth.max_total_requests - used, remaining, excluded)
                destination = output / "cycles" / f"{cycle:04d}" / kind.lower()
                sequence += 1
                write_new_json(output / "events" / f"{sequence:04d}.json", {
                    "cycle": cycle, "phase": kind, "skipped": skip, "source": current.relative_to(store.TEST_ROOT).as_posix(),
                    "output": destination.relative_to(store.TEST_ROOT).as_posix(), "elapsed_before_ms": before,
                    "authorization": child_auth.model_dump()})
                if skip:
                    no_progress += 1
                    break
                if kind == "EXPLORATION":
                    report = campaign.run(**common, source_dir=current, output=destination, authorization=child_auth,
                        backend_factory=(lambda role, n, arm=None, case=None, repetition=None: backend_factory(cycle, role, n, arm, case, repetition)) if backend_factory else None)
                else:
                    report = retest.run(**common, campaign_dir=current, output=destination, authorization=child_auth,
                        backend_factory=(lambda n, arm, case, repetition: backend_factory(cycle, "PLANNER", n, arm, case, repetition)) if backend_factory else None)
                if report.get("stage") != "COMPLETED":
                    stage, reason = report.get("stage", "FAILED"), report.get("stop_reason", "LOOP_CHILD_INVALID")
                    if stage not in {"STOPPED", "FAILED", "INTERRUPTED"}:
                        stage = "FAILED"
                    break
                current = destination
                if kind == "EXPLORATION" and report.get("stop_reason") in {"MODEL_SELECTED_NO_CHANGE", "REPEATED_CANDIDATE"}:
                    reason = report["stop_reason"]
                    break
                if kind == "RETEST":
                    excluded.update(tested(destination, store._read(destination / "experiment.json")[0], report))
                    margin = best_margin(report)
                    no_progress = 0 if margin > best else no_progress + 1
                    best = max(best, margin)
                    if report["selection"]["selected"] is not None:
                        reason = "QUALIFIED_CANDIDATE_FOUND"
            if stage != "COMPLETED" or reason in {"MODEL_SELECTED_NO_CHANGE", "REPEATED_CANDIDATE", "QUALIFIED_CANDIDATE_FOUND"}:
                break
            if no_progress >= auth.max_no_progress_cycles:
                reason = "NO_PROGRESS_LIMIT"
                break
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "FAILED", "LOOP_PHASE_FAILED"
    if elapsed() > auth.max_active_seconds * 1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    write_new_json(output / "experiment.json", metadata)
    write_new_json(output / "run-summary.json", {"stage": stage, "stop_reason": reason,
        "active_duration_ms": elapsed(), "requests_used": ledger(output)["requests_used"]})
    write_new_json(output / "integrity.json", file_manifest(output))
    result = compare(output)
    write_new_json(output / "comparison.json", result)
    return result


def _compare(output):
    """核对控制器顺序和剩余额度，实际计划与评分仍从子阶段原始响应重放。"""
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", **ledger(output),
            "phases_started": len(list((output / "events").glob("*.json"))), "automatically_adopted": False}
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json"):
        manifest.pop(name, None)
    if manifest != store._read(output / "integrity.json")[0]:
        raise ValueError("LOOP_ARTIFACT_CHANGED")
    meta = store._read(output / "experiment.json")[0]
    if (meta != store._read(output / "loop.json")[0] or meta["schema_version"] != VERSION
        or meta["controller_ref"] not in {controller_ref(), PRE_WINDOWS_CONTROLLER_REF}
        or meta["trusted_code_ref"] not in compatible_trusted_refs()
        or meta["holdout_evaluated"] is not False or meta["automatically_adopted"] is not False):
        raise ValueError("LOOP_PROTOCOL_CHANGED")
    auth = Authorization.model_validate(meta["authorization"])
    current, seed_meta, seed_report, origin = seed(store.TEST_ROOT / meta["source_experiment"])
    if (meta["source_ref"] != digest(store._read(current / "integrity.json")[0]) or meta["source"] != seed_meta["source"]
        or meta["profile"] != seed_meta["profile"] or meta["provider"] != seed_meta.get("provider", "openai-compatible")
        or meta["development_inputs_ref"] != digest(origin["inputs"]) or meta["development_gold_ref"] != digest(origin["gold"])
        or meta["baseline_manifest"] != origin["metadata"]["baseline_manifest"]):
        raise ValueError("LOOP_SOURCE_CHANGED")
    excluded = tested(current, seed_meta, seed_report)
    best, no_progress, used, cycle, phase = best_margin(seed_report), 0, 0, 1, "EXPLORATION"
    reports, recommendation, terminal, allowed_calls = [], None, None, set()
    summary = store._read(output / "run-summary.json")[0]
    if type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"] < 0:
        raise ValueError("LOOP_TIME_INVALID")
    previous_end = 0
    for n, path in enumerate(sorted((output / "events").glob("*.json")), 1):
        event = store._read(path)[0]
        if terminal or path.name != f"{n:04d}.json" or event["cycle"] != cycle or event["phase"] != phase or cycle > auth.max_cycles:
            raise ValueError("LOOP_PHASE_SEQUENCE_CHANGED")
        before = event["elapsed_before_ms"]
        if type(before) is not int or before < previous_end or before > summary["active_duration_ms"]:
            raise ValueError("LOOP_PHASE_TIME_INVALID")
        seconds = (auth.max_active_seconds * 1000 - before) // 1000
        if seconds < 62 or used >= auth.max_total_requests:
            raise ValueError("LOOP_PHASE_OVER_BUDGET")
        expected_auth = phase_auth(auth, phase, auth.max_total_requests - used, seconds, excluded)
        child = output / "cycles" / f"{cycle:04d}" / phase.lower()
        if (event["authorization"] != expected_auth.model_dump() or event["source"] != current.relative_to(store.TEST_ROOT).as_posix()
            or event["output"] != child.relative_to(store.TEST_ROOT).as_posix()):
            raise ValueError("LOOP_PHASE_AUTHORIZATION_CHANGED")
        if event["skipped"]:
            if phase != "RETEST" or eligible(current, excluded, origin) or child.exists():
                raise ValueError("LOOP_SKIP_INVALID")
            no_progress += 1
            reports.append({"cycle": cycle, "phase": phase, "stage": "SKIPPED", "reason": "NO_NEW_PROMISING_CANDIDATE", "requests_used": 0})
        else:
            report = iteration_comparator.compare(child) if phase == "EXPLORATION" else retest.compare(child)
            starts = set(child.rglob("calls/*.started.json"))
            used += len(starts)
            allowed_calls.update(starts)
            if report.get("decision") == "INVALID_EXPERIMENT":
                raise ValueError("LOOP_CHILD_INVALID")
            if report.get("stage") != "UNFINALIZED":
                cm = store._read(child / "experiment.json")[0]
                if (cm["authorization"] != expected_auth.model_dump() or cm["profile"] != meta["profile"]
                    or cm["source"] != meta["source"] or cm["provider"] != meta["provider"]
                    or any(cm[k] != meta[k] for k in ("development_inputs_ref", "development_gold_ref", "baseline_manifest"))):
                    raise ValueError("LOOP_CHILD_BINDING_CHANGED")
                if phase == "EXPLORATION":
                    source_paths = {h["path"] for h in cm["history_sources"]}
                    if not seed_meta["schema_version"].startswith("adaptive-validation-") or reports:
                        if current.relative_to(store.TEST_ROOT).as_posix() not in source_paths:
                            raise ValueError("LOOP_FEEDBACK_NOT_CONSUMED")
                elif cm["source_campaign"] != current.relative_to(store.TEST_ROOT).as_posix():
                    raise ValueError("LOOP_RETEST_SOURCE_CHANGED")
            reports.append({"cycle": cycle, "phase": phase, "experiment": child.relative_to(store.TEST_ROOT).as_posix(),
                "stage": report.get("stage"), "decision": report.get("decision"), "requests_used": len(starts)})
            previous_end = before + report.get("active_duration_ms", 0)
            if report.get("stage") != "COMPLETED":
                terminal = report.get("stop_reason", "LOOP_CHILD_INVALID")
                continue
            current = child
            if phase == "EXPLORATION" and report.get("stop_reason") in {"MODEL_SELECTED_NO_CHANGE", "REPEATED_CANDIDATE"}:
                terminal = report["stop_reason"]
            if phase == "RETEST":
                excluded.update(tested(child, cm, report))
                margin = best_margin(report)
                no_progress = 0 if margin > best else no_progress + 1
                best = max(best, margin)
                if report["selection"]["selected"]:
                    recommendation = {"experiment": child.relative_to(store.TEST_ROOT).as_posix(), "candidate": report["selection"]["selected"]}
                    terminal = "QUALIFIED_CANDIDATE_FOUND"
        if phase == "EXPLORATION":
            phase = "RETEST"
        else:
            cycle, phase = cycle + 1, "EXPLORATION"
            terminal = terminal or ("NO_PROGRESS_LIMIT" if no_progress >= auth.max_no_progress_cycles else
                "MAX_CYCLES_REACHED" if cycle > auth.max_cycles else None)
    facts = ledger(output)
    if (set(output.rglob("calls/*.started.json")) != allowed_calls or used != facts["requests_used"]
        or used != summary["requests_used"] or used > auth.max_total_requests):
        raise ValueError("LOOP_SHARED_BUDGET_CHANGED")
    stage, reason = summary["stage"], summary["stop_reason"]
    if summary["active_duration_ms"] < previous_end:
        raise ValueError("LOOP_PHASE_DURATION_CHANGED")
    if stage == "COMPLETED":
        if (reason != terminal or not terminal or summary["active_duration_ms"] > auth.max_active_seconds * 1000
            or any(r["stage"] not in {"COMPLETED", "SKIPPED"} for r in reports)):
            raise ValueError("LOOP_COMPLETION_INVALID")
    elif stage == "STOPPED":
        if reason == "REQUEST_BUDGET_EXHAUSTED" and used < auth.max_total_requests:
            raise ValueError("LOOP_BUDGET_STOP_INVALID")
        if reason == "ACTIVE_TIME_BUDGET_EXHAUSTED" and summary["active_duration_ms"] < auth.max_active_seconds * 1000 - 63000:
            raise ValueError("LOOP_TIME_STOP_INVALID")
    elif stage == "FAILED":
        if reason not in {terminal, "LOOP_PHASE_FAILED"}:
            raise ValueError("LOOP_FAILURE_REASON_CHANGED")
    elif stage == "INTERRUPTED":
        if reason != "USER_INTERRUPTED":
            raise ValueError("LOOP_INTERRUPT_REASON_CHANGED")
    else:
        raise ValueError("LOOP_STAGE_INVALID")
    if stage != "COMPLETED":
        recommendation = None
    return {"stage": stage, "decision": "DEVELOPMENT_SELECTED" if recommendation else "NO_ELIGIBLE_CANDIDATE" if stage == "COMPLETED" else "INCONCLUSIVE",
        "stop_reason": reason, "phases": reports, **facts, "model_calls": used if meta["source"] == "live" else 0,
        "active_duration_ms": summary["active_duration_ms"], "research_best_paired_gain": best,
        "recommendation": recommendation, "last_feedback_source": current.relative_to(store.TEST_ROOT).as_posix(),
        "evaluation_scope": "DEVELOPMENT_ONLY", "holdout_evaluated": False, "automatically_adopted": False}


def compare(output):
    """核验失败只返回固定错误，不回显私有数据，不发出新模型请求。"""
    try:
        return _compare(output)
    except Exception:
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "LOOP_INTEGRITY_FAILED", "automatically_adopted": False}
