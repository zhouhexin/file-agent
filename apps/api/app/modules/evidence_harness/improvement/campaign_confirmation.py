"""只测试已锁定的一份候选：开发确认或独立保留验收，不生成建议、不回传保留反馈。"""

import time
from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import campaign, adaptive_policy as policy, adaptive_validation as validation
from app.modules.evidence_harness.improvement import codex_backend, validation_dataset as data_store
from app.modules.evidence_harness.improvement.iteration_comparator import score_round
from app.modules.evidence_harness.improvement.executor import Journal, run_trial, run_checks
from app.modules.evidence_harness.improvement.isolation import snapshot, probe_sandbox, file_manifest, trusted_code_ref

VERSION = "campaign-confirmation-v1"
EXTENDED_VERSION = "campaign-confirmation-v2"


class ConfirmationAuthorization(validation.ValidationAuthorization):
    """扩大固定版本复测次数，但总额度和总时限仍须显式固定。"""

    repetitions: int = Field(default=5, ge=2, le=10)
    max_total_requests: int = Field(ge=1, le=5000)
    max_active_seconds: int = Field(default=7200, ge=1, le=28800)


def execution_profile(settings, authorization, provider, codex_model, codex_transport):
    """只检查实际Provider身份和可观测能力，不生成任何模型请求。"""
    if provider == "codex-login":
        spec = codex_backend.inspect(codex_model, **({"transport": codex_transport} if codex_transport is not None else {}))
        return spec, codex_backend.profile(spec, authorization.planner_max_output_tokens)
    if provider == "openai-compatible" and codex_model is None and codex_transport is None:
        return None, validation._execution_profile(settings, authorization)
    raise ValueError("CAMPAIGN_CONFIRM_PROVIDER_INVALID")


def _inputs(locked, split):
    """只按冻结人工分组取实际输入；保留题只进无建议器的确认执行器。"""
    if split not in {"DEVELOPMENT", "HOLDOUT"}:
        raise ValueError("CAMPAIGN_CONFIRM_SPLIT_INVALID")
    source = locked["origin"]
    dataset, _ = data_store.load(store.TEST_ROOT / source["metadata"]["dataset_source"])
    labels = [{"case_id": a.case_id, "expected_route": a.expected_route, "expected_decision_type": a.expected_decision_type}
              for a in dataset.annotations if a.split == split]
    ids = {a["case_id"] for a in labels}
    cases = [validation.baseline.effective_case(c, "enabled") for c in dataset.inputs.cases if c.case_id in ids]
    return cases, labels, dataset.inputs.catalog_snapshot


def check(*, lock_dir, split, output, settings, authorization, source="live", provider="openai-compatible",
          codex_model=None, codex_transport=None):
    """默认零调用；锁定后的模型、Provider及输出额度必须一致，不能临时更换条件。"""
    locked = campaign.load_lock(lock_dir)
    if locked.get("research_only") and split != "DEVELOPMENT":
        raise ValueError("RESEARCH_LOCK_HOLDOUT_FORBIDDEN")
    if source != locked["metadata"]["source"] or source == "live" and not settings.llm_enabled:
        raise ValueError("CAMPAIGN_CONFIRM_SOURCE_INVALID")
    spec, profile = execution_profile(settings, authorization, provider, codex_model, codex_transport)
    if profile != locked["raw"]["profile"] or provider != locked["metadata"]["provider"]:
        raise ValueError("CAMPAIGN_CONFIRM_PROFILE_CHANGED")
    cases, gold, catalog = _inputs(locked, split)
    return {"locked": locked, "cases": cases, "gold": gold, "catalog": catalog, "profile": profile,
        "backend_spec": spec, "output": policy.new_output(output), "planned_request_upper_bound": 2 * len(cases) * authorization.repetitions * 3}


