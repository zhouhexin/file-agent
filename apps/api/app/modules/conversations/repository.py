"""会话消息持久化仓库。

Service 通过仓库写入 message，避免 HTTP 路由或 AgentRuntimeService 直接操作 ORM。
"""

from __future__ import annotations

import re
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.models import AgentRun, Conversation, Document, Message
from app.modules.agent.repository import AgentRunRepository
from app.modules.conversations.schemas import (
    ConversationAttachmentSummary,
    ConversationDetailResponse,
    ConversationHistoryMessage,
    ConversationMessage,
    ConversationPagination,
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
        batch_id = str(uuid4()) if attachments and attachment_source == "uploaded" else None
        message = Message(
            conversation_id=conversation_id,
            user_id=user_id,
            role="user",
            content=content,
            # attachments_json 是消息上下文的一部分，source/batch_id 用于区分真实上传批次和后端自动补齐。
            attachments_json=[
                {
                    **attachment.model_dump(),
                    "source": attachment_source,
                    **({"batch_id": batch_id} if batch_id else {}),
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

    def get_all_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
    ) -> list[MessageAttachment]:
        """读取当前会话全部真实或上下文附件，用于“之前所有文件”这类表达。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        document_ids = _collect_unique_document_ids(messages=messages)
        if not document_ids:
            return []
        return self._filter_owned_attachment_references(document_ids=document_ids, user_id=user_id)

    def get_filename_matched_attachment_references(
        self,
        *,
        conversation_id: str,
        user_id: str,
        content: str,
    ) -> list[MessageAttachment]:
        """按用户消息里的文件名片段匹配当前会话历史附件。"""

        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            return []
        if conversation.user_id != user_id:
            raise HTTPException(status_code=403, detail="Conversation belongs to another user")

        messages = (
            self.db.query(Message)
            .filter(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .all()
        )
        document_ids = _collect_unique_document_ids(messages=messages)
        if not document_ids:
            return []

        documents = (
            self.db.query(Document)
            .filter(Document.id.in_(document_ids), Document.user_id == user_id)
            .all()
        )
        documents_by_id = {document.id: document for document in documents}
        normalized_content = _normalize_filename_match_text(content)
        matched_ids: list[str] = []
        for document_id in document_ids:
            document = documents_by_id.get(document_id)
            if document is None:
                continue
            if _filename_matches_content(
                filename=document.original_filename,
                content=content,
                normalized_content=normalized_content,
            ):
                matched_ids.append(document_id)
        return [MessageAttachment(document_id=document_id) for document_id in matched_ids]

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
            uploaded_items = _uploaded_attachment_items(message)
            if not uploaded_items:
                continue
            batch_id = uploaded_items[0].get("batch_id")
            if batch_id:
                uploaded_items = [item for item in uploaded_items if item.get("batch_id") == batch_id]
            for item in uploaded_items:
                document_id = item.get("document_id") if isinstance(item, dict) else None
                if document_id and document_id not in seen:
                    seen.add(document_id)
                    document_ids.append(document_id)
            break
        if not document_ids:
            return []
        return self._filter_owned_attachment_references(document_ids=document_ids, user_id=user_id)

    def _filter_owned_attachment_references(
        self,
        *,
        document_ids: list[str],
        user_id: str,
    ) -> list[MessageAttachment]:
        """按当前用户过滤附件引用，防止越权文档进入 Agent 上下文。"""

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

    def get_detail(
        self,
        conversation_id: str,
        user_id: str,
        limit: int = 10,
        before_message_id: str | None = None,
    ) -> ConversationDetailResponse:
        """组装会话详情，包含消息、附件摘要和每条消息对应的 AgentRun。"""

        conversation = self.get_conversation_for_user(conversation_id=conversation_id, user_id=user_id)
        messages, has_more = self._load_message_page(
            conversation_id=conversation_id,
            limit=limit,
            before_message_id=before_message_id,
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
                        if isinstance(item, dict) and item.get("document_id")
                    ],
                    metadata=[
                        dict(item)
                        for item in message.attachments_json
                        if isinstance(item, dict) and not item.get("document_id")
                    ],
                    agent_run=(
                        agent_repository.to_result(agent_run_map[message.id])
                        if message.id in agent_run_map
                        else None
                    ),
                )
                for message in messages
            ],
            pagination=ConversationPagination(
                has_more=has_more,
                oldest_message_id=messages[0].id if messages else None,
                limit=limit,
            ),
        )

    def _load_message_page(
        self,
        *,
        conversation_id: str,
        limit: int,
        before_message_id: str | None,
    ) -> tuple[list[Message], bool]:
        """读取一页消息。

        数据库查询用倒序拿最近记录，返回前再恢复时间正序，前端可直接追加渲染。
        """

        query = self.db.query(Message).filter(Message.conversation_id == conversation_id)
        if before_message_id:
            before_message = (
                self.db.query(Message)
                .filter(Message.conversation_id == conversation_id, Message.id == before_message_id)
                .one_or_none()
            )
            if before_message is None:
                raise HTTPException(status_code=404, detail="Message not found")
            query = query.filter(
                or_(
                    Message.created_at < before_message.created_at,
                    (Message.created_at == before_message.created_at) & (Message.id < before_message.id),
                )
            )
        rows = (
            query.order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        page.reverse()
        return page, has_more

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


def _uploaded_attachment_items(message: Message) -> list[dict]:
    """读取消息中的真实上传附件项，跳过后端自动补齐的上下文附件。"""

    attachments = [
        item
        for item in message.attachments_json
        if isinstance(item, dict) and item.get("document_id")
    ]
    if not attachments:
        return []
    uploaded_items = [item for item in attachments if item.get("source") == "uploaded"]
    if uploaded_items:
        return uploaded_items
    if any(item.get("source") == "inferred_context" for item in attachments):
        return []
    if _looks_like_context_reference_message(message.content):
        return []
    return attachments


def _collect_unique_document_ids(*, messages: list[Message]) -> list[str]:
    """按消息顺序收集去重后的 document_id，供全会话附件范围使用。"""

    document_ids: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for item in message.attachments_json:
            document_id = item.get("document_id") if isinstance(item, dict) else None
            if document_id and document_id not in seen:
                seen.add(document_id)
                document_ids.append(document_id)
    return document_ids


def _looks_like_context_reference_message(content: str) -> bool:
    """兼容历史数据：旧消息没有 source 时，用文本判断是否是上下文引用消息。"""

    reference_keywords = ["上面", "上文", "前面", "刚才", "刚刚", "刚上传", "之前", "已上传", "上传的"]
    file_task_keywords = ["文件", "附件", "文章", "读取", "总结", "讲解", "内容", "分析", "分类", "归类", "重新"]
    return any(keyword in content for keyword in reference_keywords) and any(
        keyword in content for keyword in file_task_keywords
    )


def _filename_matches_content(*, filename: str, content: str, normalized_content: str) -> bool:
    """判断文件名、主干或关键片段是否出现在用户消息中。"""

    normalized_filename = _normalize_filename_match_text(filename)
    stem = _normalize_filename_match_text(re.sub(r"\.[^.]{1,12}$", "", filename))
    candidates = [value for value in {normalized_filename, stem} if len(value) >= 4]
    if any(candidate in normalized_content for candidate in candidates):
        return True

    filename_years = set(re.findall(r"(?:19|20)\d{2}", filename))
    content_years = set(re.findall(r"(?:19|20)\d{2}", content))
    if filename_years and content_years and filename_years.isdisjoint(content_years):
        return False

    tokens = _filename_fuzzy_tokens(stem)
    matched_tokens = [token for token in tokens if token in normalized_content]
    required_matches = 2 if len(tokens) <= 4 else 3
    return len(matched_tokens) >= required_matches


def _normalize_filename_match_text(value: str) -> str:
    """归一化文件名匹配文本，降低空格、括号和分隔符带来的影响。"""

    lowered = value.lower()
    return re.sub(r"[\s\-_—–《》【】\[\]（）()，,。.:：;；/\\]+", "", lowered)


def _filename_fuzzy_tokens(stem: str) -> set[str]:
    """从文件名主干提取用于模糊匹配的中文、数字和英文片段。"""

    stop_tokens = {
        "文件",
        "材料",
        "资料",
        "表格",
        "汇总",
        "汇总表",
        "统计",
        "整理",
        "学院",
        "学校",
        "年度",
    }
    tokens: set[str] = set(re.findall(r"(?:19|20)\d{2}|[a-z]{2,}", stem))
    for chinese_part in re.findall(r"[\u4e00-\u9fff]{2,}", stem):
        for size in (4, 3, 2):
            for index in range(0, max(len(chinese_part) - size + 1, 0)):
                token = chinese_part[index : index + size]
                if token not in stop_tokens:
                    tokens.add(token)
    return tokens
