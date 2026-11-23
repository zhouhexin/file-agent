"""路由最小版验收，使用实际规划节点和确定性Fake保护Gold、范围及预算。"""

from copy import deepcopy
from functools import lru_cache
import json
import os
from types import SimpleNamespace

import pytest

from app.core import logging as event_logging
from app.core.config import Settings
from app.modules.evidence_evaluation import frozen_inputs
from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness import feedback as store
from app.modules.evidence_harness.routing import dataset, runtime, service, comparator
from app.modules.evidence_harness.routing.schemas import (
    RoutingAuthorization, RoutingCase, RoutingGold, RoutingInputs, SourceSignal,
)
from app.modules.llm.client import LLMResponseError
from app.modules.llm.diagnostics import make_diagnostic


@pytest.fixture
def routing(tmp_path, monkeypatch):
    """构造包含正文任务及正常文件列表的冻结集，所有内容仅在隔离目录。"""
    root = tmp_path / "test"
    monkeypatch.setattr(store, "TEST_ROOT", root)
    monkeypatch.setattr(frozen_inputs, "TEST_ROOT", root)
    settings = Settings(database_url="postgresql://fake:fake@127.0.0.1/file_agent_unit_tests",
                        llm_enabled=True, llm_chat_model="ROUTING_FAKE_MODEL",
                        llm_base_url="http://fake.invalid/v1", llm_api_key="PRIVATE_KEY_SENTINEL",
                        log_dir=str(root / "logs"))
    monkeypatch.setattr(event_logging, "get_settings", lambda: settings)
    cases = [RoutingCase(case_id=cid, source_run_ref=digest(cid), question=question, adaptive_mode="legacy",
                        observation=SourceSignal(observed_route="FILE_LIST", entry_tool="managed-file-list",
                                                 evidence_answer_called=False, run_failed=False))
             for cid, question in (
                 ("C1", "请说明培训通知中记载的对象和时间，并引用原文。"),
                 ("N1", "给我培训通知的文件清单。"))]
    inputs = RoutingInputs(source="fake", cases=cases, catalog_snapshot=dataset.current_catalog(), source_catalog_refs=[])
    inputs_path = root / "datasets/routing-v1/inputs.json"
    write_new_json(inputs_path, inputs.model_dump(mode="json"))
    gold_path = inputs_path.with_name("gold.json")
    write_new_json(gold_path, {"cases": [RoutingGold(case_id=c.case_id,
        expected_route="CONTENT_ANSWER" if c.case_id == "C1" else "FILE_LIST",
        reason="GOLD_PRIVATE_SENTINEL", confirmed=True).model_dump() for c in cases]})
    output = inputs_path.with_name("cases.json")
    dataset.freeze(inputs_path=inputs_path, gold_path=gold_path, output=output)
    return SimpleNamespace(root=root, settings=settings, dataset=output, inputs=inputs_path, gold=gold_path)


def kwargs(routing, name="cycle", **updates):
    """每个实验使用独立输出，不覆盖现有冻结数据或历史。"""
    result = dict(dataset_path=routing.dataset, output=routing.root / f"experiments/harness-v1/{name}",
                  history=[], settings=routing.settings, authorization=RoutingAuthorization(), source="fake")
    result.update(updates)
    return result


