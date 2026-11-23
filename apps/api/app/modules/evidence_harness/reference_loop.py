"""离线参考方案闭环：保留旧说明、同条件配对测量，严格通过才更新研究起点。"""

from collections import Counter
import difflib
import hashlib
import time
from typing import Literal

from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store, campaign_loop
from app.modules.evidence_harness.paths import Path, copy_tree
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.improvement import adaptive_iteration as iteration
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import campaign, iteration_proposals as proposals
from app.modules.evidence_harness.improvement import iteration_comparator as scoring, codex_backend
from app.modules.evidence_harness.improvement import advisor_memory
from app.modules.evidence_harness.improvement.executor import Journal, run_trial, run_checks
from app.modules.evidence_harness.improvement.isolation import (
    snapshot, file_manifest, probe_sandbox, trusted_code_ref, canonical_code,
)
from app.modules.evidence_harness.improvement.planning_diagnosis import feedback_packet

VERSION = "reference-candidate-loop-v1"
ADVISOR_PROMPT = campaign.advisor_prompt({"schema_version": campaign.VERSION}) + """
本次是保留旧方案的研究。source_context.reference_instruction是永久保留的旧参考全文，不代表已通过验收。
source_context.retained_instruction是本轮对照方案全文。inherit时在该方案上做有依据的小范围删改，保护其有效规则。
fresh时从原始提示重新探索，同时参考旧方案的经验。两种方式都输出完整替代说明，不把新文字不断追加到旧说明。
完整说明最多三千字符。原系统提示不可修改；不得为迎合开发题添加特例或声称保留集已通过。
"""


class Authorization(campaign_loop.Authorization):
    """统一授权首次对照和后续搜索，生成方式由操作者固定，模型不得更改。"""

    strategy: Literal["mixed", "inherit", "fresh"] = "mixed"
    fresh_every: int = Field(default=4, ge=2, le=10)


def controller_ref():
    """新增协议单独绑定控制器，旧流程和历史执行器身份不发生变化。"""
    return hashlib.sha256(canonical_code(Path(__file__))).hexdigest()


def generation_mode(auth, number):
    """混合模式按事先固定的序号探索，不能看到成绩后偷换生成方式。"""
    return ("fresh" if number % auth.fresh_every == 0 else "inherit") if auth.strategy == "mixed" else auth.strategy


def instruction_candidate(source, instruction, packet, cases):
    """重用既有文本、样本泄漏和 AST 门禁，不允许继承扩大代码修改权限。"""
    if instruction is None:
        prompt = policy.policy_context(source)["original_prompt"]
        return {"instruction": None, "prompt": prompt, "prompt_ref": digest(prompt), "source": source}
    raw = {"schema_version": "adaptive-policy-proposal-v1", "decision": "PROPOSE_POLICY",
        "feedback_refs": [packet["items"][0]["feedback_id"]], "baseline_sha256": policy.source_hash(source),
        "diagnosis": "INSUFFICIENT_EVIDENCE", "evidence_summary": "冻结研究说明，旧成绩不代表本轮通过。",
        "hypothesis": "在同条件下测量说明的实际效果。", "instruction": instruction,
        "no_change_reason": None, "reason": "只构造受限提示，是否替换由宿主独立评分。"}
    return {**policy.materialize(source, raw, packet, cases), "instruction": instruction}


def load_reference(directory, number, origin):
    """只读核验旧 C 或开发轮次；不从保留验收或缓存赢家选择研究起点。"""
    directory = _directory(directory)
    meta = store._read(directory / "experiment.json")[0]
    if meta["schema_version"].startswith("adaptive-validation-experiment-"):
        if number is not None:
            raise ValueError("REFERENCE_ROUND_NOT_APPLICABLE")
        checked = iteration.load_source(directory)
        instruction = checked["history"][0]["instruction"]
    else:
        if type(number) is not int or number < 1:
            raise ValueError("REFERENCE_ROUND_REQUIRED")
        directory, meta, report = campaign._verified(directory)
        if not any(r["round_id"] == number for r in report["rounds"]):
            raise ValueError("REFERENCE_ROUND_NOT_FOUND")
        checked = campaign.load_origin(store.TEST_ROOT / meta["source_experiment"])
        instruction = campaign.round_candidate(directory, number, checked)["proposal"]["instruction"]
    if (meta["source"] != origin["metadata"]["source"] or checked["inputs"] != origin["inputs"]
        or checked["gold"] != origin["gold"] or meta["baseline_manifest"] != origin["metadata"]["baseline_manifest"]):
        raise ValueError("REFERENCE_SOURCE_MISMATCH")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("REFERENCE_INSTRUCTION_REQUIRED")
    return {"path": directory.relative_to(store.TEST_ROOT).as_posix(), "round_id": number,
        "integrity_ref": digest(store._read(directory / "integrity.json")[0]),
        "instruction": instruction, "profile": meta["profile"]}


