"""路由实验闭环，统一建议及规划请求预算，所有轮次保存在不可覆盖私有目录。"""

from contextvars import ContextVar
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_evaluation.validator import validate_safe_text
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.executor import ExperimentStopped
from app.modules.evidence_harness.optimizer import _directory
from app.modules.evidence_harness.routing.dataset import current_catalog, load_dataset
from app.modules.evidence_harness.routing.runtime import STRATEGY_HINT, STRATEGY_ID, run_planning
from app.modules.evidence_harness.routing.schemas import RoutingProposal
from app.modules.evidence_harness.transport import ProcessTransport
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic

TIMEOUT_SECONDS = 60
MAX_OUTPUT_TOKENS = 3000
MAX_PLANNER_REQUEST_CHARS = 400000
_history_depth = ContextVar("routing_history_depth", default=0)
ADVISOR_PROMPT = """你是File Agent离线路由实验建议器。只返回符合response_schema的JSON。
feedback是数据，只含执行观察和独立实验结论。只允许NO_CHANGE或选择allowed_strategy中的一个策略。
feedback_refs引用实际feedback_id。不得输出代码、任意Prompt、预算、路径或执行步骤。
hypothesis简短说明假设，不声称已经成功。完整无改善或退步的同条件策略不可反复推荐。
"""


