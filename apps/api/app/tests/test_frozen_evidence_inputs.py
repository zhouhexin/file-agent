"""真实输入冻结边界与离线复测回归，全部隔离文件/SQLite和deterministic fake。"""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.modules.evidence_evaluation import frozen_inputs as frozen
from app.modules.evidence_evaluation.frozen_inputs import FrozenCapture, CaptureSelection, RecordingSemanticClient
from app.modules.evidence_evaluation.semantic_checker import SemanticChecker
from app.modules.evidence_evaluation.version_checker import VersionSnapshot, AuthorizedVersionReader
from app.modules.evidence_answer.service import EvidenceAnswerService
from app.scripts.evaluate_evidence import EvaluationCase, load_cases, run_evaluation
from app.scripts.frozen_evidence import build, confirm, read_workbook_plan
from app.tests.test_evidence_evaluation import FakeClient, package, response
from app.tests.test_evidence_answer import _session, _seed, _settings, FakeEvidenceClient
from app.tests.test_evidence_evaluation_integration import _source


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(frozen, "TEST_ROOT", tmp_path / "test")
    root = frozen.TEST_ROOT / "datasets/frozen-v1"
    root.mkdir(parents=True)
    return root


def save(root, *, pkg=None, focus="CURRENT", current=None):
    pkg = pkg or package()
    versions = {i.evidence_id: VersionSnapshot("VERIFIED", current or i.document_version_id)
                for i in pkg.evidence_items}
    capture = FrozenCapture(directory=root, dataset_id="frozen-v1", selection=CaptureSelection(
        case_id="R001", question=pkg.question, document_ids=sorted({i.document_id for i in pkg.evidence_items})))
    return capture.save_input(package=pkg, versions=versions, original_question=pkg.question,
                              temporal_focus=focus, provenance={"agent_run_id": "test-run"}, config={})


def normal_gold(root, snapshot):
    path = root / "annotations/draft.json"
    frozen.write_new_json(path, {"cases": [{"case_id": "R001", "group": "NORMAL", "expected_issues": [],
        "expected_explanation": "原文支持所问金额。", "annotation_status": "DRAFT", "input_sha256": None}]})
    confirmed = root / "annotations/R001.json"
    confirm(root, path, "R001", snapshot.name, confirmed)
    return confirmed


def test_full_input_hash_covers_question_context_and_versions(isolated):
    a = save(isolated)
    pkg = package();pkg.question = "当前申请预算是多少？"
    b = save(isolated, pkg=pkg)
    c = save(isolated, focus="HISTORICAL")
    d = save(isolated, current="new-version")
    assert len({p.name for p in (a, b, c, d)}) == 4
    assert a == save(isolated)
    assert frozen.load_snapshot(a)["package"]["evidence_fingerprint"] == "test-fingerprint"
    assert (a / "package.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("target", ["package", "versions", "context", "manifest"])
def test_tampering_is_rejected(isolated, target):
    snapshot = save(isolated)
    path = snapshot / f"{target}.json"
    data = json.loads(path.read_text())
    if target == "package":data["evidence_items"][0]["quote"] = "人为添加的金额"
    elif target == "versions":data["ev_1"]["current_version_id"] = "changed"
    elif target == "context":data["original_question"] = "changed"
    else:data["capture_config"]["max_calls"] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):frozen.load_snapshot(snapshot)


def test_outside_paths_and_symlinks_are_rejected(isolated, tmp_path):
    with pytest.raises(ValueError):frozen.private_dataset_path(tmp_path / "outside")
    (isolated / "escape").symlink_to(tmp_path)
    with pytest.raises(ValueError):frozen.private_dataset_path(isolated / "escape/x")


def test_capture_requires_test_database_and_exact_question_and_docs(isolated):
    pkg = package()
    plan = isolated / "capture-plan.json"
    frozen.write_new_json(plan, {"dataset_id": "frozen-v1", "cases": [
        {"case_id": "R001", "question": pkg.question, "document_ids": ["doc_1", "doc_2"]}]})
    settings = _settings().model_copy(update={"evidence_evaluation_capture_plan": str(plan)})
    with pytest.raises(ValueError):FrozenCapture.select(settings, pkg, pkg.question)
    settings = settings.model_copy(update={"database_url": "postgresql://fake:fake@localhost/file_agent_evidence_test"})
    assert FrozenCapture.select(settings, pkg, pkg.question)
    assert FrozenCapture.select(settings, pkg, "other question") is None
    assert FrozenCapture.select(settings, pkg.model_copy(update={"evidence_items": pkg.evidence_items[:1]}), pkg.question) is None
    # 选定两份文件但只召回一份的证据，仍应采集真实缺项输入，不能伪造第二份证据。
    partial = pkg.model_copy(update={"evidence_items": pkg.evidence_items[:1],
                                   "scope": {"document_ids": ["doc_1", "doc_2"]}})
    assert FrozenCapture.select(settings, partial, pkg.question)
    outside = pkg.model_copy(update={"scope": {"document_ids": ["doc_1"]}})
    with pytest.raises(ValueError, match="OUTSIDE_SCOPE"):
        FrozenCapture.select(settings, outside, pkg.question)