def check(*, source_dir, reference_dir, output, settings, authorization, reference_round=None,
          source="live", provider="openai-compatible", codex_model=None, codex_transport=None,
          allow_model_profile_change=False):
    """默认零调用、零落盘，先核验旧参考和开发来源，再检查当前执行配置。"""
    auth = Authorization.model_validate(authorization.model_dump())
    phase_auth = campaign.CampaignAuthorization.model_validate(auth.model_dump(include=set(campaign.CampaignAuthorization.model_fields)))
    checked = campaign.check(source_dir=source_dir, output=output, settings=settings, authorization=phase_auth,
        source=source, provider=provider, codex_model=codex_model, codex_transport=codex_transport,
        allow_model_profile_change=allow_model_profile_change)
    reference = load_reference(reference_dir, reference_round, checked)
    if reference["profile"] != checked["profile"] and not allow_model_profile_change:
        raise ValueError("REFERENCE_PROFILE_CHANGE_REQUIRES_AUTHORIZATION")
    original = (checked["directory"] / "baseline" / policy.TARGET).read_text(encoding="utf-8")
    candidate = instruction_candidate(original, reference["instruction"], checked["feedback"], checked["cases"])
    upper = 2 * len(checked["cases"]) * auth.retest_repetitions * 3 + auth.max_cycles * (
        auth.max_rounds * (1 + 2 * len(checked["cases"]) * auth.repetitions * 3)
        + auth.top_k * 2 * len(checked["cases"]) * auth.retest_repetitions * 3)
    return {**checked, "authorization": auth, "reference": reference, "reference_candidate": candidate,
        "planned_request_upper_bound": upper, "authorization_covers_upper_bound": auth.max_total_requests >= upper}


def replacement(score, repetitions):
    """原闸门之外再要求每次重复均提升；旧参考不合格也不能降低替换标准。"""
    gains = []
    for n in range(1, repetitions + 1):
        totals = {arm: sum(r["correct"] for r in score["rows"] if r["arm"] == arm and r["repetition"] == n)
            for arm in ("control", "candidate")}
        gains.append(totals["candidate"] - totals["control"])
    return {"replace": repetitions >= 2 and score["decision"] == "IMPROVED" and all(g > 0 for g in gains),
        "repeat_gains": gains, "score_decision": score["decision"]}


def _identity(candidate):
    """审计只存说明和散列，不序列化运行依赖。"""
    return {k: candidate[k] for k in ("instruction", "prompt_ref")}


def _lineage(parent, candidate, mode):
    """记录相对父说明的删改，完整替换使长度不会随代数无限增长。"""
    return {"parent": _identity(parent), "candidate": _identity(candidate), "generation_mode": mode,
        "instruction_diff": "".join(difflib.unified_diff((parent["instruction"] or "").splitlines(True),
            candidate["instruction"].splitlines(True), fromfile="retained", tofile="candidate"))}


def _copies(root, baseline, control, candidate):
    """两组都从原快照生成，最多变更同一个提示常量；不修改活动业务源码。"""
    for arm, item in (("control", control), ("candidate", candidate)):
        copy_tree(baseline, root / arm)
        target = root / arm / policy.TARGET
        target.chmod(0o600)
        target.write_text(item["source"], encoding="utf-8", newline="")
        target.chmod(0o400)


def _verify_copies(root, manifest, control, candidate):
    """每个试次前以及独立复算时逐文件核验，防止测量期间偷偷更换策略。"""
    for arm, item in (("control", control), ("candidate", candidate)):
        expected = {**manifest, policy.TARGET: policy.source_hash(item["source"])}
        if file_manifest(root / arm) != expected:
            raise ValueError("REFERENCE_SNAPSHOT_CHANGED")


