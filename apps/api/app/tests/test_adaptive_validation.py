"""C阶段验收：固定候选、真实规划节点、保留隔离、重复稳定性和可信预算。"""

from copy import deepcopy
import json
import os

import pytest

from app.modules.evidence_evaluation.frozen_inputs import digest, write_new_json
from app.modules.evidence_harness.improvement import adaptive_policy as policy
from app.modules.evidence_harness.improvement import adaptive_validation as validation
from app.modules.evidence_harness.improvement import validation_dataset as data_store
from app.modules.evidence_harness.routing.schemas import RoutingInputs, RoutingCase, RoutingDocument, SourceSignal
from app.tests.test_routing_harness import routing
from app.tests.test_adaptive_policy import fast_os, make_baseline, options as policy_options, factory as policy_factory, INSTRUCTION


@pytest.fixture(autouse=True)
def isolated_provider_env(monkeypatch):
    """Fake验收不能继承终端中的真实Harness选择；需要env的用例在自身显式设置。"""
    for key in ("EVIDENCE_HARNESS_PROVIDER", "EVIDENCE_HARNESS_CODEX_MODEL", "EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE", "EVIDENCE_HARNESS_CODEX_TRANSPORT"):
        monkeypatch.delenv(key, raising=False)


def build_data(routing):
    """从真实B入口产生Fake候选和新增源输入；不手写IMPROVED或伪造原轨迹。"""
    source = make_baseline(routing)
    bopts = policy_options(routing, source)
    assert policy.run_cycle(**bopts, backend_factory=policy_factory())["decision"] == "IMPROVED"
    locked = data_store.lock_candidate(bopts["output"])
    signal = SourceSignal(observed_route="FILE_LIST", entry_tool="hybrid-search", evidence_answer_called=False, run_failed=False)
    cases = [RoutingCase(case_id=cid, source_run_ref=digest(cid), question=q, adaptive_mode="legacy", observation=signal,
        attachments=[RoutingDocument(document_id="D1-DOCUMENT", filename="培训通知.pdf")] if cid == "D1" else [])
        for cid, q in [("H1", "请说明活动通知的要求并引用原文。"), ("H2", "查找会议安排，只列文件名。"),
                       ("D1", "请说明附件培训要求并引用原文。"), ("D2", "再说明一下。")]]
    inputs = RoutingInputs(source="fake", cases=cases, source_catalog_refs=[], catalog_snapshot=locked["origin"]["dataset"].inputs.catalog_snapshot)
    imported = routing.root / "datasets/c-new-inputs.json"
    write_new_json(imported, inputs.model_dump(mode="json"))
    output = routing.root / "datasets/c-validation"
    data_store.prepare(candidate_dir=bopts["output"], inputs_paths=[imported], output=output)
    draft_path = output / "dataset-draft.json"
    draft = json.loads(draft_path.read_text())
    for a in draft["annotations"]:
        cid = a["case_id"]
        a.update(split="HOLDOUT" if cid.startswith("H") else "DEVELOPMENT", family_ids=["family-" + cid],
            expected_route="OTHER" if cid == "D2" else "CONTENT_ANSWER" if cid in {"C1", "H1", "D1"} else "FILE_LIST",
            expected_decision_type="CLARIFY" if cid == "D2" else "TOOL_PLAN", reason="人工确认Fake首轮目标及独立文件族。",
            confirmed=True, holdout_independence_confirmed=cid.startswith("H"))
    draft_path.write_text(json.dumps(draft))
    dataset = output / "cases-v1.json"
    data_store.freeze(draft_path=draft_path, output=dataset)
    return {"candidate": bopts["output"], "draft": draft_path, "dataset": dataset, "imported": imported}


@pytest.fixture
def c_data(routing, fast_os, monkeypatch):
    """快速测试仅用可信内进程模拟OS边界，真实隔离另有专项。"""
    monkeypatch.setattr(validation, "probe_sandbox", lambda *a, **kw: {
        "compile": True, "read_blocked": True, "write_blocked": True, "network_blocked": True, "exec_blocked": True})
    monkeypatch.setattr(validation, "run_checks", lambda *a: policy.expected_checks())
    return build_data(routing)


