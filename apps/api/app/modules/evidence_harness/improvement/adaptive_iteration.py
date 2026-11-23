"""Adaptive开发集的有限自主迭代入口；模型提出说明，可信宿主隔离执行并独立验收。"""

import shutil
import time

from pydantic import Field

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import adaptive_validation as validation
from app.modules.evidence_harness.improvement import validation_dataset as data_store
from app.modules.evidence_harness.improvement import codex_backend
from app.modules.evidence_harness.improvement.executor import Journal, run_trial, run_checks
from app.modules.evidence_harness.improvement.isolation import snapshot, probe_sandbox, file_manifest, trusted_code_ref
from app.modules.evidence_harness.improvement.patches import rejection_code
from app.modules.evidence_harness.improvement.planning_diagnosis import feedback_packet, tool_contracts
from app.modules.evidence_harness.improvement import iteration_proposals as proposals
from app.modules.evidence_harness.improvement import advisor_memory


# 新协议统一使用output_schema，兼容现有API与Codex包装器，不修改旧B请求身份。
LEGACY_ADVISOR_PROMPT = policy.POLICY_PROMPT.replace("response_schema", "output_schema") + """
本次是有总预算的开发集迭代，parameter_diagnostics由可信宿主用真实Tool输入模型自动生成。
区分已观察到的字段约束错误、任务对错、模型调用失败和对失败原因的推测。
source_context.tool_input_schemas是实际契约；违反契约时只能改善规划说明，不能放宽校验或编造澄清ID。
history仅含开发结果。没有收益不等于没有错误，也不等于必须继续改；允许合理NO_CHANGE。
PREFLIGHT和OUTSIDE_PROMPT_POLICY表示这轮没有使用可修改的提示，不能声称追加说明能改变该分支。
根据本轮反馈和历史自主生成一个新的完整追加说明，它替换上轮追加说明，原始提示始终保留。
不要重复已经试过的说明，不宣称保留集通过或已完成检索和回答。不要输出具体样本的特殊规则。
"""
# v1提示和载荷保留给历史只读重放；v2不再要求模型复制任何反馈或源码长哈希。
ADVISOR_PROMPT = LEGACY_ADVISOR_PROMPT.replace(
    "feedback_refs只能引用现有feedback_id；baseline_sha256绑定当前原版源码。",
    "feedback_refs只能选择本轮feedback_id的短编号；baseline_ref固定为BASELINE，完整哈希由宿主绑定。") + """
本轮输出使用adaptive-iteration-proposal-v2；feedback_refs仅从output_schema的枚举选择本轮短编号。
短编号包含轮次，不可引用其他轮次、缩写、猜测或补写不存在的编号。baseline_ref只填BASELINE。
短编号只用于feedback_refs和说明依据，不能写入instruction形成针对某条反馈的特殊规则。
instruction用通用自然语言表达，不使用斜杠列举，改用“和”“或”；原命令、路径及样本限制不变。
"""


class IterationAuthorization(policy.PolicyAuthorization):
    """操作者固定整个循环的轮数、总调用与时间；模型没有增加额度或恢复实验的权限。"""

    max_rounds: int = Field(default=2, ge=1, le=3)
    max_total_requests: int = Field(ge=1, le=1000)
    repetitions: int = Field(default=2, ge=1, le=3)
    max_active_seconds: int = Field(default=7200, ge=1, le=7200)
    max_no_progress: int = Field(default=2, ge=1, le=3)