def factory(kind="improve", requests=None, advisor_kind="valid"):
    """Fake依据任务身份及实验组返回可解释计划，不读取人工预期。"""
    requests = requests if requests is not None else []

    def build(role, arm, case, repetition):
        class Client:
            def complete_json_with_usage(self, **payload):
                """记录模型实际输入，用于验证Gold隔离和策略附录只进入候选。"""
                requests.append((role, arm, case.case_id if case else None, deepcopy(payload)))
                if kind == "interrupt" and len(requests) == 3:
                    raise KeyboardInterrupt
                if role == "ADVISOR":
                    if advisor_kind == "timeout":
                        raise LLMResponseError("private", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                    ref = payload["user_payload"]["feedback"]["signals"][0]["feedback_id"]
                    result = {"decision": "NO_CHANGE" if advisor_kind == "no_change" else "PROPOSE_EXPERIMENT",
                        "feedback_refs": [ref], "strategy_id": None if advisor_kind == "no_change" else runtime.STRATEGY_ID,
                        "hypothesis": "按正文任务意图检查路由"}
                    if advisor_kind == "invalid":
                        result["shell"] = "forbidden"
                    if advisor_kind == "foreign_ref":
                        result["feedback_refs"] = ["0" * 64]
                    return result, {"input_tokens": 3, "output_tokens": 5}
                if kind == "planner_timeout":
                    raise LLMResponseError("private", diagnostic=make_diagnostic(category="TIMEOUT", stage="LLM_REQUEST"))
                content = arm == "candidate" and (case.case_id == "C1" and kind != "no_gain" or kind == "regress")
                result = {"intent": "ANSWER_QUESTION_FROM_FILES" if content else "LIST_MANAGED_FILES",
                          "user_goal": case.question, "managed_query": "培训通知"}
                if content:
                    result["required_capabilities"] = ["evidence-answer"]
                if kind == "unknown_id" and content:
                    result["referenced_document_ids"] = ["FOREIGN_DOCUMENT"]
                return result, {"input_tokens": 11, "output_tokens": 7}
        return Client()
    return build


@pytest.mark.parametrize("kind,expected", [("improve", "IMPROVED"), ("no_gain", "NO_GAIN"),
                                           ("regress", "REGRESSION"), ("unknown_id", "REGRESSION")])
def test_real_planning_node_and_independent_comparison(routing, kind, expected):
    """同一规划节点复测两组，改善不能盖过正常搜索退步或越界ID。"""
    requests = []
    options = kwargs(routing)
    original = routing.dataset.read_bytes()
    result = service.run_cycle(**options, backend_factory=factory(kind, requests))
    assert result["stage"] == "COMPLETED" and result["comparison"] == expected
    assert result["model_calls"] == 0 and result["fake_calls"] == len(requests) == 5
    report = comparator.compare(options["output"])
    assert report["decision"] == expected and report["arms"]["control"]["planned"] == 2
    assert report["target_file_retrieval"] == report["answer_quality"] == "NOT_EXECUTED"
    assert original == routing.dataset.read_bytes() and not result["automatically_adopted"]
    for role, arm, cid, request in requests:
        text = json.dumps(request, ensure_ascii=False)
        assert "GOLD_PRIVATE_SENTINEL" not in text and '"expected_route"' not in text
        if role == "PLANNER":
            assert request["user_payload"]["attachments"] == []
            assert (runtime.STRATEGY_HINT in request["system_prompt"]) == (arm == "candidate")
        else:
            assert "培训对象" not in text and '"document_id"' not in text
            signals = request["user_payload"]["feedback"]["signals"]
            assert signals[0]["failure_kinds"] == ["SOURCE_ROUTING_MISMATCH"]
            assert signals[1]["failure_kinds"] == []
    if kind == "regress":
        assert report["regression_case_ids"] == ["N1"]
    if kind == "unknown_id":
        assert report["candidate_safety_failures"] == 1
    if os.name != "nt":  # POSIX mode bits do not describe Windows ACLs.
        assert all(p.stat().st_mode & 0o777 == 0o600 for p in options["output"].rglob("*.json"))


@pytest.mark.parametrize("kind,stage", [("no_change", "NO_CHANGE"), ("invalid", "REJECTED"),
                                       ("foreign_ref", "REJECTED"), ("timeout", "OPTIMIZER_FAILED")])
def test_early_terminal_advice_does_not_run_planning(routing, kind, stage):
    """非法建议、无调整和建议调用失败均只消耗一次请求。"""
    requests = []
    options = kwargs(routing)
    result = service.run_cycle(**options, backend_factory=factory(requests=requests, advisor_kind=kind))
    assert result["stage"] == stage and len(requests) == 1
    assert comparator.compare(options["output"])["decision"] == "NOT_RUN"
    assert not (options["output"] / "control").exists()


def test_partial_budget_counts_advisor_and_all_planned_slots(routing):
    """2次授权含建议，只能再做1次规划，未执行轮次保留在分母中。"""
    requests = []
    options = kwargs(routing, authorization=RoutingAuthorization(max_total_requests=2))
    result = service.run_cycle(**options, backend_factory=factory(requests=requests))
    report = comparator.compare(options["output"])
    assert result["stage"] == "STOPPED" and result["requests_used"] == len(requests) == 2
    assert report["decision"] == "INCONCLUSIVE"
    assert report["arms"]["candidate"]["planned"] == 2 and report["arms"]["candidate"]["recorded"] == 0


def test_interrupt_records_unknown_usage_and_is_not_resumed(routing):
    """人工中断保留发送记录，未返回的调用用量未知且新执行不能覆盖目录。"""
    requests = []
    options = kwargs(routing)
    result = service.run_cycle(**options, backend_factory=factory("interrupt", requests))
    assert result["stage"] == "INTERRUPTED" and result["requests_used"] == 3
    report = comparator.compare(options["output"])
    assert report["decision"] == "INCONCLUSIVE" and report["unknown_token_calls"] == 1
    with pytest.raises(FileExistsError):
        service.run_cycle(**options, backend_factory=factory(requests=requests))
    assert len(requests) == 3


def test_insufficient_wall_time_stops_before_advisor(routing):
    """完整单次等待预算不足时零外发，仍保留可读终止记录。"""
    requests = []
    options = kwargs(routing, authorization=RoutingAuthorization(max_active_seconds=1))
    result = service.run_cycle(**options, backend_factory=factory(requests=requests))
    assert result["stage"] == "STOPPED" and not requests
    assert comparator.compare(options["output"])["decision"] == "NOT_RUN"


def test_planner_gateway_failure_does_not_escape_budget_or_claim_zero_cost(routing):
    """真实降级分支参与复测，模型故障调用计次且未知Token保留。"""
    options = kwargs(routing)
    result = service.run_cycle(**options, backend_factory=factory("planner_timeout"))
    report = comparator.compare(options["output"])
    assert result["requests_used"] == 5 and report["failed_model_calls"] == 4
    assert report["unknown_token_calls"] == 4 and not report["token_usage_complete"]


def test_history_is_recomputed_and_inherited_after_no_change(routing):
    """缓存改成改善无法欺骗校验器，下一轮会继承更早的无改善策略记录。"""
    first = kwargs(routing, "first")
    service.run_cycle(**first, backend_factory=factory("no_gain"))
    cached = first["output"] / "comparison.json"
    raw = json.loads(cached.read_text())
    raw["decision"] = "IMPROVED"
    cached.write_text(json.dumps(raw))
    second = kwargs(routing, "second", history=[first["output"]])
    result = service.run_cycle(**second, backend_factory=factory(advisor_kind="no_change"))
    assert result["stage"] == "NO_CHANGE"
    third = kwargs(routing, "third", history=[second["output"]])
    result = service.run_cycle(**third, backend_factory=factory())
    assert result["stage"] == "REJECTED" and result["requests_used"] == 1
    proposal = json.loads((third["output"] / "proposal.json").read_text())
    assert proposal["reason_code"] == "PREVIOUSLY_UNSUCCESSFUL_ROUTING_STRATEGY"


@pytest.mark.parametrize("target", ["gold", "input", "round", "call"])
def test_independent_comparator_rejects_changed_artifacts(routing, target):
    """输入、标签、轮次或发送记录改动都必须使实验失效。"""
    options = kwargs(routing)
    service.run_cycle(**options, backend_factory=factory())
    if target in {"gold", "input"}:
        path = routing.dataset
        raw = json.loads(path.read_text())
        if target == "gold":
            raw["gold"][0]["expected_route"] = "FILE_LIST"
        else:
            raw["inputs"]["cases"][0]["question"] = "变更的问题"
    else:
        path = next((options["output"] / ("control/rounds" if target == "round" else "calls")).glob("*.json"))
        raw = json.loads(path.read_text())
        raw["changed"] = True
    path.write_text(json.dumps(raw))
    assert comparator.compare(options["output"])["decision"] == "INVALID_EXPERIMENT"


def test_draft_gold_requires_real_manual_confirmation(routing):
    """原文件参考标签不能自动替代本次人工路由Gold。"""
    raw = json.loads(routing.gold.read_text())
    raw["cases"][0]["confirmed"] = False
    routing.gold.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="NOT_CONFIRMED"):
        dataset.freeze(inputs_path=routing.inputs, gold_path=routing.gold,
                       output=routing.dataset.with_name("unconfirmed.json"))