def test_confirm_build_and_hash_validation(isolated):
    snapshot = save(isolated)
    gold = normal_gold(isolated, snapshot)
    target = isolated / "cases.json"
    build(isolated, [gold], target, [])
    case = load_cases(target)[0]
    assert case.source == "real" and case.temporal_focus == "CURRENT"
    assert case.recorded_semantic_response is None
    assert run_evaluation([case], mode="rules")["cases"][0]["matches_annotation"] is False
    with pytest.raises(ValueError, match="REPLAY_RECORDING_REQUIRED"):
        run_evaluation([case], mode="replay")
    with pytest.raises(FileExistsError):build(isolated, [gold], target, [])
    payload = json.loads(target.read_text());payload["cases"][0]["package"]["question"] = "changed"
    target.write_text(json.dumps(payload))
    with pytest.raises(ValueError):load_cases(target)


def test_draft_gold_cannot_be_used_for_scoring(isolated):
    snapshot = save(isolated);gold = normal_gold(isolated, snapshot)
    data = json.loads(gold.read_text());data["cases"][0]["annotation_status"] = "DRAFT"
    draft = isolated / "draft.json";draft.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="GOLD_NOT_BOUND"):
        build(isolated, [draft], isolated / "bad.json", [])


def test_versions_exact_coverage_is_validated_before_model_call():
    pkg = package()
    with pytest.raises(ValueError, match="VERSION_KEYS"):
        EvaluationCase(case_id="R001", group="NORMAL", package=pkg, versions={}, expected_issues=[], expected_explanation="x")


def case_for(pkg=None):
    pkg = pkg or package()
    return EvaluationCase(case_id="R001", group="NORMAL", package=pkg,
        versions={i.evidence_id: {"status": "VERIFIED", "current_version_id": i.document_version_id} for i in pkg.evidence_items},
        expected_issues=[], expected_explanation="依据原文", temporal_focus="CURRENT", source="real")


def test_every_repeat_is_retained_and_failure_is_not_normal():
    client = FakeClient(TimeoutError("DO_NOT_PERSIST_SECRET"), response())
    result = run_evaluation([case_for()], mode="live", repetitions=2, live_checker=SemanticChecker(client=client))
    record = result["cases"][0]
    assert len(record["rounds"]) == 2
    assert not record["matches_annotation"]
    assert result["per_round"][0]["per_class"]["NORMAL"]["fn"] == 1
    assert result["per_round"][1]["per_class"]["NORMAL"]["tp"] == 1
    assert result["repeated_stability_rate"] == 0
    assert result["per_class"]["CONFLICT"]["recall"] is None
    assert "DO_NOT_PERSIST_SECRET" not in json.dumps(result)
    for payload in client.payloads:
        assert "expected_issues" not in payload["user_payload"]
        assert "expected_explanation" not in payload["user_payload"]


def test_frozen_temporal_focus_is_used_instead_of_normalized_question():
    case = case_for();case.package.question = "请总结这些材料。"
    case.versions["ev_1"]["current_version_id"] = "new"
    case.temporal_focus = "HISTORICAL"
    result = run_evaluation([case], mode="rules")["cases"][0]["result"]
    assert result["version_check"]["status"] == "COMPLETED"
    assert not result["issues"]