def load_source(directory):
    """先完整只读核验C，再切出开发题；保留题内容不进入新实验和任何模型载荷。"""
    directory = _directory(directory)
    report = validation.compare(directory)
    if report.get("decision") == "INVALID_EXPERIMENT" or report.get("stage") != "COMPLETED":
        raise ValueError("ITERATION_SOURCE_NOT_VERIFIED_COMPLETED")
    meta = store._read(directory / "experiment.json")[0]
    dataset, locked = data_store.load(store.TEST_ROOT / meta["dataset_source"])
    annotations = [a for a in dataset.annotations if a.split == "DEVELOPMENT"]
    ids = {a.case_id for a in annotations}
    cases = [validation.baseline.effective_case(c, "enabled") for c in dataset.inputs.cases if c.case_id in ids]
    if not cases:
        raise ValueError("ITERATION_DEVELOPMENT_SET_EMPTY")
    records = [store._read(p)[0] for p in sorted((directory / "trials").glob("*.json"))
               if store._read(p)[0]["case_id"] in ids]
    calls = [store._read(p)[0] for p in sorted((directory / "calls").glob("*.finished.json"))
             if store._read(p)[0].get("case_id") in ids]
    scores = [{**r, "arm": arm} for arm in ("control", "candidate") for r in report["arms"][arm]["DEVELOPMENT"]["rows"]]
    projected = {"cases": [c.model_dump(mode="json") for c in cases], "catalog": dataset.inputs.catalog_snapshot}
    gold = [{"case_id": a.case_id, "expected_route": a.expected_route, "expected_decision_type": a.expected_decision_type}
            for a in annotations]
    history = [{"origin": "VERIFIED_SOURCE_DEVELOPMENT", "instruction": locked["candidate"]["proposal"]["instruction"],
        "arms": {arm: {"correct": report["arms"][arm]["DEVELOPMENT"]["correct"],
                        "planned": report["arms"][arm]["DEVELOPMENT"]["planned"]} for arm in ("control", "candidate")}}]
    return {"directory": directory, "metadata": meta, "source_ref": digest(store._read(directory / "integrity.json")[0]),
        "cases": cases, "gold": gold, "inputs": projected, "history": history,
        "feedback": feedback_packet(cases, records, scores, calls)}


def check(*, source_dir, output, settings, authorization, source="live", provider="openai-compatible",
          codex_model=None, codex_transport=None, allow_model_profile_change=False):
    """默认只读检查，不生成实验目录、不调用建议或规划模型。"""
    checked = load_source(source_dir)
    if source not in {"live", "fake"} or checked["metadata"]["source"] != source or source == "live" and not settings.llm_enabled:
        raise ValueError("ITERATION_SOURCE_INVALID")
    if provider == "codex-login":
        spec = codex_backend.inspect(codex_model, **({"transport": codex_transport} if codex_transport is not None else {}))
        profile = codex_backend.profile(spec, authorization.planner_max_output_tokens)
    elif provider == "openai-compatible" and codex_model is None and codex_transport is None:
        spec, profile = None, validation._execution_profile(settings, authorization)
    else:
        raise ValueError("ITERATION_PROVIDER_INVALID")
    details = validation._profile_details(checked["metadata"]["profile"], profile, allow_model_profile_change)
    context = policy.policy_context((checked["directory"] / "baseline" / policy.TARGET).read_text())
    context["tool_input_schemas"] = tool_contracts()
    upper = authorization.max_rounds * (1 + 2 * len(checked["cases"]) * authorization.repetitions * 3)
    return {**checked, **details, "provider": provider, "backend_spec": spec, "context": context,
        "output": policy.new_output(output), "planned_request_upper_bound": upper,
        "authorization_covers_upper_bound": authorization.max_total_requests >= upper}


def advisor_payload(packet, context, history, *, version="v2", round_id=1, prompt=None):
    """模型只见开发反馈、历史和公开契约；人工标签与保留题不外发。"""
    if version == "v1":
        return {"feedback": packet, "source_context": context, "history": history,
            "output_schema": policy.PolicyProposal.model_json_schema()}
    if version not in {"v2", "v3", "v4"}:
        raise ValueError("ITERATION_PROPOSAL_VERSION_INVALID")
    visible, source_context, schema = proposals.advisor_view(packet, context, round_id, extended=version in {"v3", "v4"})
    base = {"feedback": visible, "source_context": source_context, "output_schema": schema}
    if version == "v4":
        if not isinstance(prompt, str):
            raise ValueError("ADVISOR_MEMORY_PROMPT_REQUIRED")
        return advisor_memory.project(base, history, prompt)
    return {**base, "history": history}


def history_entry(number, candidate, score):
    """传递真实试过的说明与开发判定；不把部分改善或未运行包装成成功。"""
    return {"round_id": number, "instruction": candidate["proposal"]["instruction"],
        "policy_ref": candidate["prompt_ref"], "decision": score["decision"],
        "strategy_evaluable": score["strategy_evaluable"],
        "arms": {arm: {k: score["arms"][arm][k] for k in ("correct", "planned")} for arm in ("control", "candidate")},
        "regression_case_refs": [digest(cid) for cid in score["regression_case_ids"]],
        "candidate_parameter_failures": score["candidate_safety_failures"]}