def test_live_requires_real_data_and_file_list_counterexample(routing):
    """Fake不能冒充真实优化，真实数据还须包含正常文件搜索反例。"""
    with pytest.raises(ValueError, match="LIVE_FAKE_MISMATCH"):
        service.check_cycle(**kwargs(routing, source="live"))
    raw = json.loads(routing.dataset.read_text())
    raw["inputs"]["source"] = "real"
    raw["inputs_sha256"] = digest(raw["inputs"])
    for g in raw["gold"]:
        g["expected_route"] = "CONTENT_ANSWER"
    raw["gold_sha256"] = digest(raw["gold"])
    routing.dataset.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="SEARCH_COUNTEREXAMPLE_REQUIRED"):
        service.check_cycle(**kwargs(routing, source="live"))


def test_metadata_only_logs_exclude_original_task_and_model_error(routing, monkeypatch):
    """普通日志只保留事件状态，包含敏感输入的异常不能泄漏到日志。"""
    records = []
    monkeypatch.setattr(event_logging, "_append_jsonl", lambda record, **kwargs: records.append(record))
    service.run_cycle(**kwargs(routing), backend_factory=factory("planner_timeout"))
    assert records
    for record in records:
        assert set(record) == {"ts", "level", "event", "status", "duration_ms", "error_code"}
        assert "user_id" not in record and "message" not in record
    event_logging.log_event("test.normal", message="正常调用日志")
    assert records[-1]["message"] == "正常调用日志"


