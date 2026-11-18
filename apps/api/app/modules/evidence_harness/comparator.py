"""独立离线裁判：从不可覆盖的逐轮记录和冻结Gold重算，不相信模型自评分。"""

from collections import Counter
from pathlib import Path

from app.modules.evidence_evaluation.frozen_inputs import digest
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory, load_feedback
from app.modules.evidence_harness.schemas import ExecutionAuthorization, ExperimentProfile
from app.scripts.evaluate_evidence import load_cases, _percentile


def _signature(issues):
    return sorted((x.type, tuple(sorted(x.evidence_ids))) for x in issues)


def _metrics(rows):
    """包含失败和未执行轮次；没有正例的Recall为未知，不把空结果当正常。"""
    result = {}
    for kind in ("NORMAL", "MISSING", "CONFLICT", "OUTDATED"):
        tp = sum(kind in r["expected"] and kind in r["predicted"] for r in rows)
        fp = sum(kind not in r["expected"] and kind in r["predicted"] for r in rows)
        fn = sum(kind in r["expected"] and kind not in r["predicted"] for r in rows)
        result[kind] = {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}
    return result


def _latency(values):
    return {"count": len(values), "p50_ms": _percentile(values, .5), "p95_ms": _percentile(values, .95)}


def _compare(directory: Path) -> dict:
    """核对所有绑定与文件完整性后评分；未发布完成记录的实验只供中断检查。"""
    directory = _directory(directory)
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValueError("EXPERIMENT_SYMLINK_FORBIDDEN")
    experiment, _ = store._read(directory / "experiment.json")
    if experiment["schema_version"] != "harness-execution-v1" or experiment["repetitions"] != 3:
        raise ValueError("EXPERIMENT_SCHEMA_INVALID")
    from app.modules.evidence_harness.executor import load_validated_proposal
    packet = load_feedback(store.TEST_ROOT / experiment["feedback_source"])
    proposal, provenance = load_validated_proposal(store.TEST_ROOT / experiment["proposal_source"], packet)
    if provenance != experiment["prior_optimizer"] or experiment["source"] != provenance["source"]:
        raise ValueError("EXPERIMENT_PROVENANCE_MISMATCH")
    prior_ms = experiment.get("active_before_execution_ms", provenance["prior_active_ms"])
    if type(prior_ms) is not int or prior_ms < provenance["prior_active_ms"]:
        raise ValueError("EXPERIMENT_ACTIVE_TIME_INVALID")
    dataset_path = store._dataset_file(store.TEST_ROOT / experiment["dataset_source"])
    cases = load_cases(dataset_path)
    if (digest([c.model_dump(mode="json") for c in cases]) != experiment["dataset_sha256"]
        or experiment["dataset_sha256"] != packet.dataset_sha256
        or experiment["annotation_sha256"] != packet.annotation_sha256
        or experiment["proposal_sha256"] != provenance["proposal_sha256"] or len(cases) != 3):
        raise ValueError("EXPERIMENT_GOLD_OR_INPUT_CHANGED")
    baseline = next(r for r in packet.reports if r.report_ref == packet.baseline_ref)
    control = ExperimentProfile.model_validate(experiment["profiles"]["control"])
    candidate = ExperimentProfile.model_validate(experiment["profiles"]["candidate"])
    changed = control.model_dump();changed[proposal.change.field] = proposal.change.to
    if control.model_dump() != baseline.budget.model_dump() or changed != candidate.model_dump():
        raise ValueError("EXPERIMENT_PROFILE_INVALID")
    index, _ = store._read(store.TEST_ROOT / experiment["feedback_source"] / "source-index.json")
    source = next(s for s in index["reports"] if s["report_ref"] == packet.baseline_ref)
    baseline_report, _ = store._read(store._dataset_file(store.TEST_ROOT / source["source"]))
    configs = experiment["runtime_configs"]
    expected = dict(configs["control"]);expected[proposal.change.field] = proposal.change.to
    if configs["control"] != baseline_report["runtime_config"] or configs["candidate"] != expected:
        raise ValueError("EXPERIMENT_RUNTIME_INVALID")
    for arm, profile in (("control", control), ("candidate", candidate)):
        if any(configs[arm][key] != value for key, value in profile.model_dump().items()):
            raise ValueError("EXPERIMENT_EFFECTIVE_PROFILE_INVALID")
    authorization = ExecutionAuthorization.model_validate(experiment["authorization"])
    lease_path = _directory(store.TEST_ROOT / experiment["execution_lease"])
    lease, _ = store._read(lease_path)
    if lease != {"output_source": str(directory.relative_to(store.TEST_ROOT)),
                 "proposal_sha256": experiment["proposal_sha256"],
                 "authorization": authorization.model_dump(), "source": experiment["source"]}:
        raise ValueError("EXECUTION_LEASE_INVALID")
    summary = store._read(directory / "run-summary.json")[0] if (directory / "run-summary.json").exists() else None
    if summary and (summary["source"] != experiment["source"]
        or summary["stage"] not in {"COMPLETED", "STOPPED", "INTERRUPTED", "FAILED"}
        or any(type(summary[k]) is not int or summary[k] < 0 for k in ("total_requests_used", "active_duration_ms"))):
        raise ValueError("EXPERIMENT_SUMMARY_INVALID")
    if (directory / "integrity.json").exists():
        integrity, _ = store._read(directory / "integrity.json")
        actual = {str(p.relative_to(directory)): digest(store._read(p)[0]) for p in directory.rglob("*.json")
                  if p.name not in {"integrity.json", "comparison.json", "manifest.json"}}
        if integrity != actual:
            raise ValueError("EXPERIMENT_ARTIFACT_CHANGED")
    else:
        # 只看未完成文件的状态，不把没有完整性清单的结果当正式比较依据。
        summary = None
    outcomes, all_sequences = {}, []
    all_started = 0
    for arm, profile in (("control", control), ("candidate", candidate)):
        rows, mapping, successful_ms, failed_ms = [], {}, [], []
        for path in (directory / arm / "rounds").glob("*.json"):
            record, _ = store._read(path)
            key = (record["case_id"], record["repetition"])
            if key in mapping:
                raise ValueError("DUPLICATE_EXPERIMENT_ROUND")
            mapping[key] = record
        if set(mapping) - {(c.case_id, n) for c in cases for n in range(1, 4)}:
            raise ValueError("UNEXPECTED_EXPERIMENT_ROUND")
        calls, usages, unknown, request_times = {}, Counter(), 0, []
        for path in (directory / arm / "calls").glob("*.started.json"):
            start, _ = store._read(path)
            sequence = start["sequence"]
            if type(sequence) is not int or sequence in calls or start["profile"] != profile.model_dump():
                raise ValueError("REQUEST_PROFILE_OR_SEQUENCE_INVALID")
            calls[sequence] = start
            all_sequences.append(sequence);all_started += 1
            end_path = path.with_name(path.name.replace(".started.json", ".finished.json"))
            if not end_path.exists():
                unknown += 1
                continue
            end, _ = store._read(end_path)
            if end["sequence"] != sequence or end["duration_ms"] < 0 or end["status"] not in {"RETURNED", "FAILED", "INTERRUPTED"}:
                raise ValueError("REQUEST_END_INVALID")
            request_times.append(end["duration_ms"])
            for k, value in end["usage"].items():
                if k not in {"input_tokens", "output_tokens"} or type(value) is not int or value < 0:
                    raise ValueError("REQUEST_USAGE_INVALID")
                usages[k] += value
            unknown += set(end["usage"]) != {"input_tokens", "output_tokens"}
        for end_path in (directory / arm / "calls").glob("*.finished.json"):
            end, _ = store._read(end_path)
            if end["sequence"] not in calls:
                raise ValueError("REQUEST_END_WITHOUT_START")
        assigned = []
        for case in cases:
            for repetition in range(1, 4):
                record = mapping.get((case.case_id, repetition))
                row = {"case_id": case.case_id, "repetition": repetition,
                       "expected": sorted({x.type for x in case.expected_issues}) or ["NORMAL"],
                       "predicted": [], "status": "NOT_RUN", "matches_gold": False}
                if record:
                    if record["arm"] != arm or record["input_sha256"] != case.input_sha256:
                        raise ValueError("EXPERIMENT_ROUND_BINDING_INVALID")
                    trial = store._round(record["result"], report_ref=digest(experiment), case=case, repetition=repetition)
                    sequences = record["call_sequences"]
                    if len(sequences) > 1 or any(s not in calls for s in sequences) or trial.llm_calls != len(sequences):
                        raise ValueError("EXPERIMENT_ROUND_REQUEST_INVALID")
                    for sequence in sequences:
                        end_path = directory / arm / "calls" / f"{sequence:03d}.finished.json"
                        if not end_path.exists():
                            raise ValueError("ROUND_WITH_UNFINISHED_REQUEST")
                        end, _ = store._read(end_path)
                        if end["status"] == "INTERRUPTED" or (end["status"] == "FAILED" and trial.semantic_status != "EVALUATION_FAILED"):
                            raise ValueError("ROUND_REQUEST_STATUS_INVALID")
                    assigned.extend(sequences)
                    from app.modules.evidence_evaluation.schemas import EvaluationResult
                    result = EvaluationResult.model_validate(record["result"])
                    completed = result.evaluation_status == "COMPLETED"
                    row.update(status=result.evaluation_status,
                        predicted=sorted({x.type for x in result.issues}) or (["NORMAL"] if completed else []),
                        matches_gold=completed and _signature(result.issues) == _signature(case.expected_issues))
                    (successful_ms if completed else failed_ms).append(result.duration_ms)
                rows.append(row)
        if len(set(assigned)) != len(assigned):
            raise ValueError("REQUEST_REUSED_BY_MULTIPLE_ROUNDS")
        outcomes[arm] = {"denominator": len(rows), "completed": sum(r["status"] == "COMPLETED" for r in rows),
            "strict_matches": sum(r["matches_gold"] for r in rows), "rounds": rows,
            "per_class": _metrics(rows), "per_round": [{"repetition": n,
                "per_class": _metrics([r for r in rows if r["repetition"] == n])} for n in range(1, 4)],
            "per_case": {c.case_id: {"matches": sum(r["matches_gold"] for r in rows if r["case_id"] == c.case_id),
                "completed": sum(r["status"] == "COMPLETED" for r in rows if r["case_id"] == c.case_id)} for c in cases},
            "latency": {"completed": _latency(successful_ms), "failed": _latency(failed_ms),
                        "all": _latency(successful_ms + failed_ms), "requests": _latency(request_times)},
            "requests": len(calls), "known_input_tokens": usages["input_tokens"],
            "known_output_tokens": usages["output_tokens"], "unknown_usage_requests": unknown,
            "token_usage_complete": unknown == 0}
    prior = provenance["prior_calls"]
    if sorted(all_sequences) != list(range(prior + 1, prior + all_started + 1)):
        raise ValueError("GLOBAL_REQUEST_SEQUENCE_INVALID")
    used = prior + all_started
    if used > authorization.max_total_requests or (summary and summary["total_requests_used"] != used):
        raise ValueError("GLOBAL_REQUEST_BUDGET_INVALID")
    control_result, candidate_result = outcomes["control"], outcomes["candidate"]
    regressions = [cid for cid in control_result["per_case"] if any(
        candidate_result["per_case"][cid][key] < control_result["per_case"][cid][key] for key in ("matches", "completed"))]
    fully_run = all(r["status"] != "NOT_RUN" for x in outcomes.values() for r in x["rounds"])
    within_time = summary and summary["active_duration_ms"] <= authorization.max_active_seconds * 1000
    if not summary or summary["stage"] != "COMPLETED" or not fully_run or not within_time:
        decision = "INCONCLUSIVE"
    elif regressions or candidate_result["strict_matches"] < control_result["strict_matches"]:
        decision = "REGRESSION"
    elif candidate_result["strict_matches"] > control_result["strict_matches"]:
        decision = "IMPROVED"
    else:
        decision = "NO_GAIN"
    review = decision == "IMPROVED" and candidate_result["strict_matches"] == 9 and not regressions
    return {"schema_version": "harness-comparison-v1", "source": experiment["source"],
        "decision": decision, "arms": outcomes, "regressed_cases": regressions,
        "total_requests_used": used, "total_request_limit": authorization.max_total_requests,
        "execution_stage": summary["stage"] if summary else "UNFINALIZED",
        "stop_reason": summary.get("stop_reason") if summary else "INTERRUPTED_OR_STILL_RUNNING",
        "cost_comparable": bool(summary and summary["stage"] == "COMPLETED" and fully_run and
                                all(x["token_usage_complete"] for x in outcomes.values())),
        "total_known_input_tokens": sum(x["known_input_tokens"] for x in outcomes.values()) + (provenance["input_tokens"] or 0),
        "total_known_output_tokens": sum(x["known_output_tokens"] for x in outcomes.values()) + (provenance["output_tokens"] or 0),
        "total_token_usage_complete": provenance["token_usage_complete"] and all(x["token_usage_complete"] for x in outcomes.values()),
        "meets_review_criteria": review, "live_review_candidate": review and experiment["source"] == "live",
        "automatically_adopted": False}


def compare_experiment(directory: Path) -> dict:
    """只读独立复算；损坏/篡改输入显式INVALID，不输出验证输入值或原文。"""
    try:
        return _compare(directory)
    except Exception:
        return {"schema_version": "harness-comparison-v1", "decision": "INVALID_EXPERIMENT",
                "reason_code": "INPUT_OR_ARTIFACT_VALIDATION_FAILED", "automatically_adopted": False}