def run(*, lock_dir, split, output, settings, authorization, source="live", provider="openai-compatible",
        codex_model=None, codex_transport=None, backend_factory=None):
    """一次明确授权只测固定候选；中断和未知费用均保留，不允许偷偷生成替代候选。"""
    started = time.monotonic()
    settings = settings.model_copy(deep=True)
    checked = check(lock_dir=lock_dir, split=split, output=output, settings=settings, authorization=authorization,
        source=source, provider=provider, codex_model=codex_model, codex_transport=codex_transport)
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("CAMPAIGN_CONFIRM_BACKEND_INVALID")
    output, locked = checked["output"], checked["locked"]
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    journal = Journal(output, authorization, profile_ref=digest(checked["profile"]))
    journal.started = started
    manifest = snapshot(output / "baseline")
    if manifest != locked["metadata"]["baseline_manifest"]:
        raise ValueError("CAMPAIGN_CONFIRM_SOURCE_CHANGED")
    extended = locked.get("research_only") or isinstance(authorization, ConfirmationAuthorization)
    meta = {"schema_version": EXTENDED_VERSION if extended else VERSION, "source": source, "provider": provider,
        "lock_source": str(locked["directory"].relative_to(store.TEST_ROOT)), "lock_ref": locked["lock_ref"], "split": split,
        "profile": checked["profile"], "policy_ref": locked["candidate"]["prompt_ref"], "baseline_manifest": manifest,
        "authorization": authorization.model_dump(), "trusted_code_ref": trusted_code_ref(),
        "inputs_ref": digest([c.model_dump(mode="json") for c in checked["cases"]]), "gold_ref": digest(checked["gold"]),
        "advisor_calls": 0, "feedback_export": "FORBIDDEN", "automatically_adopted": False}
    if extended:
        meta["purpose"] = "DEVELOPMENT_RETEST_ONLY" if locked.get("research_only") else "FINAL_CONFIRMATION"
    write_new_json(output / "experiment.json", meta)
    order = validation.schedule(checked["cases"], authorization.repetitions)
    write_new_json(output / "frozen-evaluation.json", {"metadata_ref": digest(meta), "schedule": order})
    stage, reason = "COMPLETED", "CONFIRMATION_COMPLETED"
    try:
        write_new_json(output / "sandbox-checks.json", probe_sandbox(output / "baseline", output, remaining=journal.remaining))
        policy.prepare_copies(output, locked["candidate"]["source"])
        checks = run_checks(output, journal)
        write_new_json(output / "checks.json", checks)
        if not checks["passed"]:
            raise ExperimentStopped("CAMPAIGN_CONFIRM_CHECKS_FAILED")
        for slot in order:
            journal.remaining()
            if journal.used >= authorization.max_total_requests:
                raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
            arm, cid, n = slot["arm"], slot["case_id"], slot["repetition"]
            case = next(c for c in checked["cases"] if c.case_id == cid)
            backend = (backend_factory(arm, case, n) if source == "fake" else
                codex_backend.CodexLoginTransport(checked["backend_spec"]) if provider == "codex-login" else policy._backend(settings))
            trial_started = time.monotonic()
            result = run_trial(code=output / arm, scratch=output / "scratch" / f"{arm}-{cid}-{n}", case=case,
                catalog=checked["catalog"], journal=journal, backend=backend, round_id=1, arm=arm, repetition=n, capture_audit=True,
                adaptive_prompt=locked["candidate"]["prompt"] if arm == "candidate" else None)
            write_new_json(output / "trials" / f"{arm}-{cid}-{n}.json", {**slot, "case_input_ref": digest(case.model_dump(mode="json")),
                "duration_ms": max(0, int((time.monotonic() - trial_started) * 1000)), "result": result})
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "FAILED", "CAMPAIGN_CONFIRM_ISOLATION_FAILED"
    if journal.elapsed() > authorization.max_active_seconds * 1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    write_new_json(output / "run-summary.json", {"stage": stage, "stop_reason": reason, "requests_used": journal.used,
        "active_duration_ms": journal.elapsed(), "model_calls": journal.used if source == "live" else 0})
    write_new_json(output / "integrity.json", file_manifest(output))
    report = compare(output)
    write_new_json(output / "comparison.json", report)
    return report