def _measure(root, output, control, candidate, checked, journal, rid, repetitions, backend):
    """固定同一对策略交错运行，所有请求共用外层账本，不在复测时生成说明。"""
    _copies(root, output / "baseline", control, candidate)
    frozen = {
        "control": _identity(control), "candidate": _identity(candidate), "round_id": rid,
        "repetitions": repetitions, "profile_ref": digest(checked["profile"]),
        "schedule": iteration.validation.schedule(checked["cases"], repetitions)}
    write_new_json(root / "frozen-evaluation.json", frozen)
    checks = run_checks(root, journal)
    write_new_json(root / "checks.json", checks)
    if checks != policy.expected_checks():
        raise ExperimentStopped("REFERENCE_PROTECTED_CHECK_FAILED")
    records = []
    for slot in frozen["schedule"]:
        _verify_copies(root, checked["metadata"]["baseline_manifest"], control, candidate)
        journal.remaining()
        case = next(c for c in checked["cases"] if c.case_id == slot["case_id"])
        arm, n = slot["arm"], slot["repetition"]
        item = candidate if arm == "candidate" else control
        started = time.monotonic()
        result = run_trial(code=root / arm, scratch=root / "scratch" / f"{arm}-{case.case_id}-{n}",
            case=case, catalog=checked["inputs"]["catalog"], journal=journal,
            backend=backend(arm, case, n), round_id=rid, arm=arm, repetition=n, capture_audit=True,
            adaptive_prompt=item["prompt"] if item["instruction"] is not None else None)
        record = {**slot, "case_input_ref": digest(case.model_dump(mode="json")),
            "duration_ms": max(0, int((time.monotonic() - started) * 1000)), "result": result}
        write_new_json(root / "trials" / f"{arm}-{case.case_id}-{n}.json", record)
        records.append(record)
    calls = [store._read(p)[0] for p in sorted((output / "calls").glob("*.finished.json"))
        if store._read(p)[0]["round_id"] == rid and store._read(p)[0]["role"] == "PLANNER"]
    score = scoring.score_round(checked["cases"], checked["gold"], records, calls, checked["inputs"]["catalog"],
        control["prompt"], candidate["prompt"], repetitions, checks_ok=True)
    write_new_json(root / "score.json", score)
    return score, feedback_packet(checked["cases"], records, score["rows"], calls)


def _payload(checked, packet, history, retained, reference, mode, number):
    """完整父说明放在必保留上下文，历史可有界压缩，标签和保留题不交给建议器。"""
    context = {**checked["context"], "reference_instruction": reference["instruction"],
        "retained_instruction": retained["instruction"], "retained_policy_ref": retained["prompt_ref"],
        "generation_mode": mode, "allowed_change": "只替换完整追加说明，原系统提示逐字保留；说明最多三千字符"}
    return iteration.advisor_payload(packet, context, history, version="v4", round_id=number, prompt=ADVISOR_PROMPT)


