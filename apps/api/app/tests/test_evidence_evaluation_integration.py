"""真实 ORM 与回答服务的旁观兼容测试；数据库与文件均为测试隔离对象。"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select

from app.core import config
from app.db.models import (
    AnswerReference, DocumentIndexRun, DocumentVersion, ManagedFile, ManagedFileRevision,
    ManagedRoot, QAAnswer, WorkingCopy,
)
from app.modules.evidence_answer.schemas import EvidenceItem, EvidencePackage
from app.modules.evidence_answer.service import EvidenceAnswerService
from app.modules.evidence_evaluation.service import EvidenceEvaluationService
from app.modules.evidence_evaluation.version_checker import AuthorizedVersionReader, VersionChecker
from app.scripts.evaluate_evidence import load_cases, run_evaluation
from app.tests.test_evidence_answer import _seed, _session, _settings, FakeEvidenceClient
from app.tests.test_evidence_evaluation import FakeClient, response


def _package():
    """与已有回答测试的稳定证据身份一致，不读取真实目录。"""
    return EvidencePackage(
        question="当前申报截止时间是什么？", question_type="FACT", answer_mode="FOCUSED",
        evidence_fingerprint="integration-test", evidence_items=[EvidenceItem(
            evidence_id="evidence-1", document_id="document-1", document_version_id="version-1",
            working_copy_id="working-copy-1", filename="申报通知.docx",
            quote="申报截止时间是2026年7月31日。", page_number=1,
        )],
    )


def _reader(db, *, user_id="user-1", allowed=True, source=False):
    """授权集合模拟后端已完成范围核验的数据库记录。"""
    return AuthorizedVersionReader(
        db=db, user_id=user_id, conversation_id="conversation-1", workspace_id="workspace-shared",
        working_copy_ids=frozenset(["working-copy-1"]) if allowed else frozenset(),
        revision_ids=frozenset(["source-revision-1"]) if source and allowed else frozenset(),
    )


def _new_version(db, *, document_id="document-1", version_id="version-2", ready=True):
    """创建新内容版本及索引，用版本 ID 而非时间戳证明技术版本替换。"""
    db.add(DocumentVersion(
        id=version_id, document_id=document_id, version_number=2, storage_tier="WORKING_COPY",
        storage_path="test/new.docx", filename="申报通知.docx", content_type="text/plain",
        size_bytes=128, sha256="z" * 64, source_type="IMPORT",
    ))
    if ready:
        db.add(DocumentIndexRun(
            id="index-2", document_id=document_id, document_version_id=version_id,
            extraction_run_id="extraction-1", index_version="document-chunk-index-v2",
            tokenizer="jieba", tokenizer_version="test", config_hash="z" * 64, status="COMPLETED",
        ))
    db.flush()


@pytest.mark.parametrize("ready,focus,expected", [
    (True, "CURRENT", ["OUTDATED"]), (True, "HISTORICAL", []),
    (False, "CURRENT", []), (True, "UNKNOWN", []),
])
def test_working_copy_versions_are_read_only_and_historical_safe(ready, focus, expected):
    """当前索引未就绪不强判过期，历史查询合法引用旧版本，检查不写任何事实。"""
    with _session() as db:
        copy, _ = _seed(db)
        _new_version(db, ready=ready)
        copy.current_version_id = "version-2"
        db.flush()
        statements = []
        def capture(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement.strip().split()[0].upper())
        event.listen(db.get_bind(), "before_cursor_execute", capture)
        try:
            result = VersionChecker(_reader(db)).check(_package(), temporal_focus=focus)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", capture)
        assert [i.type for i in result.issues] == expected
        assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)
        assert result.status == ("PARTIAL" if not ready or focus == "UNKNOWN" else "COMPLETED")


@pytest.mark.parametrize("scenario", ["user", "allowlist", "private_workspace", "binding"])
def test_permission_and_binding_failures_do_not_disclose_objects(scenario):
    """猜到对象 ID 也不能扩大授权；跨用户、跨工作区或版本错绑均不泄漏存在性。"""
    with _session() as db:
        copy, _ = _seed(db)
        pkg = _package()
        if scenario == "private_workspace":
            copy.workspace_id = "private-workspace"
            db.flush()
        if scenario == "binding":
            pkg.evidence_items[0].document_version_id = "not-this-document"
        reader = _reader(db, user_id="intruder" if scenario == "user" else "user-1",
                         allowed=scenario != "allowlist")
        result = VersionChecker(reader).check(pkg, temporal_focus="CURRENT")
        assert not result.issues
        assert result.status in {"PARTIAL", "EVALUATION_FAILED"}
        assert "private-workspace" not in result.model_dump_json()


@pytest.mark.parametrize("state", ["missing", "trashed", "no_current"])
def test_unavailable_working_copy_is_unverifiable(state):
    """工作副本不存在、已回收或无当前版本时，不猜测 OUTDATED。"""
    with _session() as db:
        copy, _ = _seed(db)
        if state == "missing":
            db.delete(copy)
        elif state == "trashed":
            copy.status = "TRASHED"
        else:
            copy.current_version_id = None
        db.flush()
        result = VersionChecker(_reader(db)).check(_package(), temporal_focus="CURRENT")
        assert result.status == "PARTIAL"
        assert not result.issues


def _source(db):
    """把测试事实接到受管源修订，不创建或修改真实原件。"""
    copy, _ = _seed(db)
    db.delete(copy)
    db.add(ManagedRoot(id="source-root", root_key="test", display_name="测试源", container_path="/test", enabled=True))
    db.add(ManagedFile(id="managed-file-1", root_id="source-root", relative_path="申报通知.docx",
                       filename="申报通知.docx", extension=".docx", status="ACTIVE"))
    db.add(ManagedFileRevision(
        id="source-revision-1", managed_file_id="managed-file-1", revision_number=1,
        status="READY", analysis_status="READY", is_current=True,
        analysis_document_id="document-1", analysis_document_version_id="version-1",
    ))
    db.flush()


@pytest.mark.parametrize("changed,ready,focus,expected", [
    (False, True, "CURRENT", []), (True, True, "CURRENT", ["OUTDATED"]),
    (True, False, "CURRENT", []), (True, True, "HISTORICAL", []),
])
def test_source_revision_uses_its_own_current_version_path(changed, ready, focus, expected):
    """修订 ID 不等于版本 ID；源当前修订须就绪，历史修订不等于业务失效。"""
    with _session() as db:
        _source(db)
        pkg = _package()
        pkg.evidence_items[0].working_copy_id = None
        pkg.evidence_items[0].managed_file_revision_id = "source-revision-1"
        if changed:
            _new_version(db, document_id="document-2", ready=ready)
            db.get(ManagedFileRevision, "source-revision-1").is_current = False
            db.add(ManagedFileRevision(
                id="source-revision-2", managed_file_id="managed-file-1", revision_number=2,
                status="READY" if ready else "ANALYSIS_PENDING", is_current=True,
                analysis_document_id="document-2", analysis_document_version_id="version-2",
            ))
            db.flush()
        result = VersionChecker(_reader(db, source=True)).check(pkg, temporal_focus=focus)
        assert [i.type for i in result.issues] == expected
        assert result.status != "EVALUATION_FAILED"


@pytest.mark.parametrize("source", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_shadow_preserves_answer_references_calls_and_audit(source, failure, tmp_path):
    """两条真实回答路径在评估成功/失败时均保持答案、引用和原模型调用一致。"""
    with _session() as db:
        _source(db) if source else _seed(db)
        settings = _settings().model_copy(update={"log_dir": str(tmp_path), "evidence_answer_cache_enabled": False})
        answer_client = FakeEvidenceClient()
        raw = response()
        raw["requirements"][0]["evidence_ids"] = ["evidence-1"]
        check_client = FakeClient(TimeoutError("secret-full-prompt") if failure else raw)
        service = EvidenceAnswerService(db=db, user_id="user-1", conversation_id="conversation-1",
                                        settings=settings, client=answer_client, evaluation_client=check_client)
        baseline = service.answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        original_calls = answer_client.calls
        service.settings = settings.model_copy(update={"evidence_evaluation_mode": "shadow"})
        shadow = service.answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        assert baseline["ok"] and shadow["ok"]
        assert {k: v for k, v in baseline.items() if k != "answer_id"} == {k: v for k, v in shadow.items() if k != "answer_id"}
        assert answer_client.calls == 2 * original_calls
        assert len(check_client.payloads) == 1
        assert db.query(QAAnswer).count() == 2
        assert db.query(AnswerReference).count() == 2
        assert db.query(DocumentVersion).count() == 1
        assert all("evidence_evaluation" not in (row.retrieval_trace_json or {}) for row in db.query(QAAnswer))
        records = [json.loads(line) for path in tmp_path.glob("*.log") for line in path.read_text().splitlines()]
        record = next(r for r in records if r["event"] == "evidence_evaluation.shadow")
        assert record["status"] == ("EVALUATION_FAILED" if failure else "COMPLETED")
        assert "secret-full-prompt" not in json.dumps(records)
        assert "申报截止时间是2026年7月31日" not in json.dumps(record, ensure_ascii=False)


def test_shadow_sql_failure_rolls_back_savepoint_only(monkeypatch):
    """评估 SQL 出错后主回答事务仍可保存；保存点不得触发待提交对象 flush。"""
    with _session() as db:
        copy, _ = _seed(db)
        def broken(_self, connection, _item):
            connection.exec_driver_sql("SELECT * FROM deliberately_nonexistent_evaluation_table")
        monkeypatch.setattr(AuthorizedVersionReader, "_read", broken)
        copy.filename = "尚未flush的名字.docx"
        result = VersionChecker(_reader(db)).check(_package(), temporal_focus="CURRENT")
        assert result.status == "EVALUATION_FAILED"
        persisted = db.connection().execute(select(WorkingCopy.filename)).scalar_one()
        assert persisted == "申报通知.docx"
        db.flush()
        assert db.query(WorkingCopy).one().filename == "尚未flush的名字.docx"


@pytest.mark.parametrize("case", load_cases(), ids=lambda item: item.case_id)
def test_frozen_case_replay(case):
    """64条合成标签用于工程回归，绝不把 Fake 回放 F1 宣称为真实模型准确率。"""
    report = run_evaluation([case], repetitions=2)
    assert report["exact_issue_mismatches"] == 0
    assert report["evaluation_failures"] == 0
    assert report["repeated_stability_rate"] == 1


def test_evaluation_configuration_is_explicit_and_bounded(monkeypatch, tmp_path):
    """未知运行模式和越界预算不能隐式开启模型外发。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("EVIDENCE_EVALUATION_MODE", "shadow")
    assert config.get_settings().evidence_evaluation_mode == "shadow"
    config.get_settings.cache_clear()
    monkeypatch.setenv("EVIDENCE_EVALUATION_MAX_CALLS", "99")
    with pytest.raises(RuntimeError, match="EVIDENCE_EVALUATION_MAX_CALLS"):
        config.get_settings()


