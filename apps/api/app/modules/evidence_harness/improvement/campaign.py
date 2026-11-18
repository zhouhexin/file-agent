"""开发集实验管理：只读承接历史、逐候选冻结、独立排名及锁定，不替模型编写业务策略。"""

from contextvars import ContextVar
from copy import deepcopy
from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import adaptive_iteration as iteration
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import iteration_proposals as proposals
from app.modules.evidence_harness.improvement.isolation import file_manifest
from app.modules.evidence_harness.improvement.planning_diagnosis import feedback_packet
from app.modules.evidence_harness.improvement import advisor_memory

LEGACY_VERSION = "adaptive-campaign-experiment-v1"
EXPLORATION_V2 = "adaptive-campaign-experiment-v2"
VERSION = "adaptive-campaign-experiment-v3"
# 旧协议的提示、评分和冻结身份不变，只扩充有限来源链容量以支持阶段闭环。
PRE_LOOP_CONTROLLER_REF = "c7a54803c8365a159439c9985c1b4d0c48980fef5db0fba5cf11e1d2fa42bf93"
SELECTION = {"version": "campaign-selection-v1", "minimum_repetitions": 2,
    "eligibility": "STRICT_IMPROVED", "order": ["accuracy_desc", "all_repetitions_correct_desc", "complete_tokens_asc", "median_latency_asc", "id_asc"]}
_LOADING = ContextVar("campaign_history_stack", default=())


class LegacyCampaignAuthorization(iteration.IterationAuthorization):
    """新候选必须重复测量，所有轮共用明确预算，历史费用只审计不重新授权。"""

    repetitions: int = Field(default=2, ge=2, le=3)
    max_rounds: int = Field(default=3, ge=1, le=3)
    max_no_progress: int = Field(default=3, ge=1, le=3)


class CampaignAuthorization(LegacyCampaignAuthorization):
    """新探索先扩大候选覆盖，显式请求预算；一轮初测不等于最终验证通过。"""

    repetitions: int = Field(default=1, ge=1, le=3)
    max_rounds: int = Field(default=6, ge=1, le=10)
    max_no_progress: int = Field(default=6, ge=1, le=10)
    max_total_requests: int = Field(ge=1, le=5000)
    max_active_seconds: int = Field(default=7200, ge=1, le=28800)


def is_exploration(meta):
    """两版探索共用十轮短编号；只有v3启用有界历史，旧v2输入身份保持原样。"""
    return meta["schema_version"] in {EXPLORATION_V2, VERSION}


def advisor_prompt(meta):
    """改变实验策略说明，不人工规定任何业务修复；旧批次使用旧提示逐字重放。"""
    if not is_exploration(meta):
        return iteration.ADVISOR_PROMPT
    prompt = iteration.ADVISOR_PROMPT.replace("adaptive-iteration-proposal-v2", "adaptive-exploration-proposal-v1") + """
本次处于候选探索阶段。阅读全部开发历史，先识别已尝试的策略假设及其证据缺口，再自主提出有实质差异的新假设。
不要仅换措辞重复无效方案；说明新假设与已尝试方案的区别、预期改善的观察及可能损害的正常任务。
初测分数只是研究信号，不能声称已经稳定改善。程序会筛选值得复测的方案，并固定同一版本增加重复次数。
值得研究与最终推荐是两种资格，固定预检、模型调用失败和小样本波动应分别说明，不得删除失败以提高分数。
具体通用规划说明由你根据反馈自主生成，不要求固定修复句子；证据不足或超出允许范围时可合理NO_CHANGE。
候选测量期间禁止修改版本、题库、Gold、权限或预算，完成独立评分后才能提出下一份候选。
"""
    if meta["schema_version"] == VERSION:
        prompt += """
history是宿主从全部开发历史生成的有界摘要，不是全部逐题原始报告。计数覆盖所有历史观察，示例只展示其中一部分。
策略文字可能是标明省略字数的节选，全文身份保留在source_ref中。缺少展开详情不代表没有错误，不能据此声称全部修复。
结合当前轮完整feedback、全部候选摘要、错误统计及有限示例自主提出通用策略；不要把基础设施失败解释成已确认的业务根因。
"""
    return prompt


