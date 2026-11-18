"""一次受控离线闭环：统一编排、总预算、历史反馈新版本和可检查的终止记录。"""

import hashlib
from pathlib import Path
import time

from app.modules.evidence_evaluation.frozen_inputs import digest, runtime_config, write_new_json
from app.modules.evidence_evaluation.semantic_checker import SemanticChecker
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.executor import _profile_settings, run_experiment, ExperimentStopped
from app.modules.evidence_harness.history import extend_feedback, save_feedback
from app.modules.evidence_harness.optimizer import (
    _directory, request_payload, build_optimizer_client, propose, load_feedback, TIMEOUT_SECONDS,
)
from app.modules.evidence_harness.schemas import ExperimentProfile, ExecutionAuthorization
from app.modules.evidence_harness.transport import ProcessTransport
from app.scripts.evaluate_evidence import load_cases


def check_cycle(*, dataset_path: Path, baseline_path: Path, history_paths: list[Path],
                experiment_dirs: list[Path], output: Path, settings, authorization: ExecutionAuthorization,
                source: str, previous_cycle: Path | None = None) -> dict:
    """只读预检查：先验证所有历史和输入，留出一条新实验历史的位置，再允许外发。"""
    output = _directory(output)
    reserved = _directory(store.TEST_ROOT / "experiments/harness-v1/_execution-leases")
    if output.exists():
        raise FileExistsError("OUTPUT_ALREADY_EXISTS")
    if output.is_relative_to(reserved) or reserved.is_relative_to(output):
        raise ValueError("CYCLE_RESERVED_NAMESPACE")
    experiment_dirs = list(experiment_dirs)
    if previous_cycle is not None:
        previous_cycle = _directory(previous_cycle)
        if output.is_relative_to(previous_cycle) or previous_cycle.is_relative_to(output):
            raise ValueError("CYCLE_OUTPUT_OVERLAPS_PARENT")
        parent = inspect_cycle(previous_cycle)
        if parent["stage"] in {"UNFINALIZED", "INVALID_CYCLE"} or not parent.get("next_feedback_source"):
            raise ValueError("CYCLE_PARENT_NOT_READY")
        if parent["source"] != source:
            raise ValueError("CYCLE_PARENT_SOURCE_MISMATCH")
        old_index, _ = store._read(store.TEST_ROOT / parent["next_feedback_source"] / "source-index.json")
        inherited = [store.TEST_ROOT / s["source"] for s in old_index.get("experiment_sources", [])]
        experiment_dirs = list(dict.fromkeys(_directory(p) for p in [*inherited, *experiment_dirs]))
    if source not in {"live", "fake"} or len(experiment_dirs) > 9:
        raise ValueError("CYCLE_HISTORY_OR_SOURCE_INVALID")
    for path in experiment_dirs:
        path = _directory(path)
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError("CYCLE_OUTPUT_OVERLAPS_HISTORY")
    base, index = store.build_feedback(dataset_path=dataset_path, baseline_path=baseline_path, history_paths=history_paths)
    packet, extended_index = extend_feedback(base, index, experiment_dirs, source=source)
    request_payload(packet)  # 输入过大即停止，不能悄悄删除无效历史或截断后付费。
    cases = load_cases(store._dataset_file(dataset_path))
    if len(cases) != 3:
        raise ValueError("THREE_FROZEN_SINGLE_BATCH_CASES_REQUIRED")
    baseline = next(r for r in packet.reports if r.report_ref == packet.baseline_ref)
    profile = ExperimentProfile.model_validate(baseline.budget.model_dump())
    raw_baseline, _ = store._read(store._dataset_file(baseline_path))
    if runtime_config(_profile_settings(settings, profile)) != raw_baseline["runtime_config"]:
        raise ValueError("CYCLE_RUNTIME_DIFFERS_FROM_BASELINE")
    for case in cases:
        batches, truncated = SemanticChecker(client=None, max_input_chars=profile.max_input_chars)._batches(case.package)
        if truncated or len(batches) != 1:
            raise ValueError("CYCLE_SINGLE_BATCH_REQUIRED")
    if source == "live" and not settings.llm_enabled:
        raise ValueError("CYCLE_LLM_DISABLED")
    return {"base": base, "base_index": index, "packet": packet, "index": extended_index,
            "output": output, "authorization": authorization, "experiment_dirs": experiment_dirs}