def test_scope_inspection_uses_actual_tool_input_and_blocks_write_handlers(routing):
    """顶层自述范围不能替代实际参数，也不能执行被模型选择的文件写操作。"""
    from app.modules.evidence_harness.routing.schemas import RoutingDocument
    case = dataset.load_dataset(routing.dataset).inputs.cases[0].model_copy(update={
        "attachments": [RoutingDocument(document_id="D1")]})
    plan = {"intent": "EVIDENCE_ANSWER", "decision_type": "TOOL_PLAN",
            "planner_decision": {"scope": {"document_ids": ["D1"]}},
            "tool_plan": {"steps": [{"tool_name": "evidence-answer", "input": {"question": "问题", "document_ids": []}}]}}
    assert "EXPLICIT_SCOPE_CHANGED" in runtime.inspect_plan(case, plan)["violation_codes"]
    plan["tool_plan"]["steps"][0] = {"tool_name": "confirmed-file-action", "input": {}}
    assert "WRITE_ROUTE_BLOCKED" in runtime.inspect_plan(case, plan)["violation_codes"]


def test_unfinalized_report_does_not_claim_completed_or_resume(routing):
    """强制结束留下的不完整实验只能只读检查。"""
    path = kwargs(routing)["output"]
    path.mkdir(parents=True)
    report = comparator.compare(path)
    assert report["execution_stage"] == "UNFINALIZED" and not report["auto_resume"]


def test_output_cannot_add_files_inside_an_existing_experiment(routing):
    """新目录若位于旧实验内部也会破坏旧完整性清单，必须在任何外发前拒绝。"""
    parent = routing.root / "experiments/harness-v1/old-experiment"
    write_new_json(parent / "manifest.json", {"stage": "COMPLETED"})
    options = kwargs(routing, output=parent / "new-routing")
    with pytest.raises(ValueError, match="INSIDE_EXISTING_EXPERIMENT"):
        service.check_cycle(**options)
    assert not options["output"].exists()


@pytest.mark.parametrize("mode,expected_calls", [("enabled", 9), ("shadow", 9)])
def test_adaptive_fallback_and_shadow_requests_share_global_budget(routing, mode, expected_calls):
    """Adaptive schema失败回退或Shadow调用都经过同一个计数client。"""
    raw = json.loads(routing.dataset.read_text())
    for case in raw["inputs"]["cases"]:
        case["adaptive_mode"] = mode
    raw["inputs_sha256"] = digest(raw["inputs"])
    routing.dataset.write_text(json.dumps(raw))
    requests = []
    result = service.run_cycle(**kwargs(routing), backend_factory=factory(requests=requests))
    assert result["stage"] == "COMPLETED" and result["requests_used"] == len(requests) == expected_calls


