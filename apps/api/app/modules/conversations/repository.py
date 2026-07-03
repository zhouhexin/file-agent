"""会话消息持久化仓库。

Service 通过仓库写入 message，避免 HTTP 路由或 AgentRuntimeService 直接操作 ORM。
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from fastapi import HTTPException

from app.db.models import AgentRun, Conversation, Document, Message
from app.modules.agent.repository import AgentRunRepository
from app.modules.conversations.schemas import (
    ConversationAttachmentSummary,
    ConversationDetailResponse,
    ConversationHistoryMessage,
    ConversationMessage,
    MessageAttachment,
)


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
            if conversation.user_id != user_id:
                raise HTTPException(status_code=403, detail="Conversation belongs to another user")
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
        attachment_source: str = "uploaded",
    ) -> Message:
        """创建用户消息并保存附件引用 JSON。"""

        self.ensure_conversation(conversation_id=conversation_id, user_id=user_id)
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=content,
            # attachments_json 是消息上下文的一部分，额外保存 source 用于区分真实上传和后端自动补齐。
            attachments_json=[
                {
                    **attachment.model_dump(),
                    "source": attachment_source,
                }
                for attachment in attachments
            ],
        )
        self.db.add(message)
        self.db.flush()
        return message

    def get_recent_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
        limit: int = 10,
    ) -> list[MessageAttachment]:
        """读取当前会话最近消息中的附件引用，供“上面上传的文件”这类表达复用。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
            .all()
        )
        document_ids: list[str] = []
        seen: set[str] = set()
        for message in messages:
            for item in message.attachments_json:
                document_id = item.get("document_id") if isinstance(item, dict) else None
                if document_id and document_id not in seen:
                    seen.add(document_id)
                    document_ids.append(document_id)
        if not document_ids:
            return []

        owned_documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        owned_ids = {document.id for document in owned_documents}
        return [
            MessageAttachment(document_id=document_id)
            for document_id in document_ids
            if document_id in owned_ids
        ]

    def get_latest_attachment_batch_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
        limit: int = 20,
    ) -> list[MessageAttachment]:
        """读取当前会话最近一条带附件消息中的整批附件，用于“刚刚上传的文件”。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
            .all()
        )
        document_ids: list[str] = []
        seen: set[str] = set()
        for message in messages:
            if not message.attachments_json:
                continue
            if _is_inferred_attachment_message(message):
                continue
            for item in message.attachments_json:
                document_id = item.get("document_id") if isinstance(item, dict) else None
                if document_id and document_id not in seen:
                    seen.add(document_id)
                    document_ids.append(document_id)
            break
        if not document_ids:
            return []

        owned_documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        owned_ids = {document.id for document in owned_documents}
        return [
            MessageAttachment(document_id=document_id)
            for document_id in document_ids
            if document_id in owned_ids
        ]

    def get_conversation_for_user(self, conversation_id: str, user_id: str) -> Conversation:
        """读取当前用户自己的会话，不存在或越权时返回明确错误。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")
        return conversation

    def get_detail(self, conversation_id: str, user_id: str) -> ConversationDetailResponse:
        """组装会话详情，包含消息、附件摘要和每条消息对应的 AgentRun。"""

        conversation = self.get_conversation_for_user(conversation_id=conversation_id, user_id=user_id)
        messages = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        document_map = self._load_document_map(messages=messages, user_id=user_id)
        agent_run_map = self._load_agent_run_map(messages=messages)
        agent_repository = AgentRunRepository(self.db)
        return ConversationDetailResponse(
            id=conversation.id,
            user_id=conversation.user_id,
            title=conversation.title,
            status=conversation.status,
            messages=[
                ConversationHistoryMessage(
                    id=message.id,
                    conversation_id=message.conversation_id,
                    user_id=message.user_id,
                    role=message.role,
                    content=message.content,
                    attachments=[
                        self._attachment_to_summary(item=item, document_map=document_map)
                        for item in message.attachments_json
                    ],
                    agent_run=(
                        agent_repository.to_result(agent_run_map[message.id])
                        if message.id in agent_run_map
                        else None
                    ),
                )
                for message in messages
            ],
        )

    def _load_document_map(self, *, messages: list[Message], user_id: str) -> dict[str, Document]:
        """批量加载历史消息引用的文档，避免逐条消息查询。"""

        document_ids = {
            item.get("document_id")
            for message in messages
            for item in message.attachments_json
            if isinstance(item, dict) and item.get("document_id")
        }
        if not document_ids:
            return {}
        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        return {document.id: document for document in documents}

    def _load_agent_run_map(self, *, messages: list[Message]) -> dict[str, AgentRun]:
        """按 message_id 取最新 AgentRun，供历史会话恢复助手回复。"""

        message_ids = [message.id for message in messages]
        if not message_ids:
            return {}
        runs = (
            self.db.query(AgentRun)
            .filter(AgentRun.message_id.in_(message_ids))
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .all()
        )
        return {run.message_id: run for run in runs}

    @staticmethod
    def _attachment_to_summary(
        *,
        item: dict,
        document_map: dict[str, Document],
    ) -> ConversationAttachmentSummary:
        """把消息中的 document_id 引用扩展为前端可展示的附件摘要。"""

        document_id = item.get("document_id", "")
        document = document_map.get(document_id)
        if document is None:
            return ConversationAttachmentSummary(
                document_id=document_id,
                filename=document_id or "未知文件",
                content_type="application/octet-stream",
                size_bytes=0,
                sha256="",
                status="MISSING",
                ingest_status="FAILED",
            )
        return ConversationAttachmentSummary(
            document_id=document.id,
            filename=document.original_filename,
            content_type=document.content_type,
            size_bytes=document.size_bytes,
            sha256=document.sha256,
            status=document.status,
            ingest_status=document.ingest_status,
        )

    @staticmethod
    def to_schema(message: Message) -> ConversationMessage:
        """把 ORM Message 转为 API 响应 schema。"""

        return ConversationMessage(
            id=message.id,
            conversation_id=message.conversation_id,
            user_id=message.user_id,
            role=message.role,
            content=message.content,
            attachments=[
                MessageAttachment.model_validate(item)
                for item in message.attachments_json
            ],
        )


def _is_inferred_attachment_message(message: Message) -> bool:
    """判断消息附件是否来自后端上下文推断，避免污染“最近上传批次”。"""

    attachments = [
        item
        for item in message.attachments_json
        if isinstance(item, dict) and item.get("document_id")
    ]
    if not attachments:
        return False
    sources = {item.get("source") for item in attachments}
    if "uploaded" in sources:
        return False
    if "inferred_context" in sources:
        return True
    return _looks_like_context_reference_message(message.content)


def _looks_like_context_reference_message(content: str) -> bool:
    """兼容历史数据：旧消息没有 source 时，用文本判断是否是上下文引用消息。"""

    reference_keywords = ["上面", "上文", "前面", "刚才", "刚刚", "刚上传", "之前", "已上传", "上传的"]
    file_task_keywords = ["文件", "附件", "文章", "读取", "总结", "讲解", "内容", "分析", "分类", "归类", "重新"]
    return any(keyword in content for keyword in reference_keywords) and any(
        keyword in content for keyword in file_task_keywords
    )