def _request_count(output: Path) -> int:
    """从请求前的持久化记录计数；即使无最终响应，也视为可能已消费预算。"""
    return int((output / "proposal/invocation-started.json").exists()) + sum(
        len(list((output / "execution" / arm / "calls").glob("*.started.json")))
        for arm in ("control", "candidate"))


def run_cycle(*, dataset_path: Path, baseline_path: Path, history_paths: list[Path],
              experiment_dirs: list[Path], output: Path, settings, authorization: ExecutionAuthorization,
              source="live", advisor_client=None, client_factory=None, clock=time.monotonic,
              previous_cycle: Path | None = None) -> dict:
    """只尝试一个建议；NO_CHANGE/拒绝/失败即止步，不循环调参，不自动采纳。"""
    started = clock()
    context = check_cycle(dataset_path=dataset_path, baseline_path=baseline_path, history_paths=history_paths,
        experiment_dirs=experiment_dirs, output=output, settings=settings, authorization=authorization, source=source,
        previous_cycle=previous_cycle)
    if (source == "fake") != (advisor_client is not None and client_factory is not None):
        raise ValueError("CYCLE_CLIENT_SOURCE_INVALID")
    if source == "live" and (advisor_client is not None or client_factory is not None):
        raise ValueError("CYCLE_LIVE_CLIENT_INJECTION_FORBIDDEN")
    output = context["output"]
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700);output.mkdir(mode=0o700, exist_ok=False)
    events = output / "events";events.mkdir(mode=0o700)
    sequence = 0

    def elapsed():
        return int((clock() - started) * 1000)

    def event(stage):
        nonlocal sequence
        sequence += 1
        write_new_json(events / f"{sequence:03d}.json", {"stage": stage, "active_duration_ms": elapsed(),
                                                       "requests_used": _request_count(output)})

    cycle = {"schema_version": "harness-cycle-v1", "source": source,
             "dataset_sha256": context["packet"].dataset_sha256,
             "annotation_sha256": context["packet"].annotation_sha256,
             "baseline_ref": context["packet"].baseline_ref,
             "authorization": authorization.model_dump(), "max_candidates": 1,
             "parent_cycle_source": str(_directory(previous_cycle).relative_to(store.TEST_ROOT)) if previous_cycle else None,
             "history_before": [h.experiment_ref for h in context["packet"].experiment_history],
             "service_code_sha256": digest({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ("service.py", "history.py", "policy.py", "optimizer.py", "executor.py", "comparator.py")})}
    write_new_json(output / "cycle.json", cycle)
    stage, stop_reason, comparison = "FAILED", None, None
    next_feedback = output / "feedback"
    history_after = len(context["packet"].experiment_history)
    try:
        save_feedback(context["packet"], context["index"], output / "feedback")
        event("FEEDBACK_READY")
        if elapsed() + (TIMEOUT_SECONDS + 2) * 1000 > authorization.max_active_seconds * 1000:
            raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
        event("PROPOSING")
        transport = None
        if source == "live":
            advisor_client = build_optimizer_client(settings, context["packet"])
            transport = ProcessTransport({"api_key": advisor_client.api_key, "base_url": advisor_client.base_url,
                                          "model": advisor_client.model}, TIMEOUT_SECONDS)
        proposed = propose(feedback_dir=output / "feedback", output=output / "proposal",
                           client=advisor_client, source=source, request_transport=transport)
        stage = proposed["stage"]
        event(stage)
        if stage != "VALIDATED":
            # 总回执直接呈现终止原因，避免用户必须打开子目录才能知道为何未复测。
            validation, _ = store._read(output / "proposal/validation.json")
            stop_reason = validation["reason_code"]
        if stage == "VALIDATED":
            if _request_count(output) >= authorization.max_total_requests:
                raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
            if elapsed() >= authorization.max_active_seconds * 1000:
                raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
            invocation, _ = store._read(output / "proposal/invocation.json")
            event("EXPERIMENT_RUNNING")
            executed = run_experiment(feedback_dir=output / "feedback", proposal_dir=output / "proposal",
                output=output / "execution", settings=settings, authorization=authorization, source=source,
                client_factory=client_factory, clock=clock,
                active_before_execution_ms=max(elapsed(), invocation["duration_ms"]))
            comparison = executed["comparison"]
            stage = comparison if executed["stage"] == "COMPLETED" else executed["stage"]
            execution_summary, _ = store._read(output / "execution/run-summary.json")
            stop_reason = execution_summary["stop_reason"]
            if comparison == "INVALID_EXPERIMENT":
                stop_reason = "EXPERIMENT_VALIDATION_FAILED"
            event(stage)
            if comparison != "INVALID_EXPERIMENT":
                new_packet, new_index = extend_feedback(context["base"], context["base_index"],
                    [*context["experiment_dirs"], output / "execution"], source=source)
                next_feedback = output / "feedback-next"
                save_feedback(new_packet, new_index, next_feedback)
                history_after = len(new_packet.experiment_history)
                event("HISTORY_RECORDED")
    except ExperimentStopped as exc:
        stage, stop_reason = "STOPPED", str(exc)
        event(stage)
    except KeyboardInterrupt:
        stage, stop_reason = "INTERRUPTED", "USER_INTERRUPTED"
        event(stage)
    except Exception:
        stage, stop_reason = "FAILED", "CYCLE_STAGE_FAILED"
        event(stage)
    used = _request_count(output)
    if used > authorization.max_total_requests:
        stage, stop_reason = "FAILED", "CYCLE_REQUEST_BUDGET_INVALID"
    if elapsed() > authorization.max_active_seconds * 1000 and stage not in {"STOPPED", "INTERRUPTED", "FAILED"}:
        stage, stop_reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    result = {"stage": stage, "source": source, "comparison": comparison,
              "total_request_attempts": used, "model_calls": used if source == "live" else 0,
              "fake_calls": used if source == "fake" else 0, "active_duration_ms": elapsed(),
              "stop_reason": stop_reason, "history_count_before": len(context["packet"].experiment_history),
              "history_count_after": history_after,
              "next_feedback_source": str(next_feedback.relative_to(store.TEST_ROOT)) if next_feedback.exists() else None,
              "automatically_adopted": False}
    write_new_json(output / "cycle-summary.json", result)
    integrity = {str(p.relative_to(output)): digest(store._read(p)[0]) for p in output.rglob("*.json")}
    write_new_json(output / "integrity.json", integrity)
    write_new_json(output / "manifest.json", result)
    return result