def run(*, backend_factory=None, **options):
    """先重测旧方案，随后受限生成；任何失败都不覆盖旧参考或生产策略。"""
    started = time.monotonic()
    checked = check(**options)
    auth, output = checked["authorization"], checked["output"]
    source, provider = options.get("source", "live"), options.get("provider", "openai-compatible")
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("REFERENCE_BACKEND_SOURCE_INVALID")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    manifest = snapshot(output / "baseline")
    if manifest != checked["metadata"]["baseline_manifest"]:
        raise ValueError("REFERENCE_EXECUTION_SOURCE_CHANGED")
    meta = {"schema_version": VERSION, "controller_ref": controller_ref(), "trusted_code_ref": trusted_code_ref(),
        "source": source, "provider": provider, "source_experiment": _directory(options["source_dir"]).relative_to(store.TEST_ROOT).as_posix(),
        "reference": checked["reference"], "authorization": auth.model_dump(), "profile": checked["profile"],
        "allow_model_profile_change": options.get("allow_model_profile_change", False),
        "origin_profile": checked["origin_profile"], "model_changed": checked["model_changed"],
        "endpoint_changed": checked["endpoint_changed"], "comparison_scope": checked["comparison_scope"],
        "source_ref": digest(store._read(_directory(options["source_dir"]) / "integrity.json")[0]),
        "development_inputs_ref": digest(checked["inputs"]), "development_gold_ref": digest(checked["gold"]),
        "baseline_manifest": manifest, "holdout_evaluated": False, "automatically_adopted": False}
    write_new_json(output / "experiment.json", meta)
    write_new_json(output / "reference-policy.json", _identity(checked["reference_candidate"]))
    journal = Journal(output, auth, profile_ref=digest(checked["profile"]))
    journal.started = started
    reference = retained = checked["reference_candidate"]
    original_source = (output / "baseline" / policy.TARGET).read_text(encoding="utf-8")
    original = instruction_candidate(original_source, None, checked["feedback"], checked["cases"])
    packet, history, seen = checked["feedback"], list(checked["history"]), {retained["prompt_ref"]}
    stage, reason, attempts, no_progress = "COMPLETED", "MAX_CYCLES_REACHED", 0, 0

    def backend(cycle, role, number, arm=None, case=None, repetition=None):
        """两组共用当前 Provider 配置，候选子进程得不到真实凭证。"""
        if source == "fake":
            return backend_factory(cycle, role, number, arm, case, repetition)
        return codex_backend.CodexLoginTransport(checked["backend_spec"]) if provider == "codex-login" else policy._backend(options["settings"])

    try:
        write_new_json(output / "sandbox-checks.json", probe_sandbox(output / "baseline", output, remaining=journal.remaining))
        initial, packet = _measure(output / "reference-evaluation", output, original, reference, checked, journal, 0,
            auth.retest_repetitions, lambda arm, case, n: backend(0, "PLANNER", 0, arm, case, n))
        history.append(iteration.history_entry(0, reference, initial))
        for cycle in range(1, auth.max_cycles + 1):
            retests = 0
            for number in range(1, auth.max_rounds + 1):
                journal.remaining()
                attempts += 1
                root = output / "attempts" / f"{attempts:04d}"
                mode = generation_mode(auth, attempts)
                write_new_json(root / "context.json", {"cycle": cycle, "number": number, "mode": mode,
                    "parent": _identity(retained), "elapsed_before_ms": journal.elapsed()})
                payload = _payload(checked, packet, history, retained, reference, mode, number)
                raw = journal.call(backend(cycle, "ADVISOR", number), role="ADVISOR", round_id=2 * attempts - 1,
                    system_prompt=ADVISOR_PROMPT, user_payload=payload)
                write_new_json(root / "proposal.json", raw)
                candidate = proposals.materialize(original_source, raw, packet, checked["cases"], number, extended=True)
                if candidate["stage"] == "NO_CHANGE":
                    reason = "MODEL_SELECTED_NO_CHANGE"
                    break
                candidate["instruction"] = candidate["proposal"]["instruction"]
                write_new_json(root / "lineage.json", _lineage(retained, candidate, mode))
                if candidate["prompt_ref"] in seen:
                    reason = "REPEATED_CANDIDATE"
                    break
                seen.add(candidate["prompt_ref"])
                exploration, packet = _measure(root / "exploration", output, retained, candidate, checked, journal,
                    2 * attempts - 1, auth.repetitions, lambda arm, case, n: backend(cycle, "PLANNER", number, arm, case, n))
                history.append(iteration.history_entry(attempts, candidate, exploration))
                improved = exploration["arms"]["candidate"]["correct"] > exploration["arms"]["control"]["correct"]
                if improved and retests < auth.top_k:
                    retests += 1
                    score, packet = _measure(root / "retest", output, retained, candidate, checked, journal,
                        2 * attempts, auth.retest_repetitions, lambda arm, case, n: backend(cycle, "PLANNER", number, arm, case, n))
                    decision = replacement(score, auth.retest_repetitions)
                    write_new_json(root / "replacement.json", decision)
                    history.append(iteration.history_entry(attempts, candidate, score))
                    if decision["replace"]:
                        retained = candidate
                        no_progress = 0
                    else:
                        no_progress += 1
                else:
                    no_progress += 1
                if no_progress >= auth.max_no_progress * auth.max_no_progress_cycles:
                    reason = "NO_PROGRESS_LIMIT"
                    break
            if reason in {"MODEL_SELECTED_NO_CHANGE", "REPEATED_CANDIDATE", "NO_PROGRESS_LIMIT"}:
                break
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "FAILED", "REFERENCE_EXECUTION_FAILED"
    if journal.elapsed() > auth.max_active_seconds * 1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    write_new_json(output / "run-summary.json", {"stage": stage, "stop_reason": reason,
        "active_duration_ms": journal.elapsed(), "requests_used": journal.used, "attempts_started": attempts})
    write_new_json(output / "retained-policy.json", _identity(retained))
    write_new_json(output / "integrity.json", file_manifest(output))
    result = compare(output)
    write_new_json(output / "comparison.json", result)
    return result