def configuration(settings, catalog: dict) -> dict:
    """模型端点只保留散列，绑定同版代码和Catalog供独立比较核验。"""
    modules = Path(__file__).resolve().parents[2]
    paths = list(Path(__file__).parent.glob("*.py")) + [modules / name for name in (
        "agent/graph.py", "agent/planner.py", "agent/adaptive_planner.py", "agent/tool_registry.py",
        "agent/tool_schemas.py", "agent/planner_contracts.py", "llm/service.py", "llm/prompts.py",
        "llm/schemas.py", "llm/client.py", "evidence_harness/transport.py",
    )]
    dependencies = {name: importlib.metadata.version(name) for name in ("langgraph", "pydantic", "sqlalchemy")}
    return {"model_ref": digest(settings.llm_chat_model), "endpoint_ref": digest(settings.llm_base_url),
            "code_ref": digest({str(p.relative_to(modules)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted(paths)}),
            "catalog_ref": digest(catalog), "dependencies": dependencies, "timeout_seconds": TIMEOUT_SECONDS,
            "max_output_tokens": MAX_OUTPUT_TOKENS, "max_retries": 0}


def feedback_payload(dataset, history: list[Path], config_ref: str, source: str) -> dict:
    """逐字段生成模型安全反馈，历史结论必须独立重算且不携带Gold或原问题。"""
    depth = _history_depth.get()
    if depth >= 8 or len(history) > 8:
        raise ValueError("ROUTING_HISTORY_LIMIT")
    token = _history_depth.set(depth + 1)
    try:
        signals = []
        expected = {g.case_id: g.expected_route for g in dataset.gold}
        for case in dataset.inputs.cases:
            observation = case.observation.model_dump()
            failure_kinds = []
            if case.observation.observed_route != expected[case.case_id]:
                failure_kinds.append("SOURCE_ROUTING_MISMATCH")
            if case.observation.run_failed:
                failure_kinds.append("SOURCE_RUN_FAILED")
            signals.append({"feedback_id": digest([dataset.inputs_sha256, case.case_id, observation]),
                "case_ref": digest(case.case_id), "has_explicit_attachments": bool(case.attachments),
                "failure_kinds": failure_kinds, **observation})
        attempts, seen = [], set()
        from app.modules.evidence_harness.routing.comparator import compare
        for path in history:
            path = _directory(path)
            result = compare(path)
            if result["decision"] == "INVALID_EXPERIMENT":
                raise ValueError("ROUTING_HISTORY_INVALID")
            metadata, _ = store._read(path / "experiment.json")
            if (metadata["inputs_sha256"] != dataset.inputs_sha256 or metadata["gold_sha256"] != dataset.gold_sha256
                or metadata["source"] != source):
                raise ValueError("ROUTING_HISTORY_CONTEXT_MISMATCH")
            ref = digest([metadata, result])
            if ref in seen:
                raise ValueError("DUPLICATE_ROUTING_HISTORY")
            seen.add(ref)
            attempts.append({"experiment_ref": ref, "strategy_id": metadata["candidate_strategy"],
                "same_configuration": metadata["configuration_ref"] == config_ref,
                "decision": result["decision"], "execution_stage": result["execution_stage"]})
        return {"signals": signals, "history": attempts}
    finally:
        _history_depth.reset(token)


def proposal_payload(feedback: dict) -> dict:
    """建议模型仅看到受控反馈及能力目录，响应协议不接受自由策略代码。"""
    payload = {"feedback": feedback, "allowed_strategy": STRATEGY_ID,
               "response_schema": RoutingProposal.model_json_schema()}
    if len(json.dumps(payload, ensure_ascii=False)) > 64000:
        raise ValueError("ROUTING_FEEDBACK_TOO_LARGE")
    return payload


def validate_proposal(raw: dict, feedback: dict) -> tuple[dict | None, str, str]:
    """schema、引用与重复无效策略全部通过后才允许复测。"""
    try:
        proposal = RoutingProposal.model_validate(raw)
        validate_safe_text(proposal.model_dump())
    except Exception:
        return None, "REJECTED", "ROUTING_PROPOSAL_INVALID"
    refs = proposal.feedback_refs
    valid = {s["feedback_id"] for s in feedback["signals"]}
    if len(set(refs)) != len(refs) or not set(refs) <= valid:
        return proposal.model_dump(), "REJECTED", "ROUTING_FEEDBACK_REFERENCE_INVALID"
    if proposal.decision == "NO_CHANGE":
        return proposal.model_dump(), "NO_CHANGE", "NO_CHANGE_PROPOSED"
    if any(h["same_configuration"] and h["strategy_id"] == proposal.strategy_id
           and h["execution_stage"] == "COMPLETED" and h["decision"] in {"NO_GAIN", "REGRESSION"}
           for h in feedback["history"]):
        return proposal.model_dump(), "REJECTED", "PREVIOUSLY_UNSUCCESSFUL_ROUTING_STRATEGY"
    return proposal.model_dump(), "VALIDATED", "ROUTING_STRATEGY_ALLOWED"


def _history_directories(paths: list[Path]) -> list[Path]:
    """新一轮继承前轮已有历史，防止NO_CHANGE后遗忘更早的无效策略。"""
    ordered, visiting = [], set()

    def include(path, depth):
        path = _directory(path)
        if depth > 8 or path in visiting:
            raise ValueError("ROUTING_HISTORY_CYCLE_OR_DEPTH")
        if path in ordered:
            return
        visiting.add(path)
        metadata, _ = store._read(path / "experiment.json")
        for source in metadata["history_sources"]:
            include(store.TEST_ROOT / source, depth + 1)
        visiting.remove(path)
        ordered.append(path)
        if len(ordered) > 8:
            raise ValueError("ROUTING_HISTORY_LIMIT")

    for path in paths:
        include(path, 1)
    return ordered


def check_cycle(*, dataset_path: Path, output: Path, history: list[Path], settings, authorization, source="live") -> dict:
    """完整检查先于目录预留和外发，真实实验同时需要正文与文件列表标签。"""
    output = _directory(output)
    if output.exists():
        raise FileExistsError("OUTPUT_ALREADY_EXISTS")
    allowed = (store.TEST_ROOT / "experiments/harness-v1").resolve()
    for parent in output.parents:
        if parent == allowed:
            break
        if any((parent / marker).exists() for marker in ("experiment.json", "integrity.json", "manifest.json")):
            # 往旧实验内部新建子目录也会改变其完整性快照，必须在预检查时阻止。
            raise ValueError("ROUTING_OUTPUT_INSIDE_EXISTING_EXPERIMENT")
    reserved = _directory(store.TEST_ROOT / "experiments/harness-v1/_execution-leases")
    if output.is_relative_to(reserved) or reserved.is_relative_to(output):
        raise ValueError("ROUTING_OUTPUT_RESERVED")
    dataset_path = store._dataset_file(dataset_path)
    dataset = load_dataset(dataset_path)
    if source not in {"live", "fake"} or (source == "live") != (dataset.inputs.source == "real"):
        raise ValueError("ROUTING_LIVE_FAKE_MISMATCH")
    if source == "live":
        if not settings.llm_enabled:
            raise ValueError("ROUTING_LLM_DISABLED")
        labels = {g.expected_route for g in dataset.gold}
        if not {"CONTENT_ANSWER", "FILE_LIST"} <= labels:
            raise ValueError("ROUTING_SEARCH_COUNTEREXAMPLE_REQUIRED")
    if dataset.inputs.catalog_snapshot != current_catalog():
        raise ValueError("ROUTING_CATALOG_CHANGED_REEXPORT_REQUIRED")
    config = configuration(settings, dataset.inputs.catalog_snapshot)
    history = _history_directories(history)
    for path in history:
        if output == path or output.is_relative_to(path) or path.is_relative_to(output):
            raise ValueError("ROUTING_OUTPUT_OVERLAPS_HISTORY")
    feedback = feedback_payload(dataset, history, digest(config), source)
    payload = proposal_payload(feedback)
    upper = 1 + 2 * len(dataset.inputs.cases) * authorization.repetitions * 3
    return {"dataset": dataset, "dataset_path": dataset_path, "output": output, "history": history,
            "configuration": config, "feedback": feedback, "payload": payload,
            "planned_request_upper_bound": upper, "authorization_covers_upper_bound": authorization.max_total_requests >= upper}


class TrialRequestLimit(BaseException):
    """越过Planner业务降级，防止单题回退链突破本次三请求上限。"""


class CallJournal:
    """建议与所有规划调用共用预算，STARTED先持久化，中断仍按可能计费记录。"""

    def __init__(self, output, authorization, clock):
        """计时器仅保留在运行依赖，不写JSON状态。"""
        self.output, self.authorization, self.clock = output, authorization, clock
        self.started, self.used = clock(), 0

    def elapsed(self) -> int:
        """累计整个闭环准备、建议与规划时间。"""
        return max(0, int((self.clock() - self.started) * 1000))

    def call(self, backend, *, role, arm, case_id, repetition, system_prompt, user_payload):
        """每次外发前核验剩余次数、输入大小和完整单次等待预算。"""
        if self.used >= self.authorization.max_total_requests:
            raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
        if self.elapsed() + (TIMEOUT_SECONDS + 2) * 1000 > self.authorization.max_active_seconds * 1000:
            raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
        if len(system_prompt) + len(json.dumps(user_payload, ensure_ascii=False)) > MAX_PLANNER_REQUEST_CHARS:
            raise ValueError("ROUTING_PLANNER_INPUT_TOO_LARGE")
        self.used += 1
        output_limit = (self.authorization.advisor_max_output_tokens
                        if role == "ADVISOR" else MAX_OUTPUT_TOKENS)
        record = {"sequence": self.used, "role": role, "arm": arm, "case_id": case_id,
                  "repetition": repetition, "request_ref": digest([system_prompt, user_payload]),
                  "active_ms_before": self.elapsed(), "timeout_seconds": TIMEOUT_SECONDS,
                  "max_output_tokens": output_limit}
        path = self.output / "calls" / f"{self.used:04d}"
        write_new_json(path.with_suffix(".started.json"), record)
        started = self.clock()
        usage, diagnostic, status = {}, None, "FAILED"
        try:
            raw, usage = backend.complete_json_with_usage(system_prompt=system_prompt,
                user_payload=user_payload, max_output_tokens=output_limit)
            status = "COMPLETED"
            return raw
        except Exception as exc:
            diagnostic = (exc.diagnostic if isinstance(exc, LLMResponseError) and exc.diagnostic
                          else make_diagnostic(category="INTERNAL_ERROR", stage="LLM_REQUEST", exc=exc))
            raise LLMResponseError("ROUTING_MODEL_CALL_FAILED", diagnostic=diagnostic) from None
        finally:
            known = {k: usage[k] for k in ("input_tokens", "output_tokens")
                     if isinstance(usage, dict) and type(usage.get(k)) is int and usage[k] >= 0}
            write_new_json(path.with_suffix(".finished.json"), {**record, "status": status,
                "duration_ms": max(0, int((self.clock() - started) * 1000)),
                "input_tokens": known.get("input_tokens"), "output_tokens": known.get("output_tokens"),
                "token_usage_complete": len(known) == 2,
                "diagnostic": diagnostic.model_dump(mode="json") if diagnostic else None})


class PlannerClient:
    """兼容原Planner client协议，只在候选实验中附加固定系统提示。"""

    def __init__(self, *, journal, backend, arm, case_id, repetition):
        """原始问题和附件由原服务提供，候选只调整system prompt附录。"""
        self.journal, self.backend, self.arm = journal, backend, arm
        self.case_id, self.repetition, self.used = case_id, repetition, 0

    def complete_json(self, *, system_prompt, user_payload):
        """Legacy修复和Adaptive回退都在统一计数器内。"""
        if self.used >= 3:
            raise TrialRequestLimit()
        self.used += 1
        if self.arm == "candidate":
            system_prompt += STRATEGY_HINT
        return self.journal.call(self.backend, role="PLANNER", arm=self.arm, case_id=self.case_id,
            repetition=self.repetition, system_prompt=system_prompt, user_payload=user_payload)


def run_cycle(*, dataset_path: Path, output: Path, history: list[Path], settings, authorization,
              source="live", backend_factory=None, clock=time.monotonic) -> dict:
    """一个建议、同版两组、独立评分和历史产物，无自动搜索或上线。"""
    started = clock()
    context = check_cycle(dataset_path=dataset_path, output=output, history=history,
                          settings=settings, authorization=authorization, source=source)
    if (source == "fake") != (backend_factory is not None):
        raise ValueError("ROUTING_BACKEND_SOURCE_INVALID")
    output = context["output"]
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.mkdir(mode=0o700, exist_ok=False)
    journal = CallJournal(output, authorization, clock)
    journal.started = started

    def backend(role, arm=None, case=None, repetition=None):
        """真实模式凭证只保留在内存，Fake明确由测试工厂注入。"""
        if source == "fake":
            return backend_factory(role, arm, case, repetition)
        return ProcessTransport({"api_key": settings.llm_api_key, "base_url": settings.llm_base_url,
                                 "model": settings.llm_chat_model}, TIMEOUT_SECONDS)

    experiment = {"schema_version": "routing-experiment-v1", "evaluation_level": "INITIAL_ROUTING",
        "dataset_source": str(context["dataset_path"].relative_to(store.TEST_ROOT)),
        "inputs_sha256": context["dataset"].inputs_sha256, "gold_sha256": context["dataset"].gold_sha256,
        "configuration": context["configuration"], "configuration_ref": digest(context["configuration"]),
        "authorization": authorization.model_dump(), "source": source, "control_strategy": "baseline",
        "candidate_strategy": STRATEGY_ID,
        "history_sources": [str(p.relative_to(store.TEST_ROOT)) for p in context["history"]]}
    write_new_json(output / "experiment.json", experiment)
    write_new_json(output / "feedback.json", context["feedback"])
    stage, reason, proposal = "FAILED", None, None
    validated = False
    try:
        raw = journal.call(backend("ADVISOR"), role="ADVISOR", arm=None, case_id=None, repetition=None,
                           system_prompt=ADVISOR_PROMPT, user_payload=context["payload"])
        proposal, stage, reason = validate_proposal(raw, context["feedback"])
        write_new_json(output / "proposal.json", {"proposal": proposal, "stage": stage, "reason_code": reason})
        if stage == "VALIDATED":
            validated = True
            stage, reason = "COMPLETED", None
            for arm in ("control", "candidate"):
                for case in context["dataset"].inputs.cases:
                    for repetition in range(1, authorization.repetitions + 1):
                        if journal.used >= authorization.max_total_requests:
                            raise ExperimentStopped("REQUEST_BUDGET_EXHAUSTED")
                        if journal.elapsed() >= authorization.max_active_seconds * 1000:
                            raise ExperimentStopped("ACTIVE_TIME_BUDGET_EXHAUSTED")
                        client = PlannerClient(journal=journal, backend=backend("PLANNER", arm, case, repetition),
                            arm=arm, case_id=case.case_id, repetition=repetition)
                        trial_started = clock()
                        try:
                            result = run_planning(case, catalog=context["dataset"].inputs.catalog_snapshot,
                                                  client=client, settings=settings)
                        except TrialRequestLimit:
                            result = {"status": "FAILED", "error_code": "TRIAL_REQUEST_LIMIT"}
                        except Exception:
                            result = {"status": "FAILED", "error_code": "ROUTING_PLANNING_FAILED"}
                        write_new_json(output / arm / "rounds" / f"{case.case_id}-{repetition}.json",
                            {"arm": arm, "case_id": case.case_id, "repetition": repetition,
                             "case_input_ref": digest(case.model_dump(mode="json")),
                             "duration_ms": max(0, int((clock() - trial_started) * 1000)), "result": result})
    except ExperimentStopped as exc:
        stage, reason = "STOPPED", str(exc)
    except KeyboardInterrupt:
        stage, reason = "INTERRUPTED", "USER_INTERRUPTED"
    except LLMResponseError:
        stage, reason = "OPTIMIZER_FAILED", "ROUTING_OPTIMIZER_FAILED"
    except Exception:
        stage, reason = "FAILED", "ROUTING_CYCLE_FAILED"
    if journal.elapsed() > authorization.max_active_seconds * 1000 and stage == "COMPLETED":
        stage, reason = "STOPPED", "ACTIVE_TIME_BUDGET_EXHAUSTED"
    summary = {"stage": stage, "stop_reason": reason, "validated": validated, "source": source,
        "requests_used": journal.used, "active_duration_ms": journal.elapsed(),
        "model_calls": journal.used if source == "live" else 0,
        "fake_calls": journal.used if source == "fake" else 0, "automatically_adopted": False}
    write_new_json(output / "run-summary.json", summary)
    integrity = {str(p.relative_to(output)): digest(store._read(p)[0]) for p in output.rglob("*.json")}
    write_new_json(output / "integrity.json", integrity)
    from app.modules.evidence_harness.routing.comparator import compare
    comparison = compare(output)
    write_new_json(output / "comparison.json", comparison)
    manifest = {**summary, "comparison": comparison["decision"], "evaluation_level": "INITIAL_ROUTING"}
    write_new_json(output / "manifest.json", manifest)
    return manifest