def test_planner_request_limit_cannot_be_retried_through_normal_exception_handler(routing):
    """每题最多3次真实请求，第4次用独立控制流阻止内部降级继续外发。"""
    options = kwargs(routing)
    options["output"].mkdir(parents=True)
    journal = service.CallJournal(options["output"], RoutingAuthorization(), service.time.monotonic)
    case = dataset.load_dataset(routing.dataset).inputs.cases[0]
    client = service.PlannerClient(journal=journal, backend=factory()("PLANNER", "control", case, 1),
                                   arm="control", case_id=case.case_id, repetition=1)
    for _ in range(3):
        client.complete_json(system_prompt="原提示", user_payload={})
    with pytest.raises(service.TrialRequestLimit):
        client.complete_json(system_prompt="原提示", user_payload={})
    assert journal.used == 3


def test_original_list_word_rule_can_still_produce_no_gain(routing):
    """忠实保留后端规则，即使Fake模型提对意图，也不强行把候选实验记成改善。"""
    case = dataset.load_dataset(routing.dataset).inputs.cases[0].model_copy(update={
        "question": "以下仅核对当时信息。只根据培训通知，列出培训对象和日期。请引用原文。"})
    path = kwargs(routing)["output"]
    path.mkdir(parents=True)
    journal = service.CallJournal(path, RoutingAuthorization(), service.time.monotonic)
    client = service.PlannerClient(journal=journal, backend=factory()("PLANNER", "candidate", case, 1),
                                   arm="candidate", case_id=case.case_id, repetition=1)
    result = runtime.run_planning(case, catalog=dataset.current_catalog(), client=client, settings=routing.settings)
    assert result["route"] == "FILE_LIST" and result["scope_valid"]


def test_cumulative_time_after_advisor_prevents_later_requests(routing):
    """建议阶段耗尽整轮时间后，下一阶段不能重领预算。"""
    now = [0.0]
    builder = factory()
    def delayed(role, arm, case, repetition):
        """用确定性时钟代替实际等待。"""
        original = builder(role, arm, case, repetition)
        class Delayed:
            def complete_json_with_usage(self, **payload):
                """模拟长时间建议调用返回时总预算已经到期。"""
                raw = original.complete_json_with_usage(**payload)
                now[0] = 1801
                return raw
        return Delayed()
    options = kwargs(routing)
    result = service.run_cycle(**options, backend_factory=delayed, clock=lambda: now[0])
    assert result["stage"] == "STOPPED" and result["requests_used"] == 1
    assert result["stop_reason"] == "ACTIVE_TIME_BUDGET_EXHAUSTED"