def _verified(directory):
    """重新核验来源和目录外依赖；递归环及超深历史一律拒绝，不缓存模型试次。"""
    from app.modules.evidence_harness.improvement.iteration_comparator import compare
    directory = _directory(directory)
    stack = _LOADING.get()
    if directory in stack or len(stack) >= 24:
        raise ValueError("CAMPAIGN_HISTORY_CYCLE_OR_DEPTH")
    token = _LOADING.set((*stack, directory))
    try:
        meta = store._read(directory / "experiment.json")[0]
        if meta["schema_version"] == "campaign-retest-batch-v1":
            from app.modules.evidence_harness.improvement import campaign_retesting
            report = campaign_retesting.compare(directory)
        else:
            report = compare(directory)
    finally:
        _LOADING.reset(token)
    if report.get("decision") in {"INVALID_EXPERIMENT", "INCONCLUSIVE"} and report.get("stage") in {None, "UNFINALIZED"}:
        raise ValueError("CAMPAIGN_HISTORY_NOT_VERIFIED")
    if report.get("stage") not in {"COMPLETED", "STOPPED", "INTERRUPTED", "FAILED"}:
        raise ValueError("CAMPAIGN_HISTORY_NOT_COMPLETED")
    return directory, store._read(directory / "experiment.json")[0], report


def load_origin(directory):
    """C来源及其目录外输入、候选来源链每次均独立核验，不能只检查当前目录。"""
    directory = _directory(directory)
    return iteration.load_source(directory)


def resolve_source(source_dir, history_dirs=()):
    """按不可变来源链找到C，扁平承接开发历史；确认/保留产物不能进入优化反馈。"""
    pending, found, c_source = [source_dir, *history_dirs], {}, None
    while pending:
        directory = _directory(pending.pop(0))
        if directory in found:
            continue
        if len(found) >= 24:
            raise ValueError("CAMPAIGN_HISTORY_COUNT_EXCEEDED")
        meta = store._read(directory / "experiment.json")[0]
        if meta["schema_version"].startswith("adaptive-validation-experiment-"):
            if c_source is not None and c_source != directory:
                raise ValueError("CAMPAIGN_MULTIPLE_C_SOURCES")
            c_source = directory
            continue
        if meta["schema_version"] not in {"adaptive-iteration-experiment-v1", "adaptive-iteration-experiment-v2", LEGACY_VERSION, EXPLORATION_V2, VERSION, "campaign-retest-batch-v1"}:
            raise ValueError("CAMPAIGN_HISTORY_TYPE_INVALID")
        directory, meta, report = _verified(directory)
        found[directory] = (meta, report)
        pending.append(store.TEST_ROOT / meta["source_experiment"])
        pending += [store.TEST_ROOT / h["path"] for h in meta.get("history_sources", [])]
    if c_source is None:
        raise ValueError("CAMPAIGN_C_SOURCE_MISSING")
    # 复测的直接依赖只有一个，不能按列表长度把新复测排到旧搜索之前。
    depths = {}
    def depth(path, visiting=()):
        """按真实依赖拓扑展开，依赖循环即拒绝，不用mtime或目录名字猜时间。"""
        if path in visiting:
            raise ValueError("CAMPAIGN_HISTORY_CYCLE_OR_DEPTH")
        if path not in depths:
            parents = [store.TEST_ROOT / h["path"] for h in found[path][0].get("history_sources", [])]
            parents = [p.resolve() for p in parents if p.resolve() in found]
            depths[path] = 1 + max((depth(p, (*visiting, path)) for p in parents), default=-1)
        return depths[path]
    ordered = sorted(found, key=lambda p: (depth(p), str(p)))
    return c_source, [(p, *found[p]) for p in ordered]