def validation_factory(kind="normal", requests=None):
    """固定候选必须真正进入请求；Fake不读Gold、文件族或集合标签。"""
    requests = requests if requests is not None else []

    def factory(arm, case, repetition):
        """C只创建规划Provider，没有任何建议器分支。"""
        class Client:
            """Fake仅替代Provider，计划仍经真实graph和后端校验。"""

            def complete_json_with_usage(self, **request):
                """改变特定试次验证退步、波动和中断的评分边界。"""
                requests.append({"arm": arm, "case_id": case.case_id, "repetition": repetition, **deepcopy(request)})
                if kind == "interrupt" and arm == "candidate" and repetition == 2:
                    raise KeyboardInterrupt
                if case.case_id == "D2":
                    raw = {"decision_type": "CLARIFY", "intent": "UNKNOWN", "user_goal": case.question,
                           "clarification": {"question": "请说明要继续哪项任务。"}}
                    if kind == "wrong_decision" and arm == "candidate":
                        raw = {"decision_type": "DIRECT_RESPONSE", "intent": "CHAT", "user_goal": case.question, "direct_response": "可以继续交流。"}
                    return raw, {"input_tokens": 4, "output_tokens": 3}
                raw, usage = policy_factory("planner_timeout" if kind == "timeout" else "no_gain" if kind == "no_gain" else "improve")(
                    "PLANNER", 1, arm, case, repetition).complete_json_with_usage(**request)
                adaptive = "decision_type" in request["user_payload"]["output_schema"].get("required", [])
                if adaptive and case.attachments:
                    ids = [d.document_id for d in case.attachments]
                    raw["scope"] = {"source": "attachments", "document_ids": ids}
                    raw["tool_plan"]["steps"][0]["literal_input"]["document_ids"] = ids
                if kind == "regress" and arm == "candidate" and case.case_id == "N1":
                    raw["intent"] = "EVIDENCE_ANSWER"
                if kind == "unstable" and arm == "candidate" and case.case_id == "H1" and repetition == 2:
                    raw["intent"] = "SEARCH_FILES"
                if kind == "contract_regress" and case.case_id == "N1":
                    raw["intent"] = "SEARCH_FILES" if arm == "control" else "自创文件意图"
                if kind == "safety" and arm == "candidate":
                    raw["tool_plan"]["steps"][0]["requires_confirmation"] = True
                if kind == "unknown_usage" and arm == "candidate":
                    usage = {}
                return raw, usage
        return Client()
    return factory


def options(routing, data, name="c-run", **updates):
    """扩大和重复的额度必须由操作者明确给出，不能继承B的13次授权。"""
    return {"dataset_path": data["dataset"], "output": routing.root / f"experiments/harness-v1/{name}",
            "settings": routing.settings, "authorization": validation.ValidationAuthorization(max_total_requests=72, repetitions=2),
            "source": "fake", **updates}


def test_fixed_candidate_expansion_repetitions_and_private_holdout(routing, c_data):
    """同一候选跨两轮六题，源代码和B产物不变，没有建议器或Gold外发。"""
    requests = []
    opts = options(routing, c_data)
    before = {str(p): p.read_bytes() for p in c_data["candidate"].rglob("*") if p.is_file()}
    checked = validation.check(**opts)
    assert checked["planned_request_upper_bound"] == 72 and not opts["output"].exists()
    report = validation.run(**opts, backend_factory=validation_factory(requests=requests))
    assert report["decision"] == "VALIDATED", report
    assert report["case_count"] == 6 and report["repetitions"] == 2 and report["advisor_calls"] == 0
    assert report["requests_used"] == len(requests) == 24
    assert report["arms"]["candidate"]["HOLDOUT"]["correct"] == 4
    assert report["coverage"] == {"attachment_cases": 1, "clarification_cases": 1}
    assert all(r["route_and_decision_stable"] for r in report["arms"]["candidate"]["overall"]["cases"])
    assert report["policy_ref"] == json.loads((c_data["candidate"] / "policy.json").read_text())["prompt_ref"]
    assert all((INSTRUCTION in r["system_prompt"]) == (r["arm"] == "candidate") for r in requests)
    assert requests[0]["arm"] == "control" and requests[12]["arm"] == "candidate"
    serialized = json.dumps(requests)
    for secret in ["expected_route", "HOLDOUT", "family_ids", "PRIVATE_KEY_SENTINEL", "GOLD_PRIVATE_SENTINEL"]:
        assert secret not in serialized
    snapshot = {str(p): p.read_bytes() for p in opts["output"].rglob("*") if p.is_file()}
    assert validation.compare(opts["output"]) == report
    assert snapshot == {str(p): p.read_bytes() for p in opts["output"].rglob("*") if p.is_file()}
    assert before == {str(p): p.read_bytes() for p in c_data["candidate"].rglob("*") if p.is_file()}


