"""会话消息服务。

该服务负责把 HTTP 消息持久化为 message，并启动对应的 AgentRun。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.modules.agent.service import AgentRuntimeService
from app.modules.agent.state import AgentRunResult
from app.modules.conversations.context import ConversationAttachmentContextService
from app.modules.conversations.repository import ConversationRepository
from app.modules.conversations.schemas import (
    ClearConversationResponse,
    ConversationDetailResponse,
    ConversationMessage,
    SendMessageRequest,
)
from app.modules.files.repository import FileRepository


@dataclass(frozen=True)
class ConversationExecutionResult:
    """消息服务内部执行结果，供路由投影和服务层测试使用。

    `agent_run` 只在后端进程内流转，HTTP 路由必须显式转换成不含内部载荷的
    `SendMessageResponse`。
    """

    message: ConversationMessage
    agent_run: AgentRunResult


class ConversationMessageService:
    """负责创建用户 message，并启动对应的 LangGraph AgentRun。"""

    def __init__(self, db: Session, agent_service: AgentRuntimeService | None = None) -> None:
        """注入数据库会话和 AgentRuntimeService。"""

        self.db = db
        self.agent_service = agent_service or AgentRuntimeService()
        self.repository = ConversationRepository(db)

    def send_user_message(
        self,
        conversation_id: str,
        request: SendMessageRequest,
        user_id: str = "user-memory",
    ) -> ConversationExecutionResult:
        """创建持久化用户消息，并把消息交给 Agent Runtime 执行。

        HTTP 调用必须传入认证用户 ID；默认值只保留给不经过 HTTP 的最小服务测试。
        """

        attachment_context = ConversationAttachmentContextService(self.repository).resolve(
            conversation_id=conversation_id,
            user_id=user_id,
            content=request.content,
            explicit_attachments=list(request.attachments),
        )
        attachments = attachment_context.attachments

        message = self.repository.create_user_message(
            conversation_id=conversation_id,
            user_id=user_id,
            content=request.content,
            attachments=attachments,
            attachment_source=attachment_context.source,
        )
        FileRepository(self.db).lock_documents_for_message(
            document_ids=[attachment.document_id for attachment in attachments],
            user_id=user_id,
            conversation_id=conversation_id,
            message_id=message.id,
        )
        agent_run = self.agent_service.run_message(
            conversation_id=conversation_id,
            user_id=user_id,
            message_id=message.id,
            message=request.content,
            attachments=[
                {
                    **attachment.model_dump(),
                    "context_scope": attachment_context.scope,
                }
                for attachment in attachments
            ],
            db=self.db,
        )
        self.db.commit()
        self.db.refresh(message)
        return ConversationExecutionResult(
            message=self.repository.to_schema(message),
            agent_run=agent_run,
        )

    def get_conversation_detail(
        self,
        conversation_id: str,
        user_id: str,
        limit: int = 10,
        before_message_id: str | None = None,
    ) -> ConversationDetailResponse:
        """读取会话详情，供前端刷新后恢复历史聊天记录。"""

        return self.repository.get_detail(
            conversation_id=conversation_id,
            user_id=user_id,
            limit=limit,
            before_message_id=before_message_id,
        )

    def clear_conversation_history(self, *, conversation_id: str, user_id: str) -> ClearConversationResponse:
        """清空当前用户的聊天显示历史，保留文件和运行审计。"""

        cleared_count = self.repository.clear_visible_history(
            conversation_id=conversation_id,
            user_id=user_id,
        )
        self.db.commit()
        return ClearConversationResponse(
            conversation_id=conversation_id,
            cleared_message_count=cleared_count,
        )