def _calls(output, meta, auth):
    """核验统一请求序列、Profile 和额度，未知费用保留；未消费请求不能混入评分。"""
    rows, previous, counts = [], -1, Counter()
    starts = sorted((output / "calls").glob("*.started.json"))
    if {p.name for p in (output / "calls").glob("*.finished.json")} - {
        p.name.replace("started", "finished") for p in starts}:
        raise ValueError("REFERENCE_ORPHAN_CALL")
    for n, path in enumerate(starts, 1):
        row = store._read(path)[0]
        rid, role = row["round_id"], row["role"]
        if (row["sequence"] != n or path.name != f"{n:04d}.started.json" or type(rid) is not int or rid < 0 or rid < previous
            or rid > 2 * auth.max_cycles * auth.max_rounds or role not in {"ADVISOR", "PLANNER"}
            or row.get("profile_ref") != digest(meta["profile"]) or row["timeout_seconds"] != 60
            or row["max_output_tokens"] != (auth.advisor_max_output_tokens if role == "ADVISOR" else auth.planner_max_output_tokens)
            or type(row["active_ms_before"]) is not int or not 0 <= row["active_ms_before"] <= auth.max_active_seconds * 1000 - 62000
            or rows and row["active_ms_before"] < rows[-1]["active_ms_before"]):
            raise ValueError("REFERENCE_CALL_BINDING_CHANGED")
        if role == "ADVISOR" and (rid % 2 != 1 or any(r["round_id"] == rid for r in rows)
            or any(row[k] is not None for k in ("arm", "case_id", "repetition"))):
            raise ValueError("REFERENCE_ADVISOR_SLOT_INVALID")
        slot = rid, role, row["arm"], row["case_id"], row["repetition"]
        counts[slot] += 1
        if counts[slot] > (1 if role == "ADVISOR" else 3):
            raise ValueError("REFERENCE_SLOT_CALL_LIMIT")
        finish = path.with_name(path.name.replace("started", "finished"))
        item = store._read(finish)[0] if finish.exists() else {**row, "status": "UNKNOWN"}
        if any(item.get(k) != v for k, v in row.items()) or item["status"] not in {"COMPLETED", "FAILED", "UNKNOWN"}:
            raise ValueError("REFERENCE_CALL_FINISH_CHANGED")
        for key in ("duration_ms", "input_tokens", "output_tokens"):
            value = item.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("REFERENCE_USAGE_INVALID")
        rows.append(item)
        previous = rid
    if len(rows) > auth.max_total_requests:
        raise ValueError("REFERENCE_GLOBAL_BUDGET_EXCEEDED")
    if rows:
        probes = store._read(output / "sandbox-checks.json")[0]
        if any(probes.get(k) is not True for k in ("compile", "read_blocked", "write_blocked", "network_blocked", "exec_blocked")):
            raise ValueError("REFERENCE_RUNTIME_UNVERIFIED")
    return rows