@pytest.mark.parametrize("field", ["llm_chat_model", "llm_base_url"])
def test_changed_model_profile_requires_explicit_choice_before_any_call(routing, c_data, field):
    """更换模型或接口不能静默混入同模型实验，拒绝时不建目录也不请求Provider。"""
    settings = routing.settings.model_copy(update={field: "new-model" if field == "llm_chat_model" else "https://new.invalid/v1"})
    opts = options(routing, c_data, settings=settings)
    requests = []
    with pytest.raises(ValueError, match="VALIDATION_MODEL_PROFILE_CHANGED"):
        validation.run(**opts, backend_factory=validation_factory(requests=requests))
    assert requests == [] and not opts["output"].exists()


@pytest.mark.parametrize("changes", [{}, {"llm_chat_model": "new-model"}, {"llm_base_url": "https://new.invalid/v1"}])
def test_explicit_new_profile_binds_both_arms_without_rebuilding_candidate(routing, c_data, monkeypatch, changes):
    """显式允许后固定两组共用配置，复制Settings避免外部修改影响后续轮次。"""
    settings = routing.settings.model_copy(update=changes)
    opts = options(routing, c_data, settings=settings, allow_model_profile_change=True)
    checked = validation.check(**opts)
    assert checked["comparison_scope"] == ("CROSS_MODEL" if changes else "SAME_MODEL")
    assert not opts["output"].exists()
    source_before = {str(p): p.read_bytes() for p in c_data["candidate"].rglob("*") if p.is_file()}
    requests, observed = [], []
    fake_factory = validation_factory(requests=requests)

    def backend_factory(arm, case, n):
        """模拟调用方修改原配置；执行器已经复制的配置不应受到影响。"""
        settings.llm_chat_model = "externally-mutated-model"
        return fake_factory(arm, case, n)

    original_profile = validation._execution_profile

    def record_profile(selected, authorization):
        """记录执行入口实际选用的配置，不能由候选进程指定。"""
        observed.append(selected)
        return original_profile(selected, authorization)

    monkeypatch.setattr(validation, "_execution_profile", record_profile)
    report = validation.run(**opts, backend_factory=backend_factory)
    assert report["decision"] == "VALIDATED", report
    assert report["comparison_scope"] == checked["comparison_scope"]
    assert report["origin_profile"] == checked["origin_profile"]
    assert report["profile"] == checked["profile"]
    assert observed[0] is not settings
    assert observed[0].llm_chat_model != settings.llm_chat_model
    assert report["requests_used"] == len(requests) == 24 and report["advisor_calls"] == 0
    for path in (opts["output"] / "calls").glob("*.json"):
        assert json.loads(path.read_text())["profile_ref"] == digest(checked["profile"])
    assert validation.compare(opts["output"]) == report
    assert source_before == {str(p): p.read_bytes() for p in c_data["candidate"].rglob("*") if p.is_file()}