def run(*, source_dir, output, settings, authorization, source="live", backend_factory=None,
        provider="openai-compatible", codex_model=None, codex_transport=None, allow_model_profile_change=False,
        campaign_mode=False, campaign_history=()):
    """单个全局Journal贯穿各轮，候选仅在隔离副本试跑；失败不重试、不改活动代码。"""
    from app.modules.evidence_harness.improvement.iteration_comparator import score_round, compare
    from app.modules.evidence_harness.improvement import campaign

    started = time.monotonic()
    settings = settings.model_copy(deep=True)
    checker = campaign.check if campaign_mode else check
    checked = checker(source_dir=source_dir, output=output, settings=settings, authorization=authorization, source=source,
        provider=provider, codex_model=codex_model, codex_transport=codex_transport,
        allow_model_profile_change=allow_model_profile_change, **({"history_dirs": campaign_history} if campaign_mode else {}))
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("ITERATION_BACKEND_SOURCE_INVALID")
    output = checked["output"]
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    journal = Journal(output, authorization, profile_ref=digest(checked["profile"]))
    journal.started = started
    stage, reason, rounds = "COMPLETED", "MAX_ROUNDS_REACHED", 0
    manifest = snapshot(output / "baseline")
    if manifest != checked["metadata"]["baseline_manifest"]:
        raise ValueError("ITERATION_EXECUTION_SOURCE_CHANGED")
    meta = {"schema_version": campaign.VERSION if campaign_mode else "adaptive-iteration-experiment-v2", "source": source, "provider": provider,
        "source_experiment": checked["directory"].relative_to(store.TEST_ROOT).as_posix(), "source_ref": checked["source_ref"],
        "development_inputs_ref": digest(checked["inputs"]), "development_gold_ref": digest(checked["gold"]),
        "baseline_manifest": manifest, "trusted_code_ref": trusted_code_ref(), "authorization": authorization.model_dump(),
        "evaluation_level": "INITIAL_ROUTING", "holdout_usage": "EXCLUDED_FROM_ITERATION",
        **{k: checked[k] for k in ("origin_profile", "profile", "allow_model_profile_change", "model_changed", "endpoint_changed", "comparison_scope")}}
    if campaign_mode:
        meta.update(history_sources=checked["history_sources"], selection=campaign.SELECTION,
            advisor_memory_policy=advisor_memory.POLICY)
    write_new_json(output / "experiment.json", meta)
    write_new_json(output / "development-inputs.json", checked["inputs"])
    write_new_json(output / "development-gold.json", {"labels": checked["gold"]})
    write_new_json(output / "initial-feedback.json", checked["feedback"])
    packet, history = checked["feedback"], checked["history"]
    seen, no_progress = set(checked.get("seen_policies", [])), 0

    def backend(role, number, arm=None, case=None, n=None):
        """凭证只留可信宿主，Codex/API对建议器和两组规划使用同一固定配置。"""
        if source == "fake":
            return backend_factory(role, number, arm, case, n)
        return codex_backend.CodexLoginTransport(checked["backend_spec"]) if provider == "codex-login" else policy._backend(settings)

    try:
        write_new_json(output / "sandbox-checks.json", probe_sandbox(output / "baseline", output, remaining=journal.remaining))
        for number in range(1, authorization.max_rounds + 1):
            journal.remaining()
            if journal.used >= authorization.max_total_requests:
                raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
            rounds = number
            root = output / "rounds" / f"{number:04d}"
            write_new_json(root / "feedback.json", packet)
            write_new_json(root / "history.json", {"attempts": history})
            source_text = (output / "baseline" / policy.TARGET).read_text()
            extended = campaign_mode and campaign.is_exploration(meta)
            write_new_json(root / "reference-map.json", proposals.reference_map(packet, number, source_text, extended=extended))
            prompt = campaign.advisor_prompt(meta) if campaign_mode else ADVISOR_PROMPT
            payload = advisor_payload(packet, checked["context"], history, round_id=number,
                version="v4" if campaign_mode else "v2", prompt=prompt)
            if campaign_mode:
                write_new_json(root / "advisor-memory.json", advisor_memory.audit(prompt, payload))
            raw = journal.call(backend("ADVISOR", number), role="ADVISOR", round_id=number,
                system_prompt=prompt, user_payload=payload)
            write_new_json(root / "proposal.json", raw)
            try:
                candidate = proposals.materialize(source_text, raw, packet, checked["cases"], number, extended=extended)
            except Exception as exc:
                reason = rejection_code(exc)
                write_new_json(root / "validation.json", proposals.rejection(exc))
                break
            write_new_json(root / "resolved-proposal.json", candidate["resolved_proposal"])
            if candidate["stage"] == "NO_CHANGE":
                reason = "MODEL_SELECTED_NO_CHANGE"
                write_new_json(root / "validation.json", {"stage": "NO_CHANGE", "reason_code": reason})
                break
            if candidate["prompt_ref"] in seen:
                reason = "REPEATED_CANDIDATE"
                write_new_json(root / "validation.json", {"stage": "REPEATED", "reason_code": reason})
                break
            seen.add(candidate["prompt_ref"])
            write_new_json(root / "validation.json", {"stage": "VALIDATED", "reason_code": None})
            policy._write_text(root / "candidate.patch", candidate["patch"])
            write_new_json(root / "policy.json", {k: candidate[k] for k in ("prompt_ref", "patch_ref")})
            shutil.copytree(output / "baseline", root / "baseline")
            policy.prepare_copies(root, candidate["source"])
            if campaign_mode:
                frozen = campaign.freeze(meta, candidate, number, checked["cases"])
                write_new_json(root / "frozen-evaluation.json", frozen)
                campaign.verify_frozen(root, frozen)
            checks = run_checks(root, journal)
            write_new_json(root / "checks.json", checks)
            if not checks["passed"]:
                stage, reason = "FAILED", "ITERATION_PROTECTED_CHECK_FAILED"
                break
            records = []
            for slot in validation.schedule(checked["cases"], authorization.repetitions):
                if campaign_mode:
                    campaign.verify_frozen(root, frozen)
                journal.remaining()
                if journal.used >= authorization.max_total_requests:
                    raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
                arm, cid, n = slot["arm"], slot["case_id"], slot["repetition"]
                case = next(c for c in checked["cases"] if c.case_id == cid)
                trial_started = time.monotonic()
                result = run_trial(code=root / arm, scratch=root / "scratch" / f"{arm}-{cid}-{n}", case=case,
                    catalog=checked["inputs"]["catalog"], journal=journal, backend=backend("PLANNER", number, arm, case, n),
                    round_id=number, arm=arm, repetition=n, capture_audit=True,
                    adaptive_prompt=candidate["prompt"] if arm == "candidate" else None)
                record = {**slot, "case_input_ref": digest(case.model_dump(mode="json")),
                    "duration_ms": max(0, int((time.monotonic() - trial_started) * 1000)), "result": result}
                write_new_json(root / "trials" / f"{arm}-{cid}-{n}.json", record)
                records.append(record)
            calls = [store._read(p)[0] for p in sorted((output / "calls").glob("*.finished.json"))
                     if store._read(p)[0]["round_id"] == number]
            score = score_round(checked["cases"], checked["gold"], records, calls, checked["inputs"]["catalog"],
                checked["context"]["original_prompt"], candidate["prompt"], authorization.repetitions, checks_ok=True)
            if campaign_mode:
                campaign.verify_frozen(root, frozen)
                write_new_json(root / "round-sealed.json", {"freeze_ref": digest(frozen), "score_ref": digest(score),
                    "trial_refs": [digest(r) for r in records], "requests_used_at_seal": journal.used})
            write_new_json(root / "score.json", score)
            packet = feedback_packet(checked["cases"], records, score["rows"], calls)
            entry = (campaign.history_entry(number, candidate, score, packet, profile=meta["profile"]) if campaign_mode else
                history_entry(number, candidate, score))
            history = [*history, entry]
            # 评分和下一轮输入均由宿主重放生成；模型不能自己声称通过。
            if score["decision"] == "IMPROVED":
                if not campaign_mode:
                    reason = "DEVELOPMENT_GAIN_VERIFIED"
                    break
                no_progress = 0
            else:
                no_progress += 1
            if no_progress >= authorization.max_no_progress:
                reason = "NO_PROGRESS_LIMIT"
                break
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "FAILED", "ITERATION_ADVISOR_OR_ISOLATION_FAILED"
    if journal.elapsed() > authorization.max_active_seconds * 1000:
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    write_new_json(output / "run-summary.json", {"stage": stage, "stop_reason": reason, "rounds_started": rounds,
        "requests_used": journal.used, "model_calls": journal.used if source == "live" else 0,
        "active_duration_ms": journal.elapsed(), "automatically_adopted": False})
    write_new_json(output / "integrity.json", file_manifest(output))
    report = compare(output)
    write_new_json(output / "comparison.json", report)
    policy._write_text(output / "summary.md", f"开发迭代：{report['decision']}；停止原因：{reason}；调用：{journal.used}。\n"
        "每轮只在隔离副本修改规划说明。保留题、检索和最终回答未执行，活动程序未更新。\n")
    return report