def test_evaluation_default_and_unknown_mode(monkeypatch, tmp_path):
    """默认关闭；拼错开关应启动报错，不悄悄启用模型。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.delenv("EVIDENCE_EVALUATION_MODE", raising=False)
    assert config.get_settings().evidence_evaluation_mode == "off"
    config.get_settings.cache_clear()
    monkeypatch.setenv("EVIDENCE_EVALUATION_MODE", "enabled")
    with pytest.raises(ValueError):
        config.get_settings()


def test_shadow_cache_hit_does_not_rerun_evaluation(tmp_path):
    """沿用已有回答缓存，不新增评估模型调用。"""
    with _session() as db:
        _seed(db)
        raw = response()
        raw["requirements"][0]["evidence_ids"] = ["evidence-1"]
        client = FakeClient(raw)
        service = EvidenceAnswerService(
            db=db, user_id="user-1", conversation_id="conversation-1", client=FakeEvidenceClient(),
            evaluation_client=client,
            settings=_settings().model_copy(update={"evidence_evaluation_mode": "shadow", "log_dir": str(tmp_path)}),
        )
        first = service.answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        cached = service.answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        assert first["answer_id"] == cached["answer_id"]
        assert cached["cached"]
        assert len(client.payloads) == 1


def test_shadow_database_error_does_not_block_answer(monkeypatch, tmp_path):
    """在真正的回答链中触发 SQL 异常，仍须成功保存答案和引用。"""
    def broken(_self, connection, _item):
        connection.exec_driver_sql("SELECT * FROM deliberately_nonexistent_evaluation_table")
    monkeypatch.setattr(AuthorizedVersionReader, "_read", broken)
    with _session() as db:
        _seed(db)
        client = FakeClient()
        result = EvidenceAnswerService(
            db=db, user_id="user-1", conversation_id="conversation-1", client=FakeEvidenceClient(),
            evaluation_client=client,
            settings=_settings().model_copy(update={"evidence_evaluation_mode": "shadow", "log_dir": str(tmp_path)}),
        ).answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        assert result["ok"]
        assert db.query(QAAnswer).count() == db.query(AnswerReference).count() == 1
        assert not client.payloads


def test_llm_disabled_cannot_call_injected_evaluation_client(tmp_path):
    """即使注入模型对象，全局外发开关关闭时也只能核对版本。"""
    with _session() as db:
        _seed(db)
        client = FakeClient()
        result = EvidenceAnswerService(
            db=db, user_id="user-1", conversation_id="conversation-1", client=FakeEvidenceClient(),
            evaluation_client=client,
            settings=_settings().model_copy(update={"llm_enabled": False, "evidence_evaluation_mode": "shadow",
                                                    "log_dir": str(tmp_path)}),
        ).answer(question="当前申报截止时间是什么？", document_ids=["document-1"])
        assert result["ok"]
        assert not client.payloads