def _compare(output):
    """从锁定来源、固定试次及原始模型响应重算，保留结果不能成为优化历史来源。"""
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"decision": "INCONCLUSIVE", "stage": "UNFINALIZED", "automatically_adopted": False}
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json"):
        manifest.pop(name, None)
    if manifest != store._read(output / "integrity.json")[0]:
        raise ValueError("CAMPAIGN_CONFIRM_ARTIFACT_CHANGED")
    meta = store._read(output / "experiment.json")[0]
    if (meta["schema_version"] not in {VERSION, EXTENDED_VERSION} or meta["trusted_code_ref"] not in {trusted_code_ref(), campaign.PRE_LOOP_CONTROLLER_REF} or meta["advisor_calls"] != 0
        or meta["feedback_export"] != "FORBIDDEN" or meta["automatically_adopted"] is not False):
        raise ValueError("CAMPAIGN_CONFIRM_PROTOCOL_INVALID")
    locked = campaign.load_lock(store.TEST_ROOT / meta["lock_source"])
    cases, gold, catalog = _inputs(locked, meta["split"])
    auth = (ConfirmationAuthorization if meta["schema_version"] == EXTENDED_VERSION else validation.ValidationAuthorization).model_validate(meta["authorization"])
    if locked.get("research_only") and (meta["split"] != "DEVELOPMENT" or meta.get("purpose") != "DEVELOPMENT_RETEST_ONLY"):
        raise ValueError("RESEARCH_LOCK_HOLDOUT_FORBIDDEN")
    if meta["schema_version"] == EXTENDED_VERSION and meta.get("purpose") != ("DEVELOPMENT_RETEST_ONLY" if locked.get("research_only") else "FINAL_CONFIRMATION"):
        raise ValueError("CAMPAIGN_CONFIRM_PURPOSE_CHANGED")
    if (meta["lock_ref"] != locked["lock_ref"] or meta["source"] != locked["metadata"]["source"]
        or meta["provider"] != locked["metadata"]["provider"] or meta["profile"] != locked["raw"]["profile"]
        or meta["profile"]["planner_max_output_tokens"] != auth.planner_max_output_tokens
        or meta["policy_ref"] != locked["candidate"]["prompt_ref"]
        or meta["baseline_manifest"] != locked["metadata"]["baseline_manifest"]
        or meta["inputs_ref"] != digest([c.model_dump(mode="json") for c in cases]) or meta["gold_ref"] != digest(gold)):
        raise ValueError("CAMPAIGN_CONFIRM_BINDING_INVALID")
    order = validation.schedule(cases, auth.repetitions)
    if store._read(output / "frozen-evaluation.json")[0] != {"metadata_ref": digest(meta), "schedule": order}:
        raise ValueError("CAMPAIGN_CONFIRM_FREEZE_CHANGED")
    calls, known, unknown = policy.read_calls(output, meta, auth, cases)
    if any(c["role"] != "PLANNER" or c.get("profile_ref") != digest(meta["profile"]) for c in calls):
        raise ValueError("CAMPAIGN_CONFIRM_ADVISOR_OR_PROFILE_INVALID")
    positions = {(s["arm"], s["case_id"], s["repetition"]): i for i, s in enumerate(order)}
    actual_order = [positions[(c["arm"], c["case_id"], c["repetition"])] for c in calls]
    if actual_order != sorted(actual_order):
        raise ValueError("CAMPAIGN_CONFIRM_ORDER_CHANGED")
    summary = store._read(output / "run-summary.json")[0]
    if (summary["requests_used"] != len(calls) or summary["model_calls"] != (len(calls) if meta["source"] == "live" else 0)
        or summary["stage"] not in {"COMPLETED", "STOPPED", "INTERRUPTED", "FAILED"}
        or type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"] < 0
        or summary["stage"] == "COMPLETED" and summary["active_duration_ms"] > auth.max_active_seconds * 1000):
        raise ValueError("CAMPAIGN_CONFIRM_SUMMARY_INVALID")
    for arm in ("baseline", "control", "candidate"):
        if (output / arm).exists():
            expected = {**meta["baseline_manifest"], **({policy.TARGET: policy.source_hash(locked["candidate"]["source"])} if arm == "candidate" else {})}
            if file_manifest(output / arm) != expected:
                raise ValueError("CAMPAIGN_CONFIRM_SOURCE_CHANGED")
        elif calls:
            raise ValueError("CAMPAIGN_CONFIRM_SOURCE_MISSING")
    checks_ok = (output / "checks.json").exists() and store._read(output / "checks.json")[0] == policy.expected_checks()
    if calls and not checks_ok:
        raise ValueError("CAMPAIGN_CONFIRM_CHECKS_UNVERIFIED")
    records = []
    for path in sorted((output / "trials").glob("*.json")):
        record = store._read(path)[0]
        if path.name != f"{record['arm']}-{record['case_id']}-{record['repetition']}.json":
            raise ValueError("CAMPAIGN_CONFIRM_SLOT_CHANGED")
        records.append(record)
    if summary["stage"] == "COMPLETED" and len(records) != len(order):
        raise ValueError("CAMPAIGN_CONFIRM_TRIALS_MISSING")
    score = score_round(cases, gold, records, calls, catalog,
        policy.policy_context((output / "baseline" / policy.TARGET).read_text())["original_prompt"], locked["candidate"]["prompt"], auth.repetitions, checks_ok=checks_ok)
    score.update(evaluation_scope="HOLDOUT_ONLY" if meta["split"] == "HOLDOUT" else "DEVELOPMENT_ONLY",
        holdout_evaluated=meta["split"] == "HOLDOUT")
    decision = score["decision"]
    if summary["stage"] != "COMPLETED":
        decision = "INCONCLUSIVE"
    elif meta["split"] == "HOLDOUT" and score["strategy_evaluable"] and not score["regression_case_ids"] and not score["contract_regression_case_ids"] and not score["candidate_safety_failures"]:
        decision = "HOLDOUT_PASSED" if score["arms"]["candidate"]["correct"] == score["arms"]["candidate"]["planned"] else "HOLDOUT_FAILED"
    elif decision == "IMPROVED":
        decision = "DEVELOPMENT_CONFIRMED"
    return {"decision": decision, "split": meta["split"], "stage": summary["stage"], "stop_reason": summary["stop_reason"],
        "score": score, "policy_ref": meta["policy_ref"], "advisor_calls": 0, "requests_used": len(calls),
        "model_calls": summary["model_calls"], "known_tokens": known, "unknown_token_calls": unknown,
        "token_usage_complete": unknown == 0, "active_duration_ms": summary["active_duration_ms"],
        "evaluation_level": "INITIAL_ROUTING", "target_file_retrieval": "NOT_EXECUTED", "answer_quality": "NOT_EXECUTED",
        "automatically_adopted": False}


def compare(output):
    """独立确认核验失败关闭，不展开私有输入或触发额外模型调用。"""
    try:
        return _compare(output)
    except Exception:
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "CAMPAIGN_CONFIRM_INTEGRITY_FAILED", "automatically_adopted": False}
