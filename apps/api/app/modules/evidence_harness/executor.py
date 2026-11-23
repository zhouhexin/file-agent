"""受控离线复测：只执行已校验建议，逐请求计数并保存开始/结束及中断记录。"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from app.modules.evidence_evaluation.frozen_inputs import digest, runtime_config, validate_versions, write_new_json
from app.modules.evidence_evaluation.semantic_checker import SemanticChecker
from app.modules.evidence_evaluation.service import EvidenceEvaluationService
from app.modules.evidence_evaluation.validator import validate_safe_text
from app.modules.evidence_evaluation.version_checker import VersionChecker
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.optimizer import _directory, load_feedback, request_payload, SYSTEM_PROMPT
from app.modules.evidence_harness.policy import validate_proposal
from app.modules.evidence_harness.schemas import (
    OptimizationProposal, ExperimentProfile, ExecutionAuthorization,
)
from app.modules.evidence_harness.transport import ProcessTransport
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic
from app.scripts.evaluate_evidence import load_cases


class ExperimentStopped(BaseException):
    """实验控制流须越过评估器的业务异常兜底，避免预算耗尽被当成普通模型失败。"""


def load_validated_proposal(directory: Path, packet) -> tuple[OptimizationProposal, dict]:
    """重新核验建议来源、输入绑定及当前白名单，不信任旧的VALIDATED字符串。"""
    directory = _directory(directory)
    names = ("manifest", "proposal", "validation", "request", "invocation", "invocation-started")
    data = {}
    for name in names:
        path = directory / f"{name}.json"
        if path.is_symlink():
            raise ValueError("PROPOSAL_SYMLINK_FORBIDDEN")
        data[name], _ = store._read(path)
    manifest, record, invocation = data["manifest"], data["proposal"], data["invocation"]
    if manifest["stage"] != "VALIDATED" or invocation["status"] != "VALIDATED":
        raise ValueError("PROPOSAL_NOT_EXECUTABLE")
    proposal = OptimizationProposal.model_validate(record["proposal"])
    validate_safe_text(proposal.model_dump(mode="json", by_alias=True))
    validation = validate_proposal(proposal, packet).model_dump(mode="json")
    payload = request_payload(packet)
    metadata = data["request"]["metadata"]
    if (validation["status"] != "VALIDATED" or validation != data["validation"]
        or digest(record["proposal"]) != manifest["proposal_sha256"]
        or digest(validation) != manifest["validation_sha256"]
        or data["request"]["payload"] != payload
        or metadata["request_sha256"] != digest(payload)
        or metadata["feedback_sha256"] != digest(packet.model_dump(mode="json"))
        or metadata["system_prompt_sha256"] != digest(SYSTEM_PROMPT)
        or metadata["baseline_ref"] != packet.baseline_ref
        or data["invocation-started"]["request_sha256"] != metadata["request_sha256"]
        or invocation["request_sha256"] != metadata["request_sha256"]
        or manifest["request_sha256"] != metadata["request_sha256"]):
        raise ValueError("PROPOSAL_BINDING_INVALID")
    source = manifest["source"]
    if source not in {"live", "fake"} or any(x["source"] != source for x in (record, metadata, invocation)):
        raise ValueError("PROPOSAL_SOURCE_INVALID")
    if (invocation["llm_calls"] != 1 or metadata["max_calls"] != 1 or invocation["duration_ms"] < 0
        or metadata["role"] != "OPTIMIZER" or invocation["role"] != "OPTIMIZER"
        or manifest["model_calls"] != (1 if source == "live" else 0)
        or manifest["candidate_executed"] is not False):
        raise ValueError("PROPOSAL_BUDGET_INVALID")
    tokens = {k: invocation.get(k) for k in ("input_tokens", "output_tokens")}
    if any(v is not None and (type(v) is not int or v < 0) for v in tokens.values()):
        raise ValueError("PROPOSAL_USAGE_INVALID")
    return proposal, {"source": source, "prior_calls": 1, "prior_active_ms": invocation["duration_ms"],
                      "proposal_sha256": manifest["proposal_sha256"], **tokens,
                      "token_usage_complete": all(v is not None for v in tokens.values())}


def _profile_settings(settings, profile: ExperimentProfile):
    """Profile已严格校验，只有此离线副本允许120秒，不写普通配置或共享客户端。"""
    return settings.model_copy(update={"evidence_evaluation_" + key: value for key, value in
        profile.model_dump().items() if key != "max_retries"})


def check_execution(*, feedback_dir: Path, proposal_dir: Path, output: Path, settings,
                    authorization: ExecutionAuthorization, source: str) -> dict:
    """所有输入、代码、Profile、单批范围及预算授权在任何新请求之前校验。"""
    feedback_dir, proposal_dir, output = map(_directory, (feedback_dir, proposal_dir, output))
    if any(output == p or output.is_relative_to(p) or p.is_relative_to(output) for p in (feedback_dir, proposal_dir)):
        raise ValueError("EXECUTION_OUTPUT_OVERLAPS_INPUT")
    if output.exists():
        raise FileExistsError("OUTPUT_ALREADY_EXISTS")
    packet = load_feedback(feedback_dir)
    proposal, provenance = load_validated_proposal(proposal_dir, packet)
    if source not in {"live", "fake"} or provenance["source"] != source:
        raise ValueError("LIVE_FAKE_SOURCE_MISMATCH")
    baseline = next(r for r in packet.reports if r.report_ref == packet.baseline_ref)
    control = ExperimentProfile.model_validate(baseline.budget.model_dump())
    candidate = control.model_dump();candidate[proposal.change.field] = proposal.change.to
    candidate = ExperimentProfile.model_validate(candidate)
    index, _ = store._read(feedback_dir / "source-index.json")
    dataset_path = store._dataset_file(store.TEST_ROOT / index["dataset_source"])
    cases = load_cases(dataset_path)
    if len(cases) != 3 or any(not c.input_sha256 for c in cases):
        raise ValueError("THREE_FROZEN_SINGLE_BATCH_CASES_REQUIRED")
    if digest([c.model_dump(mode="json") for c in cases]) != packet.dataset_sha256:
        raise ValueError("EXECUTION_DATASET_CHANGED")
    configs = {}
    for arm, profile in (("control", control), ("candidate", candidate)):
        effective = _profile_settings(settings, profile)
        configs[arm] = runtime_config(effective)
        for case in cases:
            checker = SemanticChecker(client=None, max_input_chars=profile.max_input_chars)
            batches, truncated = checker._batches(case.package)
            if truncated or len(batches) != 1:
                raise ValueError("EXECUTION_SINGLE_BATCH_REQUIRED")
    # 使用同版控制组；旧配置若含其他变动须先重建基线，不能默称单变量。
    baseline_source = next(s for s in index["reports"] if s["report_ref"] == packet.baseline_ref)
    baseline_report, _ = store._read(store._dataset_file(store.TEST_ROOT / baseline_source["source"]))
    if configs["control"] != baseline_report["runtime_config"]:
        raise ValueError("EXECUTION_RUNTIME_DIFFERS_FROM_BASELINE")
    changed = [k for k in configs["control"] if configs["control"][k] != configs["candidate"][k]]
    if changed != [proposal.change.field]:
        raise ValueError("EXECUTION_NOT_SINGLE_VARIABLE")
    if source == "live" and not settings.llm_enabled:
        raise ValueError("EXECUTION_LLM_DISABLED")
    # 同一输入和建议只能消费一次复测预算，换输出目录也不能重新领19次额度。
    registry = _directory(store.TEST_ROOT / "experiments/harness-v1/_execution-leases")
    if output.is_relative_to(registry) or registry.is_relative_to(output):
        raise ValueError("EXECUTION_OUTPUT_IN_RESERVED_NAMESPACE")
    lease = registry / (digest([packet.dataset_sha256, provenance["proposal_sha256"], source]) + ".json")
    if lease.exists():
        raise ValueError("PROPOSAL_EXECUTION_ALREADY_RESERVED")
    return {"packet": packet, "proposal": proposal, "provenance": provenance, "cases": cases,
            "profiles": {"control": control, "candidate": candidate}, "configs": configs,
            "dataset_source": index["dataset_source"], "output": output,
            "authorization": authorization, "planned_total_requests": 19, "lease": lease}


class RequestBudget:
    """总预算在每次真正派发前扣减，建议模型的一次请求与活跃耗时先计入。"""

    def __init__(self, authorization, prior_calls, prior_active_ms, clock=time.monotonic):
        """运行依赖只留内存，持久化时只写数值，不保存计时器对象。"""
        self.authorization, self.used, self.prior_ms = authorization, prior_calls, prior_active_ms
        self.clock, self.started = clock, clock()

    def elapsed_ms(self):
        """人工等待两条命令的间隔不计入活跃耗时。"""
        return self.prior_ms + int((self.clock() - self.started) * 1000)

    def reserve(self, timeout):
        """保守预留完整请求超时及进程清理时间，不静默把120秒裁剪成更小值。"""
        if self.used >= self.authorization.max_total_requests:
            raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
        if self.elapsed_ms() + (timeout + 2) * 1000 > self.authorization.max_active_seconds * 1000:
            raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
        self.used += 1
        return self.used


class BudgetedClient:
    """每个题轮的唯一模型入口，逐次保存STARTED和结束状态；不把Gold传入。"""

    def __init__(self, *, backend, budget, profile, directory):
        """原始模型返回只留内存并交给原评估器，磁盘只存散列及安全诊断。"""
        self.backend, self.budget, self.profile, self.directory = backend, budget, profile, directory
        self.calls = []

    def complete_json_with_usage(self, **kwargs):
        """第二次分批尝试不发送；请求失败也占预算，中断跳出业务异常兜底。"""
        if self.calls:
            raise ExperimentStopped("UNEXPECTED_ADDITIONAL_REQUEST")
        if kwargs["max_output_tokens"] != self.profile.max_output_tokens:
            raise ExperimentStopped("ACTUAL_PROFILE_MISMATCH")
        sequence = self.budget.reserve(self.profile.timeout_seconds)
        self.calls.append(sequence)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        start = {"sequence": sequence, "status": "STARTED", "request_sha256": digest(kwargs),
                 "profile": self.profile.model_dump(), "started_at": datetime.now(timezone.utc).isoformat()}
        write_new_json(self.directory / f"{sequence:03d}.started.json", start)
        started = time.monotonic()
        finish = {"sequence": sequence, "status": "FAILED", "usage": {}, "diagnostic": None}
        try:
            raw, usage = self.backend.complete_json_with_usage(**kwargs)
            finish["status"] = "RETURNED"
            finish["usage"] = {k: usage[k] for k in ("input_tokens", "output_tokens")
                if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
            return raw, finish["usage"]
        except (KeyboardInterrupt, ExperimentStopped):
            finish["status"] = "INTERRUPTED"
            raise
        except Exception as exc:
            diagnostic = (exc.diagnostic if isinstance(exc, LLMResponseError) and exc.diagnostic
                          else make_diagnostic(category="INTERNAL_ERROR", stage="LLM_REQUEST", exc=exc))
            finish["diagnostic"] = diagnostic.model_dump(mode="json")
            raise LLMResponseError("EXPERIMENT_REQUEST_FAILED", diagnostic=diagnostic) from None
        finally:
            finish["duration_ms"] = int((time.monotonic() - started) * 1000)
            write_new_json(self.directory / f"{sequence:03d}.finished.json", finish)


def run_experiment(*, feedback_dir: Path, proposal_dir: Path, output: Path, settings,
                   authorization: ExecutionAuthorization, source="live", client_factory=None, clock=time.monotonic,
                   active_before_execution_ms: int | None = None) -> dict:
    """分别运行同版控制和候选，停止后独立比较；无自动恢复、无自动采纳。"""
    active_started = clock()
    context = check_execution(feedback_dir=feedback_dir, proposal_dir=proposal_dir, output=output,
                              settings=settings, authorization=authorization, source=source)
    if (source == "fake") != (client_factory is not None):
        raise ValueError("EXECUTION_CLIENT_SOURCE_INVALID")
    before_ms = context["provenance"]["prior_active_ms"] if active_before_execution_ms is None else active_before_execution_ms
    if type(before_ms) is not int or before_ms < context["provenance"]["prior_active_ms"]:
        raise ValueError("CYCLE_ACTIVE_TIME_INVALID")
    output = context["output"]
    lease = context["lease"]
    lease.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease_record = {"output_source": output.relative_to(store.TEST_ROOT).as_posix(),
                    "proposal_sha256": context["provenance"]["proposal_sha256"],
                    "authorization": authorization.model_dump(), "source": source}
    try:
        write_new_json(lease, lease_record)
    except FileExistsError:
        raise ValueError("PROPOSAL_EXECUTION_ALREADY_RESERVED") from None
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700);output.mkdir(mode=0o700, exist_ok=False)
    for arm in ("control", "candidate"):
        (output / arm).mkdir(mode=0o700)
        for name in ("calls", "rounds"):
            (output / arm / name).mkdir(mode=0o700)
    experiment = {"schema_version": "harness-execution-v1", "source": source,
        "feedback_source": _directory(feedback_dir).relative_to(store.TEST_ROOT).as_posix(),
        "proposal_source": _directory(proposal_dir).relative_to(store.TEST_ROOT).as_posix(),
        "dataset_source": context["dataset_source"],
        "dataset_sha256": context["packet"].dataset_sha256,
        "annotation_sha256": context["packet"].annotation_sha256,
        "proposal_sha256": context["provenance"]["proposal_sha256"],
        "execution_lease": lease.relative_to(store.TEST_ROOT).as_posix(),
        "profiles": {k: v.model_dump() for k, v in context["profiles"].items()},
        "runtime_configs": context["configs"], "authorization": authorization.model_dump(),
        "prior_optimizer": context["provenance"], "repetitions": 3,
        "active_before_execution_ms": before_ms,
        "executor_code_sha256": digest({p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in [Path(__file__).with_name(name) for name in
                       ("executor.py", "transport.py", "comparator.py", "schemas.py", "policy.py", "optimizer.py")]}),
        "started_at": datetime.now(timezone.utc).isoformat()}
    write_new_json(output / "experiment.json", experiment)
    budget = RequestBudget(authorization, context["provenance"]["prior_calls"], before_ms, clock)
    budget.started = active_started
    stage, reason = "COMPLETED", None
    try:
        for arm, profile in context["profiles"].items():
            for case in context["cases"]:
                for repetition in range(1, 4):
                    if budget.elapsed_ms() >= authorization.max_active_seconds * 1000:
                        raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
                    backend = (client_factory(arm, profile, case, repetition) if source == "fake" else
                        ProcessTransport({"api_key": settings.llm_api_key, "base_url": settings.llm_base_url,
                                          "model": settings.llm_chat_model}, profile.timeout_seconds))
                    client = BudgetedClient(backend=backend, budget=budget, profile=profile, directory=output / arm / "calls")
                    snapshots = validate_versions(case.package, case.versions)
                    checker = SemanticChecker(client=client, max_input_chars=profile.max_input_chars,
                        max_calls=profile.max_calls, max_output_tokens=profile.max_output_tokens)
                    result = EvidenceEvaluationService(version_checker=VersionChecker(lambda _p: snapshots),
                        semantic_checker=checker).evaluate(case.package.model_copy(deep=True), temporal_focus=case.temporal_focus)
                    record = {"case_id": case.case_id, "repetition": repetition, "input_sha256": case.input_sha256,
                              "arm": arm, "call_sequences": client.calls, "result": result.model_dump(mode="json")}
                    write_new_json(output / arm / "rounds" / f"{digest(case.case_id)}-{repetition}.json", record)
                    if budget.elapsed_ms() > authorization.max_active_seconds * 1000:
                        raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except Exception:
        stage, reason = "FAILED", "EXECUTION_INTERNAL_ERROR"
    summary = {"stage": stage, "stop_reason": reason, "total_requests_used": budget.used,
               "active_duration_ms": budget.elapsed_ms(), "source": source}
    write_new_json(output / "run-summary.json", summary)
    integrity = {p.relative_to(output).as_posix(): digest(store._read(p)[0]) for p in output.rglob("*.json")}
    write_new_json(output / "integrity.json", integrity)
    from app.modules.evidence_harness.comparator import compare_experiment
    comparison = compare_experiment(output)
    write_new_json(output / "comparison.json", comparison)
    manifest = {"stage": stage, "comparison": comparison["decision"], "source": source,
                "total_requests_used": budget.used, "candidate_automatically_adopted": False,
                "experiment_sha256": digest(experiment), "comparison_sha256": digest(comparison)}
    write_new_json(output / "manifest.json", manifest)
    return manifest