def _replay_measure(root, control, candidate, checked, meta, calls, rid, repetitions, consumed):
    """从真实响应重放配对分数；完整缓存须全等，半份测量保留失败分母且不能替换。"""
    selected = [r for r in calls if r["round_id"] == rid and r["role"] == "PLANNER"]
    records = [store._read(p)[0] for p in sorted((root / "trials").glob("*.json"))]
    expected_slots = iteration.validation.schedule(checked["cases"], repetitions)
    positions = {(s["arm"], s["case_id"], s["repetition"]): i for i, s in enumerate(expected_slots)}
    observed = []
    record_slots = {(r["arm"], r["case_id"], r["repetition"]) for r in records}
    for row in selected:
        slot = row["arm"], row["case_id"], row["repetition"]
        if slot not in positions:
            raise ValueError("REFERENCE_TRIAL_SLOT_CHANGED")
        observed.append(positions[slot])
        consumed.add(row["sequence"])
    if observed != sorted(observed):
        raise ValueError("REFERENCE_TRIAL_ORDER_CHANGED")
    # 无响应记录的最终槽可以是执行中断；更早的请求不得消失。
    missing = [r for r in selected if (r["arm"], r["case_id"], r["repetition"]) not in record_slots]
    if missing and any(positions[(r["arm"], r["case_id"], r["repetition"])] != max(observed) for r in missing):
        raise ValueError("REFERENCE_TRIAL_RECORD_MISSING")
    frozen_path = root / "frozen-evaluation.json"
    if frozen_path.exists():
        expected = {"control": _identity(control), "candidate": _identity(candidate), "round_id": rid,
            "repetitions": repetitions, "profile_ref": digest(meta["profile"]), "schedule": expected_slots}
        if store._read(frozen_path)[0] != expected:
            raise ValueError("REFERENCE_FREEZE_CHANGED")
        _verify_copies(root, meta["baseline_manifest"], control, candidate)
    elif records or selected:
        raise ValueError("REFERENCE_FREEZE_MISSING")
    checks_path = root / "checks.json"
    checks_ok = checks_path.exists() and store._read(checks_path)[0] == policy.expected_checks()
    if selected and not checks_ok:
        raise ValueError("REFERENCE_CHECKS_CHANGED")
    readable_calls = [r for r in selected if (r["arm"], r["case_id"], r["repetition"]) in record_slots]
    score = scoring.score_round(checked["cases"], checked["gold"], records, readable_calls, checked["inputs"]["catalog"],
        control["prompt"], candidate["prompt"], repetitions, checks_ok=checks_ok)
    score_path = root / "score.json"
    if score_path.exists() and store._read(score_path)[0] != score:
        raise ValueError("REFERENCE_SCORE_CHANGED")
    complete = score_path.exists() and len(records) == len(expected_slots) and checks_ok
    return score, feedback_packet(checked["cases"], records, score["rows"], readable_calls) if records else None, complete


