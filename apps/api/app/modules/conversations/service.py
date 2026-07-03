"""会话消息服务。

该服务负责把 HTTP 消息持久化为 message，并启动对应的 AgentRun。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.agent.service import AgentRuntimeService
from app.modules.conversations.context import ConversationAttachmentContextService
from app.modules.conversations.repository import ConversationRepository
from app.modules.conversations.schemas import (
    ConversationDetailResponse,
    SendMessageRequest,
    SendMessageResponse,
)
from app.modules.files.repository import FileRepository


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
    ) -> SendMessageResponse:
        """创建持久化用户消息，并把消息交给 Agent Runtime 执行。

        当前没有接认证和数据库，所以 `user_id` 使用占位值；后续接 JWT 后必须来自认证上下文。
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
                attachment.model_dump()
                for attachment in attachments
            ],
            db=self.db,
        )
        self.db.commit()
        self.db.refresh(message)
        return SendMessageResponse(message=self.repository.to_schema(message), agent_run=agent_run)

    def get_conversation_detail(self, conversation_id: str, user_id: str) -> ConversationDetailResponse:
        """读取会话详情，供前端刷新后恢复历史聊天记录。"""

        return self.repository.get_detail(conversation_id=conversation_id, user_id=user_id)
