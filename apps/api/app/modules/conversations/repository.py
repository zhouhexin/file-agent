"""会话消息持久化仓库。

Service 通过仓库写入 message，避免 HTTP 路由或 AgentRuntimeService 直接操作 ORM。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import Conversation, Message
from app.modules.conversations.schemas import ConversationMessage, MessageAttachment


class ConversationRepository:
    """封装 conversation 和 message 的最小持久化操作。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def ensure_conversation(self, conversation_id: str, user_id: str) -> Conversation:
        """确保会话存在。

        当前阶段没有 workspace 和认证，允许按 URL 中的 conversation_id 自动创建占位会话。
        """

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is not None:
            return conversation
        conversation = Conversation(id=conversation_id, user_id=user_id, title="")
        self.db.add(conversation)
        self.db.flush()
        return conversation

    def create_user_message(
        self,
        conversation_id: str,
        user_id: str,
        content: str,
        attachments: list[MessageAttachment],
    ) -> Message:
        """创建用户消息并保存附件引用 JSON。"""

        self.ensure_conversation(conversation_id=conversation_id, user_id=user_id)
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=content,
            attachments_json=[attachment.model_dump() for attachment in attachments],
        )
        self.db.add(message)
        self.db.flush()
        return message

    @staticmethod
    def to_schema(message: Message) -> ConversationMessage:
        """把 ORM Message 转为 API 响应 schema。"""

        return ConversationMessage(
            id=message.id,
            conversation_id=message.conversation_id,
            role=message.role,
            content=message.content,
            attachments=[
                MessageAttachment.model_validate(item)
                for item in message.attachments_json
            ],
        )