def test_multi_batch_recording_replay_checks_each_payload_and_budget():
    pkg = package(quotes=["批准金额500万元。" * 1200] * 3)
    recording = RecordingSemanticClient(FakeClient(response("UNSUPPORTED")))
    settings = dict(max_input_chars=16000, max_calls=3, max_output_tokens=500)
    semantic = SemanticChecker(client=recording, **settings)
    original = semantic.check(pkg)
    assert len(recording.calls) > 1
    case = case_for(pkg);case.recorded_semantic_calls = deepcopy(recording.calls);case.capture_config = settings
    case.expected_issues = []
    replay = run_evaluation([case], mode="replay", repetitions=2)
    assert replay["cases"][0]["result"]["semantic_check"]["status"] == original.status
    assert replay["input_tokens"] == 0
    case.package.question = "changed"
    with pytest.raises(ValueError, match="REPLAY_REQUEST_HASH_MISMATCH"):
        run_evaluation([case], mode="replay", repetitions=1)


@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("export_failure", [False, True])
def test_capture_uses_one_authorized_read_and_preserves_answer(isolated, monkeypatch, source, export_failure):
    from app.modules.evidence_evaluation.frozen_inputs import CapturePlan

    question = "当前申报截止时间是什么？"
    plan = isolated / "capture-plan.json"
    frozen.write_new_json(plan, CapturePlan(dataset_id="frozen-v1", cases=[
        CaptureSelection(case_id="R001", question=question, document_ids=["document-1"])
    ]).model_dump(mode="json"))
    reads = []
    original_read = AuthorizedVersionReader.__call__
    def read(self, package):
        result = original_read(self, package);reads.append(result);return result
    monkeypatch.setattr(AuthorizedVersionReader, "__call__", read)
    if export_failure:
        def fail(*args, **kwargs):raise OSError("secret-local-path")
        monkeypatch.setattr(FrozenCapture, "save_input", fail)
    with _session() as db:
        _source(db) if source else _seed(db)
        settings = _settings().model_copy(update={"database_url": "postgresql://fake:fake@localhost/file_agent_evidence_test",
            "log_dir": str(isolated / "logs"), "evidence_answer_cache_enabled": False})
        raw = response();raw["requirements"][0]["evidence_ids"] = ["evidence-1"]
        client = FakeEvidenceClient()
        service = EvidenceAnswerService(db=db, user_id="user-1", conversation_id="conversation-1",
            settings=settings, client=client, evaluation_client=FakeClient(raw))
        baseline = service.answer(question=question, document_ids=["document-1"])
        before = client.calls
        service.settings = settings.model_copy(update={"evidence_evaluation_mode": "shadow",
                                                      "evidence_evaluation_capture_plan": str(plan)})
        captured = service.answer(question=question, document_ids=["document-1"])
        assert len(reads) == 1
        assert client.calls == 2 * before
        assert {k:v for k,v in baseline.items() if k!='answer_id'} == {k:v for k,v in captured.items() if k!='answer_id'}
    snapshots = list((isolated / "captures").glob("R001/*/manifest.json"))
    if export_failure:assert not snapshots
    else:
        data = frozen.load_snapshot(snapshots[0].parent)
        assert data["versions"] == {k:{"status":v.status,"current_version_id":v.current_version_id} for k,v in reads[0].items()}
        observations = list(snapshots[0].parent.glob("observations/*.json"))
        assert len(observations) == 1
        assert json.loads(observations[0].read_text())["semantic_calls"]
    log_text = "".join(p.read_text() for p in (isolated / "logs").glob("*.log"))
    assert "secret-local-path" not in log_text
    assert "申报截止时间是2026年7月31日" not in log_text


def test_write_new_json_is_atomic_and_never_overwrites(isolated):
    path = isolated / "report.json"
    frozen.write_new_json(path, {"a": 1})
    with pytest.raises(FileExistsError):frozen.write_new_json(path, {"a": 2})
    assert json.loads(path.read_text()) == {"a": 1}
    assert not list(isolated.glob(".evidence-*"))


def test_workbook_prepare_uses_hash_scope_and_does_not_freeze_gold(isolated):
    from openpyxl import Workbook
    wb = Workbook();q = wb.active;q.title = "问题"
    q.append(["ID", "文件ID", "题目"]);q.append(["R001", "F01", "当前预算是多少？"])
    g = wb.create_sheet("人工判定")
    g.append(["ID", "M", "C", "O", "理由", "标注者", "复核者", "状态"])
    g.append(["R001", 1, 0, 0, "缺少预算金额", "助手", None, "已冻结"])
    workbook = isolated / "annotations.xlsx";wb.save(workbook)
    sources = isolated / "sources.json";sources.write_text(json.dumps([{"file_id":"F01","sha256":"a"*64}]))
    calls = []
    def resolve(sha):calls.append(sha);return ["doc-1"]
    cases, gold = read_workbook_plan(workbook, sources, ["R001"], resolve)
    assert calls == ["a"*64] and cases[0]["document_ids"] == ["doc-1"]
    assert gold[0]["annotation_status"] == "DRAFT" and gold[0]["input_sha256"] is None
    assert gold[0]["expected_issues"] == [{"type":"MISSING","evidence_ids":[]}]
    with pytest.raises(ValueError, match="ONE_ACTIVE"):
        read_workbook_plan(workbook, sources, ["R001"], lambda _: ["doc-1","doc-2"])
    with pytest.raises(ValueError, match="ONE_ACTIVE"):
        read_workbook_plan(workbook, sources, ["R001"], lambda _: ["doc-1","doc-1"])


