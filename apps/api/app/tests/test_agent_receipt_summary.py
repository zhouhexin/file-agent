"""最终回执 LLM 表述层的安全边界测试。"""

from __future__ import annotations

from types import SimpleNamespace

from app.modules.agent.graph import response
from app.modules.llm.receipt_summary import LLMReceiptSummaryService


class _FakeReceiptClient:
    """确定性 fake，避免测试依赖真实模型服务。"""

    def __init__(self) -> None:
        """记录最终回执节点实际收到的 payload。"""

        self.payloads: list[dict] = []

    def complete_text(self, *, system_prompt: str, user_payload: dict) -> str:
        """返回不含事实字段的自然语言说明。"""

        self.payloads.append(user_payload)
        return "请查看下方经后端验证的任务明细。"


def test_unified_receipt_summary_only_receives_verified_safe_summary():
    """统一回执节点不得把文件名、路径、原文或内部标识发送给 LLM。"""

    client = _FakeReceiptClient()
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            receipt_summary_service=LLMReceiptSummaryService(
                client=client,
                enabled=True,
            ),
            document_summary_service=None,
        )
    )
    state = {
        "intent": "READ_DOCUMENT_INSIGHTS",
        "message": "读取文件洞察",
        "operation_plan_id": None,
        "result_summary": {
            "insight_documents": [
                {
                    "filename": "不应给模型的文件名.docx",
                    "storage_path": "/private/secret.docx",
                    "quote": "不应给模型的原文",
                }
            ],
            "document_results": [],
        },
    }

    result = response(state, runtime)

    assert result["status"] == "COMPLETED"
    assert result["final_response"].startswith("请查看下方经后端验证")
    assert len(client.payloads) == 1
    payload_text = str(client.payloads[0])
    assert "不应给模型" not in payload_text
    assert "storage_path" not in payload_text


def test_receipt_summary_rejects_model_claim_that_unverified_action_completed():
    """模型不得把用户目标或错误计划改写成“已经识别完成”的事实。"""

    class ClaimingClient:
        def complete_text(self, *, system_prompt: str, user_payload: dict) -> str:
            return "已识别图片内容并整理为表格。"

    runtime = SimpleNamespace(
        context=SimpleNamespace(
            receipt_summary_service=LLMReceiptSummaryService(
                client=ClaimingClient(),
                enabled=True,
            ),
            document_summary_service=None,
        )
    )
    state = {
        "intent": "READ_DOCUMENT_INSIGHTS",
        "message": "识别图片字段并用表格展示",
        "result_summary": {
            "insight_documents": [{"filename": "image.jpg"}],
            "document_results": [],
        },
    }

    result = response(state, runtime)

    assert result["final_response"] == "已读取 1 个文件的基础洞察：image.jpg。"
    assert "已识别" not in result["final_response"]


def test_evidence_answer_is_not_rewritten_by_receipt_summary_llm():
    """证据回答必须保留回答与引用的对应关系，通用回执 LLM 不能二次改写。"""

    client = _FakeReceiptClient()
    runtime = SimpleNamespace(
        context=SimpleNamespace(
            receipt_summary_service=LLMReceiptSummaryService(
                client=client,
                enabled=True,
            ),
            document_summary_service=None,
        )
    )
    state = {
        "result_summary": {
            "evidence_answer": {
                "ok": True,
                "status": "COMPLETED",
                "answer": "该结论由已验证原文证据支持。",
            },
        },
    }

    result = response(state, runtime)

    assert result["final_response"] == "该结论由已验证原文证据支持。"
    assert client.payloads == []