def inspect_cycle(directory: Path) -> dict:
    """只读检查完成或中断状态，不恢复请求；未完成清单不冒充成功闭环。"""
    try:
        directory = _directory(directory)
        if not directory.is_dir():
            raise ValueError("CYCLE_NOT_FOUND")
        if any(p.is_symlink() for p in directory.rglob("*")):
            raise ValueError("CYCLE_SYMLINK_FORBIDDEN")
        if not (directory / "manifest.json").exists():
            return {"stage": "UNFINALIZED", "total_request_attempts": _request_count(directory),
                    "auto_resume": False}
        integrity, _ = store._read(directory / "integrity.json")
        actual = {str(p.relative_to(directory)): digest(store._read(p)[0]) for p in directory.rglob("*.json")
                  if p not in {directory / "integrity.json", directory / "manifest.json"}}
        manifest, _ = store._read(directory / "manifest.json")
        summary, _ = store._read(directory / "cycle-summary.json")
        if actual != integrity or summary != manifest or manifest["total_request_attempts"] != _request_count(directory):
            raise ValueError("CYCLE_ARTIFACT_CHANGED")
        if (directory / "feedback/manifest.json").exists():
            load_feedback(directory / "feedback")
        if manifest.get("next_feedback_source"):
            load_feedback(store.TEST_ROOT / manifest["next_feedback_source"])
        return manifest
    except Exception:
        return {"stage": "INVALID_CYCLE", "reason_code": "CYCLE_VALIDATION_FAILED", "auto_resume": False}
