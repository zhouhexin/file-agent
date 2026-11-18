"""局部原文否定校验与共享修复预算回归，保留数字、引用和拒答边界。"""

import json

import pytest

from app.db.models import DocumentChunk, EvidenceSpan, QAAnswer
from app.modules.evidence_answer.schemas import EvidenceItem, StructuredAnswer
from app.modules.evidence_answer.service import EvidenceAnswerService
from app.tests.test_evidence_answer import _session, _seed, _settings


@pytest.mark.parametrize(
    ("quote", "claim", "accepted"),
    [
        ("电子版照片为jpg格式；照片原文件不进行任何修改。", "电子版照片为jpg格式。", True),
        ("照片原文件不得修改，电子版照片为jpg格式。", "电子版照片为jpg格式。", True),
        ("照片原文件不得修改，电子版照片为jpg格式。", "照片原文件不得修改。", True),
        ("申请人不需要提交纸质材料。", "申请人需要提交纸质材料。", False),
        ("申请人需要提交纸质材料。", "申请人不需要提交纸质材料。", False),
        ("2024年申请人不需要提交纸质材料；2025年申请人需要提交纸质材料。", "2024年申请人需要提交纸质材料。", False),
        ("申请人不需要提交纸质材料；办公室需要提交经费材料。", "申请人需要提交纸质材料。", False),
        ("毕业生合影照片不仅是学校声像档案的重要组成部分。", "毕业生合影照片是学校声像档案的重要组成部分。", True),
    ],
)
def test_polarity_uses_matching_clause_not_unrelated_page_text(tmp_path, quote, claim, accepted):
    """不得因同页别的句子有‘不’拒绝正确事实，也不得用不相关肯定句掩盖否定翻转。"""

    with _session() as db:
        _seed(db)
        service = EvidenceAnswerService(
            db=db, user_id="user-1", conversation_id="conversation-1", client=object(),
            settings=_settings().model_copy(update={"log_dir": str(tmp_path)}),
        )
        evidence = EvidenceItem(
            evidence_id="ev", document_id="document-1", document_version_id="version-1",
            filename="通知.pdf", quote=quote, page_number=1,
        )
        valid, warnings = service._validate_claims(
            StructuredAnswer(claims=[{"text": claim, "evidence_ids": ["ev"]}]), [evidence], answer_mode="FOCUSED",
        )
        assert bool(valid) == accepted
        assert bool(warnings) != accepted


class SequenceClient:
    """只返回固定结构化结果，记录调用次数和安全修复请求。"""

    def __init__(self, *responses):
        """复用最后一个结果，暴露无限重试等预算错误。"""

        self.responses = responses
        self.payloads = []

    def complete_json(self, *, system_prompt, user_payload):
        """模拟模型，不调用真实接口。"""

        self.payloads.append(user_payload)
        return self.responses[min(len(self.payloads) - 1, len(self.responses) - 1)]


def _response(text):
    """模型仍须引用本次真实证据ID，修复不能绕过引用校验。"""

    return {"claims": [{"text": text, "evidence_ids": ["evidence-1"]}], "status": "COMPLETED"}


def _answer(db, tmp_path, client, *, repair_calls=1):
    """通过完整回答服务测试，使用同一份含禁止条款与肯定事实的原文。"""

    _seed(db)
    quote = "电子版照片为jpg格式；照片原文件不进行任何修改。"
    db.query(EvidenceSpan).one().quote = quote
    db.query(DocumentChunk).one().text_content = quote
    db.flush()
    return EvidenceAnswerService(
        db=db, user_id="user-1", conversation_id="conversation-1", client=client,
        settings=_settings().model_copy(update={"log_dir": str(tmp_path), "evidence_answer_repair_calls": repair_calls}),
    ).answer(question="电子版照片是什么格式？", document_ids=["document-1"])


def test_valid_affirmative_fact_on_page_with_prohibition_needs_no_repair(tmp_path):
    """正确jpg结论应直接带引用保存，不浪费修复调用。"""

    client = SequenceClient(_response("电子版照片为jpg格式。"))
    with _session() as db:
        result = _answer(db, tmp_path, client)
        assert result["status"] == "COMPLETED"
        assert "jpg" in result["answer"] and result["references"]
        assert len(client.payloads) == 1


def test_all_rejected_claims_get_one_repair_with_original_evidence(tmp_path):
    """支持性纠错消耗既有预算，正确修复后才保存结论，错误png不能泄漏到答案。"""

    client = SequenceClient(_response("电子版照片为png格式。"), _response("电子版照片为jpg格式。"))
    with _session() as db:
        result = _answer(db, tmp_path, client)
        assert result["status"] == "COMPLETED"
        assert "jpg" in result["answer"] and "png" not in result["answer"]
        assert len(client.payloads) == 2
        assert client.payloads[1]["support_validation"]["max_repairs"] == 1
        assert client.payloads[1]["evidence"] == client.payloads[0]["evidence"]
        assert db.query(QAAnswer).one().usage_json["llm_calls"] == 2
        assert db.query(QAAnswer).one().usage_json["support_repair_calls"] == 1


@pytest.mark.parametrize("repair_calls,expected_calls", [(0, 1), (1, 2)])
def test_repair_never_accepts_still_unsupported_claims_or_exceeds_budget(tmp_path, repair_calls, expected_calls):
    """修复关闭或持续给出无依据内容时仍拒答，不因想生成答案就降低门槛。"""

    secret = "sk-private-model-output-do-not-log"
    client = SequenceClient(_response(secret))
    with _session() as db:
        result = _answer(db, tmp_path, client, repair_calls=repair_calls)
        assert result["status"] == "NO_EVIDENCE" and secret not in result["answer"]
        assert len(client.payloads) == expected_calls
        assert db.query(QAAnswer).count() == 0
    logs = "\n".join(path.read_text() for path in tmp_path.glob("file-agent-*.log"))
    records = [json.loads(line) for line in logs.splitlines()]
    checks = [row for row in records if row["event"] == "evidence_answer.claims_validated"]
    assert checks and checks[0]["rejection_reason_counts"]
    assert checks[0]["rejected_claim_count"] == 1
    assert secret not in logs and "电子版照片为jpg格式" not in logs


def test_schema_repair_and_support_repair_share_one_budget(tmp_path):
    """格式修复已消耗一次预算后，不得继续追加支持性修复。"""

    client = SequenceClient({"claims": "not-a-list"}, _response("电子版照片为png格式。"))
    with _session() as db:
        result = _answer(db, tmp_path, client)
        assert result["status"] == "NO_EVIDENCE"
        assert len(client.payloads) == 2
        assert "support_validation" not in client.payloads[1]