def round_candidate(directory, number, origin):
    """从原始提案精确重建，不从缓存patch或候选自报分数恢复策略。"""
    root = directory / "rounds" / f"{number:04d}"
    meta = store._read(directory / "experiment.json")[0]
    if meta["schema_version"] == "campaign-retest-batch-v1":
        child = store._read(root / "experiment.json")[0]
        return load_lock(store.TEST_ROOT / child["lock_source"])["candidate"]
    source = (directory / "baseline" / policy.TARGET).read_text()
    raw, packet = store._read(root / "proposal.json")[0], store._read(root / "feedback.json")[0]
    return (policy.materialize(source, raw, packet, origin["cases"]) if meta["schema_version"] == "adaptive-iteration-experiment-v1" else
        proposals.materialize(source, raw, packet, origin["cases"], number, extended=is_exploration(meta)))


def import_history(origin, sources):
    """只重建同一开发集的结果，不读取保留题评分或向模型发送人工预期。"""
    from app.modules.evidence_harness.improvement.iteration_comparator import score_round
    packet, history, entries, observations, seen = origin["feedback"], list(origin["history"]), [], [], set()
    for source_index, (directory, meta, report) in enumerate(sources, 1):
        if (meta["source_ref"] != origin["source_ref"] or meta["source"] != origin["metadata"]["source"]
            or meta["development_inputs_ref"] != digest(origin["inputs"]) or meta["development_gold_ref"] != digest(origin["gold"])
            or meta["baseline_manifest"] != origin["metadata"]["baseline_manifest"]):
            raise ValueError("CAMPAIGN_HISTORY_INPUT_MISMATCH")
        entries.append({"path": str(directory.relative_to(store.TEST_ROOT)),
            "integrity_ref": digest(store._read(directory / "integrity.json")[0]),
            "requests_used": report["requests_used"], "active_duration_ms": report["active_duration_ms"]})
        for item in report["rounds"]:
            number = item["round_id"]
            if item["proposal_stage"] != "VALIDATED" or not item["score"]:
                history.append({"origin": "VERIFIED_DEVELOPMENT_HISTORY", "attempt": f"H{source_index}R{number}",
                    "proposal_stage": item["proposal_stage"], "outcome": report["stop_reason"]})
                continue
            candidate = round_candidate(directory, number, origin)
            seen.add(candidate["prompt_ref"])
            root = directory / "rounds" / f"{number:04d}"
            retest = meta["schema_version"] == "campaign-retest-batch-v1"
            call_root = root if retest else directory
            records = [store._read(p)[0] for p in sorted((root / "trials").glob("*.json"))]
            calls = [store._read(p)[0] for p in sorted((call_root / "calls").glob("*.finished.json"))
                     if store._read(p)[0]["round_id"] == (1 if retest else number) and store._read(p)[0]["role"] == "PLANNER"]
            score = score_round(origin["cases"], origin["gold"], records, calls, origin["inputs"]["catalog"],
                policy.policy_context((call_root / "baseline" / policy.TARGET).read_text())["original_prompt"],
                candidate["prompt"], meta["authorization"]["repetitions"], checks_ok=(root / "checks.json").exists() and store._read(root / "checks.json")[0] == policy.expected_checks())
            historical_feedback = feedback_packet(origin["cases"], records, score["rows"], calls) if records else None
            history.append(history_entry(number, candidate, score, historical_feedback,
                prefix=f"H{source_index}R{number}", profile=meta["profile"]))
            if records:
                packet = historical_feedback
            observations.append(observation(f"H{source_index}R{number}", directory, number, meta, score, candidate["prompt_ref"]))
    return {"feedback": packet, "history": history, "history_sources": entries,
        "historical_candidates": observations, "seen_policies": seen}


def history_entry(number, candidate, score, packet, *, prefix=None, profile=None):
    """保留每轮具体观察，历史引用改用独立短标签，不与本轮可选反馈编号混淆。"""
    visible = deepcopy(packet)
    if visible:
        for index, item in enumerate(visible["items"], 1):
            item["feedback_id"] = f"{prefix or 'P' + str(number)}F{index:03d}"
        visible["schema_version"] = "campaign-historical-observations-v1"
    return {**iteration.history_entry(number, candidate, score), "origin": "VERIFIED_DEVELOPMENT_HISTORY",
        "profile": profile, "observations": visible}


