"""执行 WorkBuddy 批次在默认整理完成后附带的用户请求。

每次执行绑定持久化的条目和最终 Document ID 集合。模块复用正式会话与 Agent Runtime，
但不会重新触发上传、分类或默认导入，也不会让客户端提供任意后端文件范围。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import AgentRun, IngestBatch, IngestRequestExecution, Message
from app.modules.agent.user_receipt import build_user_task_receipt
from app.modules.conversations.schemas import MessageAttachment, SendMessageRequest
from app.modules.conversations.service import ConversationMessageService


class IngestRequestRunner:
    """把一个固定范围的批次附带请求交给受控 Agent Runtime。"""

    def __init__(self, db: Session) -> None:
        """保存 worker 请求级数据库会话。"""

        self.db = db

    def run(self, *, execution_id: str) -> IngestRequestExecution:
        """幂等执行请求；已有 AgentRun 时只重建安全回执，不重复调用 Agent。"""

        execution = self.db.get(IngestRequestExecution, execution_id)
        if execution is None:
            raise RuntimeError("批次附带请求执行记录不存在")
        batch = self.db.get(IngestBatch, execution.batch_id)
        if batch is None or not str(batch.user_request or "").strip():
            raise RuntimeError("批次附带请求不存在")
        recovered = self._recover_committed_run(execution=execution, batch=batch)
        if recovered is not None and not execution.agent_run_id:
            # Agent Runtime 自身事务可能已提交，而 worker 在写回 execution 前退出；
            # 通过固定会话、请求文本和附件集合恢复，避免再次执行同一用户任务。
            execution.agent_run_id = recovered.id
        if execution.agent_run_id:
            run = self.db.get(AgentRun, execution.agent_run_id)
            if run is None:
                raise RuntimeError("附带请求映射的 AgentRun 不存在")
            resolved_status = "FAILED" if run.status == "FAILED" else "COMPLETED"
            if execution.status != resolved_status:
                batch.result_revision += 1
            execution.status = resolved_status
            execution.result_json = self._receipt_from_persisted_run(run)
            self.db.flush()
            return execution

        execution.status = "RUNNING"
        conversation_id = batch.conversation_id or batch.id
        execution.conversation_id = conversation_id
        batch.conversation_id = conversation_id
        self.db.flush()
        # 附件只来自已完成 ingest_items 的最终映射，不接受 MCP 再次传入文件 ID。
        request = SendMessageRequest(
            content=str(batch.user_request).strip(),
            attachments=[
                MessageAttachment(document_id=str(document_id))
                for document_id in list(execution.document_ids_json or [])
            ],
        )
        try:
            result = ConversationMessageService(db=self.db).send_user_message(
                conversation_id=conversation_id,
                request=request,
                user_id=batch.user_id,
            )
        except Exception as exc:
            execution.status = "FAILED"
            execution.error_json = {
                "code": exc.__class__.__name__,
                "message": "附带请求执行失败，可通过批次恢复接口重试。",
            }
            self.db.flush()
            raise
        execution.agent_run_id = result.agent_run.agent_run_id
        # NEEDS_REVIEW 表示 Agent 已给出“依据不足”等受控结果，不是执行基础设施失败。
        execution.status = "FAILED" if result.agent_run.status == "FAILED" else "COMPLETED"
        receipt = build_user_task_receipt(result.agent_run, db=self.db)
        execution.result_json = receipt.model_dump(mode="json")
        execution.error_json = {}
        batch.result_revision += 1
        self.db.flush()
        return execution

    def _recover_committed_run(
        self,
        *,
        execution: IngestRequestExecution,
        batch: IngestBatch,
    ) -> AgentRun | None:
        """查找崩溃窗口中已经提交但尚未回写执行映射的唯一 AgentRun。"""

        conversation_id = execution.conversation_id or batch.conversation_id or batch.id
        candidates = (
            self.db.query(AgentRun, Message)
            .join(Message, Message.id == AgentRun.message_id)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.user_id == batch.user_id,
                Message.role == "user",
                Message.content == str(batch.user_request or "").strip(),
                Message.created_at >= execution.created_at,
            )
            .order_by(Message.created_at.asc())
            .all()
        )
        expected = set(str(value) for value in list(execution.document_ids_json or []))
        matches = [
            run
            for run, message in candidates
            if {
                str(attachment.get("document_id") or "")
                for attachment in list(message.attachments_json or [])
                if isinstance(attachment, dict)
            }
            == expected
        ]
        if len(matches) > 1:
            raise RuntimeError("附带请求恢复发现多个候选 AgentRun，需要人工检查")
        return matches[0] if matches else None

    def _receipt_from_persisted_run(self, run: AgentRun) -> dict:
        """从持久化运行恢复最小安全结果，绝不返回原始 Tool 输入输出。"""

        return {
            "task_id": run.id,
            "task_status": "failed" if run.status == "FAILED" else "completed",
            "response_type": "text",
            "final_response": run.final_response,
            "document_results": list((run.graph_state_json or {}).get("document_results") or []),
        }