def _compare(output):
    """独立重建父版本、请求、候选和替换，不登录模型，不采信缓存赢家。"""
    output = _directory(output)
    if not (output / "integrity.json").exists():
        return {"stage": "UNFINALIZED", "decision": "INCONCLUSIVE", "automatically_adopted": False}
    manifest = file_manifest(output)
    for name in ("integrity.json", "comparison.json"):
        manifest.pop(name, None)
    if manifest != store._read(output / "integrity.json")[0]:
        raise ValueError("REFERENCE_ARTIFACT_CHANGED")
    meta = store._read(output / "experiment.json")[0]
    if (meta["schema_version"] != VERSION or meta["controller_ref"] != controller_ref()
        or meta["trusted_code_ref"] != trusted_code_ref() or meta["automatically_adopted"] is not False
        or meta["holdout_evaluated"] is not False):
        raise ValueError("REFERENCE_PROTOCOL_CHANGED")
    auth = Authorization.model_validate(meta["authorization"])
    source_dir = store.TEST_ROOT / meta["source_experiment"]
    c_source, history_sources = campaign.resolve_source(source_dir)
    checked = campaign.load_origin(c_source)
    checked.update(campaign.import_history(checked, history_sources))
    checked["context"] = policy.policy_context((output / "baseline" / policy.TARGET).read_text(encoding="utf-8"))
    checked["context"]["tool_input_schemas"] = iteration.tool_contracts()
    reference_data = load_reference(store.TEST_ROOT / meta["reference"]["path"], meta["reference"]["round_id"], checked)
    if (reference_data != meta["reference"] or meta["source_ref"] != digest(store._read(source_dir / "integrity.json")[0])
        or digest(checked["inputs"]) != meta["development_inputs_ref"] or digest(checked["gold"]) != meta["development_gold_ref"]
        or file_manifest(output / "baseline") != meta["baseline_manifest"]
        or meta["baseline_manifest"] != checked["metadata"]["baseline_manifest"]):
        raise ValueError("REFERENCE_SOURCE_CHANGED")
    scoring._profile(meta, checked["metadata"], auth)
    if reference_data["profile"] != meta["profile"] and not meta["allow_model_profile_change"]:
        raise ValueError("REFERENCE_PROFILE_AUTHORIZATION_CHANGED")
    checked["profile"] = meta["profile"]
    source_text = (output / "baseline" / policy.TARGET).read_text(encoding="utf-8")
    retained = reference = instruction_candidate(source_text, reference_data["instruction"], checked["feedback"], checked["cases"])
    original = instruction_candidate(source_text, None, checked["feedback"], checked["cases"])
    if store._read(output / "reference-policy.json")[0] != _identity(reference):
        raise ValueError("REFERENCE_POLICY_CHANGED")
    summary = store._read(output / "run-summary.json")[0]
    if type(summary["active_duration_ms"]) is not int or summary["active_duration_ms"] < 0:
        raise ValueError("REFERENCE_TIME_INVALID")
    calls, consumed = _calls(output, meta, auth), set()
    packet, history = checked["feedback"], list(checked["history"])
    initial, initial_packet, complete = _replay_measure(output / "reference-evaluation", original, reference, checked, meta,
        calls, 0, auth.retest_repetitions, consumed)
    phases = [{"phase": "REFERENCE", "score": initial, "completed": complete}]
    partial = not complete
    if complete:
        packet = initial_packet
        history.append(iteration.history_entry(0, reference, initial))
    seen, promotions, no_progress, terminal = {reference["prompt_ref"]}, [], 0, None
    paths = sorted((output / "attempts").glob("*"))
    if paths and partial:
        raise ValueError("REFERENCE_INITIAL_MEASUREMENT_INCOMPLETE")
    for attempt, root in enumerate(paths, 1):
        cycle, number = (attempt - 1) // auth.max_rounds + 1, (attempt - 1) % auth.max_rounds + 1
        if partial or terminal or cycle > auth.max_cycles or root.name != f"{attempt:04d}":
            raise ValueError("REFERENCE_ATTEMPT_ORDER_CHANGED")
        retests = 0 if number == 1 else retests
        mode = generation_mode(auth, attempt)
        context = store._read(root / "context.json")[0]
        before = context["elapsed_before_ms"]
        if (type(before) is not int or not 0 <= before <= summary["active_duration_ms"]
            or context != {"cycle": cycle, "number": number, "mode": mode, "parent": _identity(retained), "elapsed_before_ms": before}):
            raise ValueError("REFERENCE_PARENT_CHANGED")
        rid = 2 * attempt - 1
        payload = _payload(checked, packet, history, retained, reference, mode, number)
        advisors = [r for r in calls if r["round_id"] == rid and r["role"] == "ADVISOR"]
        for row in advisors:
            if row["request_ref"] != digest([ADVISOR_PROMPT, payload]) or row["active_ms_before"] < before:
                raise ValueError("REFERENCE_ADVISOR_REQUEST_CHANGED")
            consumed.add(row["sequence"])
        if not (root / "proposal.json").exists():
            partial = True
            continue
        if len(advisors) != 1 or advisors[0]["status"] != "COMPLETED":
            raise ValueError("REFERENCE_ADVISOR_RESPONSE_UNBOUND")
        raw = store._read(root / "proposal.json")[0]
        try:
            candidate = proposals.materialize(source_text, raw, packet, checked["cases"], number, extended=True)
        except ValueError:
            partial = True
            continue
        if candidate["stage"] == "NO_CHANGE":
            terminal = "MODEL_SELECTED_NO_CHANGE"
            continue
        candidate["instruction"] = candidate["proposal"]["instruction"]
        if store._read(root / "lineage.json")[0] != _lineage(retained, candidate, mode):
            raise ValueError("REFERENCE_LINEAGE_CHANGED")
        if candidate["prompt_ref"] in seen:
            terminal = "REPEATED_CANDIDATE"
            continue
        seen.add(candidate["prompt_ref"])
        score, feedback, complete = _replay_measure(root / "exploration", retained, candidate, checked, meta,
            calls, rid, auth.repetitions, consumed)
        phases.append({"phase": "EXPLORATION", "attempt": attempt, "score": score, "completed": complete})
        if not complete:
            partial = True
            continue
        packet = feedback
        history.append(iteration.history_entry(attempt, candidate, score))
        improved = score["arms"]["candidate"]["correct"] > score["arms"]["control"]["correct"]
        if improved and retests < auth.top_k:
            retests += 1
            score, feedback, complete = _replay_measure(root / "retest", retained, candidate, checked, meta,
                calls, rid + 1, auth.retest_repetitions, consumed)
            phases.append({"phase": "RETEST", "attempt": attempt, "score": score, "completed": complete})
            if not complete or not (root / "replacement.json").exists():
                partial = True
                continue
            decision = replacement(score, auth.retest_repetitions)
            if store._read(root / "replacement.json")[0] != decision:
                raise ValueError("REFERENCE_REPLACEMENT_CHANGED")
            packet = feedback
            history.append(iteration.history_entry(attempt, candidate, score))
            if decision["replace"]:
                retained = candidate
                promotions.append({"attempt": attempt, "policy_ref": retained["prompt_ref"]})
                no_progress = 0
            else:
                no_progress += 1
        else:
            if (root / "retest").exists() or (root / "replacement.json").exists():
                raise ValueError("REFERENCE_UNAUTHORIZED_RETEST")
            no_progress += 1
        if no_progress >= auth.max_no_progress * auth.max_no_progress_cycles:
            terminal = "NO_PROGRESS_LIMIT"
    if consumed != {r["sequence"] for r in calls} or summary["requests_used"] != len(calls) or summary["attempts_started"] != len(paths):
        raise ValueError("REFERENCE_CALL_ACCOUNTING_CHANGED")
    if store._read(output / "retained-policy.json")[0] != _identity(retained):
        raise ValueError("REFERENCE_RETAINED_POLICY_CHANGED")
    stage, reason = summary["stage"], summary["stop_reason"]
    if stage == "COMPLETED":
        expected_reason = terminal or "MAX_CYCLES_REACHED"
        if partial or reason != expected_reason or not terminal and len(paths) != auth.max_cycles * auth.max_rounds:
            raise ValueError("REFERENCE_STOP_CHANGED")
    elif stage == "STOPPED":
        if reason not in {"REQUEST_BUDGET_EXHAUSTED", "ACTIVE_TIME_BUDGET_EXHAUSTED", "REFERENCE_PROTECTED_CHECK_FAILED"}:
            raise ValueError("REFERENCE_STOP_REASON_INVALID")
        if reason == "REQUEST_BUDGET_EXHAUSTED" and len(calls) != auth.max_total_requests:
            raise ValueError("REFERENCE_BUDGET_STOP_INVALID")
    elif stage == "INTERRUPTED":
        if reason != "USER_INTERRUPTED":
            raise ValueError("REFERENCE_INTERRUPT_CHANGED")
    elif stage != "FAILED" or reason != "REFERENCE_EXECUTION_FAILED":
        raise ValueError("REFERENCE_STAGE_INVALID")
    facts = campaign_loop.ledger(output)
    if calls and calls[-1]["active_ms_before"] > summary["active_duration_ms"]:
        raise ValueError("REFERENCE_FINAL_TIME_CHANGED")
    return {"stage": stage, "decision": "DEVELOPMENT_SELECTED" if promotions and stage == "COMPLETED" else
        "NO_ELIGIBLE_CANDIDATE" if stage == "COMPLETED" else "INCONCLUSIVE", "stop_reason": reason,
        "reference": _identity(reference), "retained": _identity(retained), "promotions": promotions,
        "reference_qualified": initial["decision"] == "IMPROVED", "phases": phases, **facts,
        "model_calls": len(calls) if meta["source"] == "live" else 0, "active_duration_ms": summary["active_duration_ms"],
        "evaluation_scope": "DEVELOPMENT_ONLY", "holdout_evaluated": False, "automatically_adopted": False}


def compare(output):
    """比较失败只返回固定错误，不回显私有内容，不触发模型或配置加载。"""
    try:
        return _compare(output)
    except Exception:
        return {"decision": "INVALID_EXPERIMENT", "reason_code": "REFERENCE_INTEGRITY_FAILED", "automatically_adopted": False}