def check(*, source_dir, history_dirs=(), **options):
    """默认完整核验且零调用、不建目录；历史和当前两组采用明确的新实验授权。"""
    options["authorization"] = CampaignAuthorization.model_validate(options["authorization"].model_dump())
    c_source, sources = resolve_source(source_dir, history_dirs)
    checked = iteration.check(source_dir=c_source, **options)
    # 已观察固定预检不属于可修改提示，启动前明确展示，不能靠删题制造合格候选。
    by_case = {c.case_id: [i for i in checked["feedback"]["items"] if i["case_ref"] == digest(c.case_id)] for c in checked["cases"]}
    preflight = [cid for cid, rows in by_case.items() if rows and all(i["effective_mode"] == "PREFLIGHT" for i in rows)]
    result = {**checked, **import_history(checked, sources), "initial_preflight_case_ids": preflight}
    prompt = advisor_prompt({"schema_version": VERSION})
    payload = iteration.advisor_payload(result["feedback"], result["context"], result["history"],
        version="v4", round_id=1, prompt=prompt)
    return {**result, "advisor_request_characters": advisor_memory.request_characters(prompt, payload),
        "advisor_request_limit": advisor_memory.POLICY["request_characters"]}


def run(*, history_dirs=(), **options):
    """只切换实验管理模式，模型仍自行生成候选，复用原全局预算和OS执行器。"""
    return iteration.run(**options, campaign_mode=True, campaign_history=history_dirs)


def freeze(meta, candidate, number, cases):
    """测试前固定全部可观测条件；模型不能在测量中更换代码、题目、Profile或裁判。"""
    from app.modules.evidence_harness.improvement.adaptive_validation import schedule
    return {"schema_version": "campaign-frozen-evaluation-v1", "round_id": number,
        "baseline_manifest": meta["baseline_manifest"], "candidate_manifest": {**meta["baseline_manifest"], policy.TARGET: policy.source_hash(candidate["source"])},
        "policy_ref": candidate["prompt_ref"], "patch_ref": candidate["patch_ref"],
        "inputs_ref": meta["development_inputs_ref"], "gold_ref": meta["development_gold_ref"],
        "profile": meta["profile"], "authorization": meta["authorization"], "trusted_code_ref": meta["trusted_code_ref"],
        "advisor_prompt_ref": digest(advisor_prompt(meta)), "selection": SELECTION,
        "schedule": schedule(cases, meta["authorization"]["repetitions"])}


def verify_frozen(root, expected):
    """前后核对真实源码清单，不采信候选进程或冻结JSON自身的承诺。"""
    if store._read(root / "frozen-evaluation.json")[0] != expected:
        raise ValueError("CAMPAIGN_FREEZE_CHANGED")
    for arm in ("baseline", "control", "candidate"):
        manifest = expected["candidate_manifest"] if arm == "candidate" else expected["baseline_manifest"]
        if file_manifest(root / arm) != manifest:
            raise ValueError("CAMPAIGN_FROZEN_SOURCE_CHANGED")


def observation(identifier, directory, number, meta, score, policy_ref):
    """保存开发观察及参与统一比较所需的条件，不含题干、人工标签或保留信息。"""
    arm = score["arms"]["candidate"]
    return {"id": identifier, "source": str(directory.relative_to(store.TEST_ROOT)), "round_id": number,
        "policy_ref": policy_ref, "profile": meta["profile"], "repetitions": meta["authorization"]["repetitions"],
        "decision": score["decision"], "strategy_evaluable": score["strategy_evaluable"],
        "correct": arm["correct"], "planned": arm["planned"],
        "stable_correct_cases": sum(c["correct"] == c["planned"] for c in arm["cases"]),
        "unknown_token_calls": arm["unknown_token_calls"], "known_tokens": arm["known_tokens"],
        "median_latency_ms": arm["latency_ms"]["median"], "safety_failures": score["candidate_safety_failures"]}