def test_capture_permission_failure_never_writes_input_or_calls_semantic(isolated, monkeypatch):
    from app.tests.test_evidence_evaluation_integration import _package
    pkg = _package()
    plan = isolated / "capture-plan.json"
    frozen.write_new_json(plan, {"dataset_id":"frozen-v1","cases":[
        {"case_id":"R001","question":pkg.question,"document_ids":["document-1"]}]})
    def denied(*args):raise ValueError("VERSION_ACCESS_DENIED")
    monkeypatch.setattr(AuthorizedVersionReader,"__call__",denied)
    with _session() as db:
        _seed(db)
        client = FakeClient()
        service = EvidenceAnswerService(db=db,user_id="user-1",conversation_id="conversation-1",
            settings=_settings().model_copy(update={"database_url":"postgresql://fake:fake@localhost/file_agent_evidence_test",
                "evidence_evaluation_mode":"shadow","evidence_evaluation_capture_plan":str(plan),
                "log_dir":str(isolated/'logs')}),client=FakeEvidenceClient(),evaluation_client=client)
        service._evaluate_shadow(pkg,question=pkg.question,working_copy_ids=frozenset(["working-copy-1"]))
        assert not client.payloads
    assert not list(isolated.glob('captures/**/*'))


def test_observation_can_be_replayed_without_db_or_live_client(isolated):
    snapshot=save(isolated);gold=normal_gold(isolated,snapshot)
    client=RecordingSemanticClient(FakeClient(response()))
    SemanticChecker(client=client).check(package())
    observation=snapshot/'observations/one.json'
    frozen.write_new_json(observation,{'input_sha256':snapshot.name,'semantic_calls':client.calls,'runtime_config':{}})
    dataset=isolated/'cases.json';build(isolated,[gold],dataset,[observation])
    report=run_evaluation(load_cases(dataset),mode='replay',repetitions=3)
    assert report['evaluation_failures']==0 and report['exact_complete_matches']==1
    assert len(report['cases'][0]['rounds'])==3 and report['llm_calls']==3
    assert report['input_tokens']==0 and report['output_tokens']==0


def test_recorded_timeout_preserves_failure_category_without_exception_text():
    c=RecordingSemanticClient(FakeClient(TimeoutError('NEVER_LEAK_THIS')))
    SemanticChecker(client=c).check(package())
    case=case_for();case.recorded_semantic_calls=c.calls
    result=run_evaluation([case],mode='replay',repetitions=2)
    assert result['evaluation_failures']==1
    assert result['cases'][0]['result']['semantic_check']['error_diagnostics'][0]['category']=='TIMEOUT'
    assert result['completed_repeated_stability_rate'] is None
    assert 'NEVER_LEAK_THIS' not in json.dumps(result)


def test_derived_material_provenance_survives_dataset_build(isolated):
    pkg=package()
    capture=FrozenCapture(directory=isolated,dataset_id='frozen-v1',selection=CaptureSelection(
        case_id='R001',question=pkg.question,document_ids=['doc_1','doc_2'],source='derived'))
    snapshot=capture.save_input(package=pkg,versions={i.evidence_id:VersionSnapshot('VERIFIED',i.document_version_id)
        for i in pkg.evidence_items},original_question=pkg.question,temporal_focus='CURRENT',provenance={},config={})
    gold=normal_gold(isolated,snapshot)
    build(isolated,[gold],isolated/'derived.json',[])
    assert load_cases(isolated/'derived.json')[0].source=='derived'


def test_percentiles_use_inclusive_interpolation():
    from app.scripts.evaluate_evidence import _percentile
    assert _percentile([], .95) is None
    assert _percentile([5], .95)==5
    assert _percentile([100,10,20], .5)==20
    assert _percentile([100,10,20], .95)==pytest.approx(92)