def _renew_integrity(root):
    """重新计算外层清单，验证比较器还会独立核对内部协议关系。"""
    manifest = policy.file_manifest(root)
    for key in ("integrity.json", "comparison.json", "summary.md"):
        manifest.pop(key, None)
    (root / "integrity.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize("change", ["origin", "effective", "authorization", "scope", "start", "finish", "missing_profile", "trusted"])
def test_new_profile_protocol_rejects_inconsistent_records(routing, c_data, change):
    """新旧配置、明确授权和逐调用身份相互绑定，不能仅改头部宣称另一模型结果。"""
    settings = routing.settings.model_copy(update={"llm_chat_model": "new-model"})
    opts = options(routing, c_data, settings=settings, allow_model_profile_change=True)
    assert validation.run(**opts, backend_factory=validation_factory())["decision"] == "VALIDATED"
    root = opts["output"]
    path = root / ("calls/0001.started.json" if change in {"start", "missing_profile"}
        else "calls/0001.finished.json" if change == "finish" else "experiment.json")
    raw = json.loads(path.read_text())
    if change == "origin": raw["origin_profile"]["model_ref"] = "0" * 64
    if change == "effective": raw["profile"]["model_ref"] = "0" * 64
    if change == "authorization": raw["allow_model_profile_change"] = False
    if change == "scope": raw["comparison_scope"] = "SAME_MODEL"
    if change in {"start", "finish"}: raw["profile_ref"] = "0" * 64
    if change == "missing_profile": raw.pop("profile_ref")
    if change == "trusted": raw["trusted_code_ref"] = "0" * 64
    path.write_text(json.dumps(raw))
    _renew_integrity(root)
    assert validation.compare(root)["decision"] == "INVALID_EXPERIMENT"


def test_known_old_c_protocol_remains_read_only_and_same_model(routing, c_data):
    """旧C没有逐调用配置字段，只有明确已知旧执行器且保持原模型时才能重放。"""
    opts = options(routing, c_data)
    report = validation.run(**opts, backend_factory=validation_factory())
    root = opts["output"]
    path = root / "experiment.json"
    meta = json.loads(path.read_text())
    for key in ("origin_profile", "allow_model_profile_change", "model_changed", "endpoint_changed", "comparison_scope"):
        meta.pop(key)
    meta.update(schema_version="adaptive-validation-experiment-v1", trusted_code_ref=validation.KNOWN_C_V1_TRUSTED_REF)
    path.write_text(json.dumps(meta))
    for path in (root / "calls").glob("*.json"):
        record = json.loads(path.read_text())
        record.pop("profile_ref")
        path.write_text(json.dumps(record))
    _renew_integrity(root)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    old_report = validation.compare(root)
    assert old_report["decision"] == report["decision"] == "VALIDATED"
    assert "comparison_scope" not in old_report
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    meta["profile"]["model_ref"] = digest("another-model")
    (root / "experiment.json").write_text(json.dumps(meta))
    _renew_integrity(root)
    assert validation.compare(root)["decision"] == "INVALID_EXPERIMENT"


@pytest.mark.parametrize("transport", ["auto", "https"])
def test_codex_provider_has_explicit_profile_and_preserves_fixed_candidate(routing, c_data, monkeypatch, transport):
    """Codex只是宿主模型代理，两组仍调用原规划图、固定候选和独立Gold裁判。"""
    from app.modules.evidence_harness.improvement import codex_backend
    from app.tests.test_codex_harness_backend import fake_spec
    monkeypatch.setattr(codex_backend, "inspect", lambda model, **kw: {**fake_spec(), "model": model, **kw})
    opts = options(routing, c_data, provider="codex-login", codex_model="fake-model", allow_model_profile_change=True,
        codex_transport=transport)
    checked = validation.check(**opts)
    assert checked["provider"] == "codex-login" and not opts["output"].exists()
    assert checked["profile"]["request_count_unit"] == "CODEX_INVOCATIONS"
    assert checked["profile"]["output_token_limit_enforced"] is False
    requests = []
    report = validation.run(**opts, backend_factory=validation_factory(requests=requests))
    assert report["decision"] == "VALIDATED", report
    assert report["provider"] == "codex-login" and report["requests_used"] == len(requests) == 24
    assert report["profile"]["adapter_ref"] == (codex_backend.HTTPS_POLICY_REF if transport == "https" else codex_backend.POLICY_REF)
    assert report["advisor_calls"] == 0 and not report["automatically_adopted"]
    # 比较完全离线，不要求本机还有登录或安装Codex。
    monkeypatch.setattr(codex_backend, "inspect", lambda model: (_ for _ in ()).throw(AssertionError("must not inspect login")))
    assert validation.compare(opts["output"]) == report
    for key, value in (("adapter_ref", "0" * 64), ("output_token_limit_enforced", True), ("request_count_unit", "MODEL_REQUESTS")):
        path = opts["output"] / "experiment.json"
        original = json.loads(path.read_text())
        changed = deepcopy(original)
        changed["profile"][key] = value
        path.write_text(json.dumps(changed))
        _renew_integrity(opts["output"])
        assert validation.compare(opts["output"])["decision"] == "INVALID_EXPERIMENT"
        path.write_text(json.dumps(original))
        _renew_integrity(opts["output"])


def test_known_old_c_v2_replays_without_codex_login(routing, c_data, monkeypatch):
    """旧跨模型API实验按原v2协议重放，不允许新增认证方式破坏历史核验。"""
    opts = options(routing, c_data, settings=routing.settings.model_copy(update={"llm_chat_model": "new-model"}),
        allow_model_profile_change=True)
    report = validation.run(**opts, backend_factory=validation_factory())
    root = opts["output"]
    path = root / "experiment.json"
    meta = json.loads(path.read_text())
    meta.pop("provider")
    meta.update(schema_version="adaptive-validation-experiment-v2", trusted_code_ref=validation.KNOWN_C_V2_TRUSTED_REF)
    path.write_text(json.dumps(meta))
    _renew_integrity(root)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    old_report = validation.compare(root)
    assert old_report == {k: v for k, v in report.items() if k != "provider"}
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("kind,decision", [("regress", "REGRESSION"), ("contract_regress", "REGRESSION"),
    ("unstable", "NO_GAIN"), ("no_gain", "NO_GAIN"), ("safety", "REGRESSION"),
    ("timeout", "INCONCLUSIVE"), ("wrong_decision", "REGRESSION"), ("unknown_usage", "VALIDATED")])
def test_regressions_instability_and_execution_failures(routing, c_data, kind, decision):
    """正常题与契约退步优先拦截，保留集错误不能通过扩大验证，失败费用不记零。"""
    report = validation.run(**options(routing, c_data), backend_factory=validation_factory(kind))
    assert report["decision"] == decision, report
    if kind == "contract_regress":
        assert report["contract_regression_case_ids"] == ["N1"]
    if kind == "unstable":
        case = next(c for c in report["arms"]["candidate"]["HOLDOUT"]["cases"] if c["case_id"] == "H1")
        assert case["correct"] == 1 and not case["route_and_decision_stable"]
    if kind == "wrong_decision":
        rows = [r for r in report["arms"]["candidate"]["overall"]["rows"] if r["case_id"] == "D2"]
        assert all(r["route_correct"] and not r["decision_type_correct"] for r in rows)
    if kind == "unknown_usage":
        assert report["unknown_token_calls"] == 10 and not report["token_usage_complete"]


@pytest.mark.parametrize("kind", ["budget", "time", "interrupt"])
def test_partial_trials_not_misreported_as_business_regression(routing, c_data, kind):
    """未运行题保留分母，不把预算、时间或中断当成已证实策略退步。"""
    auth = validation.ValidationAuthorization(max_total_requests=3 if kind == "budget" else 72,
        max_active_seconds=1 if kind == "time" else 3600, repetitions=2)
    report = validation.run(**options(routing, c_data, authorization=auth), backend_factory=validation_factory(kind))
    assert report["decision"] == "INCONCLUSIVE", report
    assert report["arms"]["candidate"]["overall"]["planned"] == 12
    assert report["stage"] == ("INTERRUPTED" if kind == "interrupt" else "STOPPED")
    if kind == "time": assert report["requests_used"] == 0
    if kind == "budget": assert report["requests_used"] == 3


@pytest.mark.parametrize("change", ["unconfirmed", "family", "document", "original_holdout", "old_gold", "other_type", "holdout_not_confirmed", "input_changed"])
def test_freeze_rejects_unconfirmed_or_contaminated_inputs(routing, c_data, change):
    """保持原题、真实上下文和独立保留集边界，不能靠改分组或标签制造收益。"""
    draft = json.loads(c_data["draft"].read_text())
    annotations = {a["case_id"]: a for a in draft["annotations"]}
    if change == "unconfirmed": annotations["H1"]["confirmed"] = False
    if change == "family": annotations["H1"]["family_ids"] = annotations["C1"]["family_ids"]
    if change == "original_holdout": annotations["C1"].update(split="HOLDOUT", holdout_independence_confirmed=True)
    if change == "old_gold": annotations["C1"]["expected_route"] = "FILE_LIST"
    if change == "other_type": annotations["D2"]["expected_decision_type"] = None
    if change == "holdout_not_confirmed": annotations["H1"]["holdout_independence_confirmed"] = False
    if change == "input_changed": draft["inputs"]["cases"][0]["question"] = "伪造原问题"
    if change == "document":
        # 改动真实导出后重新prepare，证明即使源身份更新，跨集合共用文档也拒绝。
        imported = json.loads(c_data["imported"].read_text())
        imported["cases"][0]["attachments"] = imported["cases"][2]["attachments"]
        c_data["imported"].write_text(json.dumps(imported))
        draft["imports"][0]["sha256"] = data_store.store._read(c_data["imported"])[1]
        draft["inputs"]["cases"][2]["attachments"] = imported["cases"][0]["attachments"]
    c_data["draft"].write_text(json.dumps(draft))
    with pytest.raises(ValueError):
        data_store.freeze(draft_path=c_data["draft"], output=c_data["draft"].with_name("invalid.json"))


@pytest.mark.parametrize("change", ["policy", "schedule", "raw", "profile", "split", "code"])
def test_compare_rejects_tampering_after_integrity_is_rewritten(routing, c_data, change):
    """源候选、试次顺序、模型原始结构、预算Profile、分组和源码均独立核验。"""
    opts = options(routing, c_data)
    assert validation.run(**opts, backend_factory=validation_factory())["decision"] == "VALIDATED"
    root = opts["output"]
    path = root / {"policy": "experiment.json", "schedule": "schedule.json", "raw": "trials/candidate-C1-1.json", "profile": "experiment.json"}.get(change, "experiment.json")
    if change == "code":
        target = root / "candidate" / policy.TARGET
        target.chmod(0o600)
        target.write_text(target.read_text() + "\nFORGED = True\n")
    elif change == "split":
        raw = json.loads(c_data["dataset"].read_text())
        raw["annotations"][2]["split"] = "DEVELOPMENT"
        raw["annotations_sha256"] = digest(raw["annotations"])
        c_data["dataset"].write_text(json.dumps(raw))
    else:
        raw = json.loads(path.read_text())
        if change == "policy": raw["policy_ref"] = "0" * 64
        if change == "schedule": raw["trials"].reverse()
        if change == "profile": raw["profile"]["planner_max_output_tokens"] = 8000
        if change == "raw": raw["result"]["planning_audit"]["responses"][0]["raw"]["intent"] = "SEARCH_FILES"
        path.write_text(json.dumps(raw))
    manifest = policy.file_manifest(root)
    for key in ("integrity.json", "comparison.json", "summary.md"): manifest.pop(key, None)
    (root / "integrity.json").write_text(json.dumps(manifest))
    assert validation.compare(root)["decision"] == "INVALID_EXPERIMENT"


@pytest.mark.parametrize("changed", [False, True])
def test_cli_check_is_zero_call_and_requires_new_output(routing, c_data, monkeypatch, capsys, changed):
    """默认检查明确展示72次上界，无建议器和模型调用，也不创建实验目录。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    original = validation.check
    opts = options(routing, c_data)
    monkeypatch.setattr(validation, "check", lambda **kw: original(**{**kw, "source": "fake"}))
    monkeypatch.setattr(sys, "argv", ["harness", "adaptive-validate", "--dataset", str(c_data["dataset"]), "--output", str(opts["output"]),
        "--max-requests", "72", "--repetitions", "2", *(["--allow-model-profile-change"] if changed else [])])
    settings = routing.settings.model_copy(update={"llm_chat_model": "new-model"}) if changed else routing.settings
    loader = config.get_settings
    try:
        config.get_settings = lambda: settings
        evidence_harness.main()
    finally:
        config.get_settings = loader
    result = json.loads(capsys.readouterr().out)
    assert result["stage"] == "CHECK_PASSED" and result["advisor_calls"] == result["model_calls"] == 0
    assert result["planned_request_upper_bound"] == 72 and result["split_counts"] == {"DEVELOPMENT": 4, "HOLDOUT": 2}
    assert result["comparison_scope"] == ("CROSS_MODEL" if changed else "SAME_MODEL")
    assert not opts["output"].exists()


@pytest.mark.parametrize("api_override,live", [(False, False), (True, False), (False, True)])
def test_cli_env_provider_and_api_override_reach_original_executor(routing, c_data, monkeypatch, capsys, api_override, live):
    """env选择能进入真实C入口，原API临时覆盖可用，live仍须显式选择且只用Fake。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    from app.modules.evidence_harness.improvement import codex_backend
    from app.tests.test_codex_harness_backend import fake_spec
    monkeypatch.setenv("EVIDENCE_HARNESS_PROVIDER", "codex-login")
    monkeypatch.setenv("EVIDENCE_HARNESS_CODEX_MODEL", "fake-model")
    monkeypatch.setenv("EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE", "true")
    inspections = []

    def inspect(model):
        """Fake只记录是否检查了Codex；API覆盖时不应依赖本机登录。"""
        inspections.append(model)
        return {**fake_spec(), "model": model}
    monkeypatch.setattr(codex_backend, "inspect", inspect)
    original_check, original_run = validation.check, validation.run
    requests = []
    monkeypatch.setattr(validation, "check", lambda **kw: original_check(**{**kw, "source": "fake"}))
    monkeypatch.setattr(validation, "run", lambda **kw: original_run(**{**kw, "source": "fake"}, backend_factory=validation_factory(requests=requests)))
    opts = options(routing, c_data)
    monkeypatch.setattr(sys, "argv", ["harness", "adaptive-validate", "--dataset", str(c_data["dataset"]),
        "--output", str(opts["output"]), "--max-requests", "72", "--repetitions", "2",
        *(["--provider", "openai-compatible"] if api_override else []), *(["--live"] if live else [])])
    loader = config.get_settings
    try:
        config.get_settings = lambda: routing.settings
        evidence_harness.main()
    finally:
        config.get_settings = loader
    result = json.loads(capsys.readouterr().out)
    assert result["provider"] == ("openai-compatible" if api_override else "codex-login")
    assert result["advisor_calls"] == 0
    assert inspections == ([] if api_override else ["fake-model"])
    if live:
        assert result["decision"] == "VALIDATED" and result["source"] == "fake"
        assert len(requests) == 24 and opts["output"].exists()
    else:
        assert result["stage"] == "CHECK_PASSED" and result["model_calls"] == 0
        assert result["configuration_sources"]["provider"] == ("CLI" if api_override else "ENV")
        assert not requests and not opts["output"].exists()


def test_cli_can_explicitly_revoke_env_model_change_permission(routing, c_data, monkeypatch, capsys):
    """env允许变更时，命令明确关闭仍应在模型调用及建目录前拒绝。"""
    import sys
    from app.core import config
    from app.scripts import evidence_harness
    monkeypatch.setenv("EVIDENCE_HARNESS_ALLOW_MODEL_PROFILE_CHANGE", "true")
    original = validation.check
    monkeypatch.setattr(validation, "check", lambda **kw: original(**{**kw, "source": "fake"}))
    opts = options(routing, c_data)
    monkeypatch.setattr(sys, "argv", ["harness", "adaptive-validate", "--dataset", str(c_data["dataset"]),
        "--output", str(opts["output"]), "--max-requests", "72", "--repetitions", "2", "--no-allow-model-profile-change"])
    loader = config.get_settings
    try:
        config.get_settings = lambda: routing.settings.model_copy(update={"llm_chat_model": "new-model"})
        with pytest.raises(SystemExit) as exc:
            evidence_harness.main()
        assert exc.value.code == 2
    finally:
        config.get_settings = loader
    assert "VALIDATION_MODEL_PROFILE_CHANGED" in capsys.readouterr().err
    assert not opts["output"].exists()


def test_separate_manual_annotations_and_no_expansion_guard(routing, c_data):
    """人工只编辑小标签文件即可冻结；原两题不能冒充扩大验证集。"""
    annotations = json.loads(c_data["draft"].read_text())["annotations"]
    path = c_data["draft"].with_name("manual-confirmed.json")
    write_new_json(path, {"cases": annotations})
    raw = json.loads(c_data["draft"].read_text())
    for a in raw["annotations"]: a["confirmed"] = False
    c_data["draft"].write_text(json.dumps(raw))
    output = c_data["draft"].with_name("external-labels.json")
    assert data_store.freeze(draft_path=c_data["draft"], annotations_path=path, output=output)["stage"] == "VALIDATION_DATASET_FROZEN"
    assert len(data_store.load(output)[0].annotations) == 6
    folder = routing.root / "datasets/not-expanded"
    data_store.prepare(candidate_dir=c_data["candidate"], inputs_paths=[], output=folder)
    with pytest.raises(ValueError, match="EXPANSION_REQUIRED"):
        data_store.freeze(draft_path=folder / "dataset-draft.json", output=folder / "invalid.json")


def test_attachment_proxy_uses_backend_context_projection(routing, c_data):
    """代理必须和真实Planning使用同一受控附件投影，不能直接采信原附件名。"""
    from app.modules.agent.graph import _planning_attachments
    from app.modules.agent.adaptive_planner import ADAPTIVE_PLANNER_SYSTEM_PROMPT
    from app.modules.agent.planner_contracts import PlannerDecision
    from app.modules.evidence_harness.improvement.executor import validate_planner_request
    dataset, _ = data_store.load(c_data["dataset"])
    case = next(c for c in dataset.inputs.cases if c.case_id == "D1").model_copy(update={"adaptive_mode": "enabled"})
    payload = {"message": case.question, "attachments": _planning_attachments({"attachments": [d.model_dump() for d in case.attachments],
        "context_documents": [d.model_dump() for d in case.context_documents]}),
        "context_documents": [d.model_dump() for d in case.context_documents], "observation": {},
        "catalog_snapshot": dataset.inputs.catalog_snapshot, "output_schema": PlannerDecision.model_json_schema()}
    request = {"type": "MODEL_REQUEST", "system_prompt": ADAPTIVE_PLANNER_SYSTEM_PROMPT, "user_payload": payload}
    validate_planner_request(request, case, dataset.inputs.catalog_snapshot, phase="ADAPTIVE")
    assert payload["attachments"][0]["filename"] is None
    payload["attachments"][0]["filename"] = case.attachments[0].filename
    with pytest.raises(ValueError, match="BROKER_INPUT_NOT_ALLOWED"):
        validate_planner_request(request, case, dataset.inputs.catalog_snapshot, phase="ADAPTIVE")


@pytest.mark.skipif(os.environ.get("HARNESS_OS_ACCEPTANCE") != "1", reason="需本机真实OS隔离验收")
def test_os_fixed_candidate_repeats_without_gold_or_env_access(routing):
    """真实隔离进程完成扩大重复对照，Provider为Fake；标签和上级env仍不可读。"""
    routing.root.mkdir(parents=True, exist_ok=True)
    (routing.root / ".env").write_text("LLM_API_KEY=OS_PRIVATE_SENTINEL\n")
    data = build_data(routing)
    opts = options(routing, data, settings=routing.settings.model_copy(update={"llm_chat_model": "new-model"}),
        allow_model_profile_change=True)
    report = validation.run(**opts, backend_factory=validation_factory())
    assert report["decision"] == "VALIDATED", report
    assert report["advisor_calls"] == 0 and report["requests_used"] == 24
    assert report["comparison_scope"] == "CROSS_MODEL" and report["model_changed"]
    assert validation.compare(opts["output"]) == report
    from app.modules.evidence_harness.improvement.isolation import isolated_run
    for name, path in (("env", routing.root / ".env"), ("gold", data["dataset"]), ("source", data["candidate"] / "proposal.json")):
        assert isolated_run(opts["output"] / "candidate", routing.root / ("forbidden-" + name),
            {"mode": "probe", "action": "read", "path": str(path)}, planner_mode="enabled")["blocked"]