def select(observations, profile, repetitions):
    """先冻结可比条件及原严格门槛，再按业务、稳定性、完整费用和延迟排序；允许没有赢家。"""
    ranked = []
    for item in observations:
        comparable = item["profile"] == profile and item["repetitions"] == repetitions
        eligible = comparable and repetitions >= 2 and item["decision"] == "IMPROVED" and item["strategy_evaluable"]
        ranked.append({**item, "comparable": comparable, "eligible": eligible})
    def key(item):
        """费用缺失排在完整费用后，不能把未知当成便宜；失败者仍列示但不获资格。"""
        return (-item["correct"] / item["planned"], -item["stable_correct_cases"], bool(item["unknown_token_calls"]),
            sum(item["known_tokens"].values()) if not item["unknown_token_calls"] else float("inf"),
            item["median_latency_ms"] if item["median_latency_ms"] is not None else float("inf"), item["id"])
    ranked.sort(key=key)
    eligible = [r for r in ranked if r["eligible"]]
    return {"criteria": SELECTION, "ranking": ranked, "selected": eligible[0]["id"] if eligible else None,
        "best_observed": next((r["id"] for r in ranked if r["comparable"]), None),
        "selection_decision": "DEVELOPMENT_SELECTED" if eligible else "NO_ELIGIBLE_CANDIDATE"}


def lock(*, campaign_dir, output):
    """独立重算后只锁定合格入选者；不会生成新建议、修改原实验或自动发布。"""
    directory, meta, report = _verified(campaign_dir)
    if meta["schema_version"] not in {LEGACY_VERSION, EXPLORATION_V2, VERSION, "campaign-retest-batch-v1"} or not report.get("selection", {}).get("selected"):
        raise ValueError("CAMPAIGN_NO_ELIGIBLE_CANDIDATE")
    selected = next(r for r in report["selection"]["ranking"] if r["id"] == report["selection"]["selected"])
    output = policy.new_output(output)
    payload = {"schema_version": "campaign-candidate-lock-v1", "campaign_source": str(directory.relative_to(store.TEST_ROOT)),
        "campaign_ref": digest(store._read(directory / "integrity.json")[0]), "selected": selected,
        "profile": meta["profile"], "holdout_used": False, "automatically_adopted": False}
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_new_json(output / "lock.json", payload)
    write_new_json(output / "integrity.json", file_manifest(output))
    return {"stage": "LOCKED", "policy_ref": selected["policy_ref"], "selected": selected["id"], "model_calls": 0, "automatically_adopted": False}


def load_lock(directory):
    """核对锁定来源及重新排名，只从真实提案恢复同一个候选，不能通过改锁文件换冠军。"""
    directory = _directory(directory)
    manifest = file_manifest(directory)
    manifest.pop("integrity.json", None)
    raw = store._read(directory / "lock.json")[0]
    if manifest != store._read(directory / "integrity.json")[0]:
        raise ValueError("CAMPAIGN_LOCK_CHANGED")
    if raw["schema_version"] == "campaign-retest-lock-v1":
        from app.modules.evidence_harness.improvement import campaign_retesting
        return campaign_retesting.load_research_lock(directory, raw)
    if raw["schema_version"] != "campaign-candidate-lock-v1":
        raise ValueError("CAMPAIGN_LOCK_CHANGED")
    source, meta, report = _verified(store.TEST_ROOT / raw["campaign_source"])
    chosen = report.get("selection", {}).get("selected")
    expected = next((r for r in report.get("selection", {}).get("ranking", []) if r["id"] == chosen), None)
    if (expected is None or raw["selected"] != expected or raw["profile"] != meta["profile"] or raw["holdout_used"] is not False
        or raw["automatically_adopted"] is not False or raw["campaign_ref"] != digest(store._read(source / "integrity.json")[0])):
        raise ValueError("CAMPAIGN_LOCK_BINDING_INVALID")
    origin = load_origin(store.TEST_ROOT / meta["source_experiment"])
    candidate = round_candidate(store.TEST_ROOT / expected["source"], expected["round_id"], origin)
    return {"directory": directory, "raw": raw, "metadata": meta, "origin": origin, "candidate": candidate,
        "lock_ref": digest(store._read(directory / "integrity.json")[0])}