def test_cli_default_checks_without_creating_output_or_model_calls(routing, monkeypatch, capsys):
    """正常CLI默认只检查，展示包括回退的请求上界和授权覆盖情况。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    raw = json.loads(routing.dataset.read_text())
    raw["inputs"]["source"] = "real"
    raw["inputs_sha256"] = digest(raw["inputs"])
    routing.dataset.write_text(json.dumps(raw))
    monkeypatch.setattr(config, "get_settings", lru_cache()(lambda: routing.settings))
    options = kwargs(routing)
    monkeypatch.setattr(sys, "argv", ["harness", "routing-cycle", "--dataset", str(routing.dataset),
                                    "--output", str(options["output"]), "--advisor-max-output-tokens", "8000"])
    evidence_harness.main()
    output = json.loads(capsys.readouterr().out)
    assert output["stage"] == "CHECK_PASSED" and output["model_calls"] == 0
    assert output["planned_request_upper_bound"] == 13 and not options["output"].exists()
    assert output["request_limits"] == {"advisor_max_output_tokens": 8000,
                                        "planner_max_output_tokens": 3000, "timeout_seconds": 60}
    assert output["authorization"]["advisor_max_output_tokens"] == 8000


def test_source_export_queries_only_owned_runs_in_read_only_connection(routing, monkeypatch):
    """导出来源由用户ID和运行ID约束，正文摘要与额外敏感字段不进入规划上下文。"""
    queries, options = [], {}
    row = {"status": "COMPLETED", "intent": "LIST_MANAGED_FILES", "content": "请说明通知里的时间。",
           "attachments_json": [], "graph_state_json": {"slots": {"query": "source"},
           "adaptive_planner_mode": "legacy", "catalog_snapshot": {"catalog_fingerprint": "old-ref"},
           "context_documents": [{"document_id": "D1", "filename": "/private/source/通知.docx",
                                   "summary": "PRIVATE_BODY_SENTINEL", "api_key": "PRIVATE_KEY_SENTINEL"}]}}
    class Connection:
        """只返回固定来源行，记录所有SQL和绑定范围。"""
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, query, values):
            """模拟只读数据库，不提供写入接口。"""
            queries.append((str(query), values))
            return SimpleNamespace(mappings=lambda: SimpleNamespace(one_or_none=lambda: row),
                                   scalars=lambda: ["managed-file-list"])
    class Engine:
        """保持真实导出器的连接释放路径。"""
        def connect(self):
            return Connection()
        def dispose(self):
            pass
    def engine(_url, **kwargs):
        """测试只检查连接限制，不记录或输出DSN。"""
        options.update(kwargs)
        return Engine()
    monkeypatch.setattr(dataset, "create_engine", engine)
    settings = routing.settings.model_copy(update={"database_url": "postgresql://fake:fake@localhost/file_agent_evidence_test_fake"})
    output = routing.root / "datasets/export"
    result = dataset.prepare(user_id="owner", run_ids=["run-1"], case_ids=["R002"], output=output, settings=settings)
    assert result["model_calls"] == 0 and "default_transaction_read_only=on" in options["connect_args"]["options"]
    assert queries[0][1] == {"run_id": "run-1", "user_id": "owner"}
    saved = json.loads((output / "inputs.json").read_text())
    assert saved["cases"][0]["attachments"] == []
    encoded = json.dumps(saved)
    assert "PRIVATE_BODY_SENTINEL" not in encoded and "PRIVATE_KEY_SENTINEL" not in encoded
    assert "/private/source" not in encoded
    assert saved["source_catalog_refs"] == ["old-ref"]
    draft = json.loads((output / "gold-draft.json").read_text())
    assert draft["cases"][0]["expected_route"] is None and not draft["cases"][0]["confirmed"]
    row["status"] = "RUNNING_TOOL"
    with pytest.raises(ValueError, match="NOT_READY_OR_NOT_OWNED"):
        dataset.prepare(user_id="owner", run_ids=["run-1"], case_ids=["R002"],
                        output=output.with_name("not-ready"), settings=settings)


def test_live_wiring_uses_hard_transport_and_does_not_serialize_credentials(routing, monkeypatch):
    """隔离假数据配合传输替身穿过live分支，验证凭证只在内存且预算参数真实传入。"""
    raw = json.loads(routing.dataset.read_text())
    raw["inputs"]["source"] = "real"
    raw["inputs_sha256"] = digest(raw["inputs"])
    routing.dataset.write_text(json.dumps(raw))
    calls, connections = [], []
    cases = {c.case_id: c for c in dataset.load_dataset(routing.dataset).inputs.cases}
    build = factory(requests=calls)
    class Transport:
        """仅供本单元测试使用的无网络替身。"""
        def __init__(self, credentials, timeout):
            connections.append((credentials, timeout))
        def complete_json_with_usage(self, **request):
            """按真实请求形态识别角色，未读取Gold。"""
            if "feedback" in request["user_payload"]:
                return build("ADVISOR", None, None, None).complete_json_with_usage(**request)
            case = next(c for c in cases.values() if c.question == request["user_payload"]["message"])
            arm = "candidate" if runtime.STRATEGY_HINT in request["system_prompt"] else "control"
            return build("PLANNER", arm, case, 1).complete_json_with_usage(**request)
    monkeypatch.setattr(service, "ProcessTransport", Transport)
    options = kwargs(routing, source="live")
    result = service.run_cycle(**options)
    assert result["comparison"] == "IMPROVED" and result["model_calls"] == len(calls) == 5
    assert len(connections) == 5 and all(timeout == 60 for _, timeout in connections)
    assert all(c["api_key"] == "PRIVATE_KEY_SENTINEL" for c, _ in connections)
    text = "".join(p.read_text() for p in options["output"].rglob("*.json"))
    assert "PRIVATE_KEY_SENTINEL" not in text


@pytest.mark.parametrize("limit", [3000, 6000, 8000])
def test_advisor_output_quota_is_independent_and_actually_sent(routing, limit):
    """建议器授权实际传到请求及记录，两组规划继续按独立的3000上限执行。"""
    requests = []
    options = kwargs(routing, authorization=RoutingAuthorization(advisor_max_output_tokens=limit))
    result = service.run_cycle(**options, backend_factory=factory(requests=requests))
    assert result["comparison"] == "IMPROVED" and result["requests_used"] == 5
    assert requests[0][3]["max_output_tokens"] == limit
    assert all(request[3]["max_output_tokens"] == 3000 for request in requests[1:])
    metadata = json.loads((options["output"] / "experiment.json").read_text())
    assert metadata["authorization"]["advisor_max_output_tokens"] == limit
    assert metadata["configuration"]["max_output_tokens"] == 3000
    for suffix in ("started", "finished"):
        first = json.loads((options["output"] / f"calls/0001.{suffix}.json").read_text())
        assert first["max_output_tokens"] == limit
    assert comparator.compare(options["output"])["decision"] == "IMPROVED"


def _renew_integrity(output):
    """模拟自报摘要也被改写，证明比较器仍从授权和实际请求角色核验。"""
    files = {p.relative_to(output).as_posix(): digest(json.loads(p.read_text())) for p in output.rglob("*.json")
             if p.name not in {"integrity.json", "comparison.json", "manifest.json"}}
    (output / "integrity.json").write_text(json.dumps(files))


@pytest.mark.parametrize("change", ["planner_quota", "advisor_authorization", "invalid_code_ref"])
def test_comparator_checks_role_limits_and_historical_binding(routing, change):
    """建议器的较大额度不能被规划调用借用，已发生请求也必须符合原授权。"""
    options = kwargs(routing, authorization=RoutingAuthorization(advisor_max_output_tokens=8000))
    service.run_cycle(**options, backend_factory=factory())
    output = options["output"]
    if change == "planner_quota":
        for suffix in ("started", "finished"):
            path = output / f"calls/0002.{suffix}.json"
            raw = json.loads(path.read_text())
            raw["max_output_tokens"] = 8000
            path.write_text(json.dumps(raw))
    else:
        path = output / "experiment.json"
        raw = json.loads(path.read_text())
        if change == "advisor_authorization":
            raw["authorization"]["advisor_max_output_tokens"] = 6000
        else:
            raw["configuration"]["code_ref"] = "invalid"
            raw["configuration_ref"] = digest(raw["configuration"])
        path.write_text(json.dumps(raw))
    _renew_integrity(output)
    assert comparator.compare(output)["decision"] == "INVALID_EXPERIMENT"


def test_legacy_authorization_and_generation_code_ref_remain_readable(routing):
    """旧v1无独立额度字段时仍按3000核验，生成时代码指纹按历史绑定读取。"""
    options = kwargs(routing)
    service.run_cycle(**options, backend_factory=factory())
    path = options["output"] / "experiment.json"
    raw = json.loads(path.read_text())
    raw["authorization"].pop("advisor_max_output_tokens")
    raw["configuration"]["code_ref"] = "9" * 64
    raw["configuration_ref"] = digest(raw["configuration"])
    path.write_text(json.dumps(raw))
    _renew_integrity(options["output"])
    assert comparator.compare(options["output"])["decision"] == "IMPROVED"


def test_unsupported_advisor_output_quota_is_rejected():
    """额度只接受操作者可见的有限选项，非法值在外发前拒绝。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RoutingAuthorization(advisor_max_output_tokens=4000)
