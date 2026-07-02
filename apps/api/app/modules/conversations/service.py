"""会话消息服务。

该服务负责把 HTTP 消息持久化为 message，并启动对应的 AgentRun。
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.modules.agent.service import AgentRuntimeService
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

        attachments = list(request.attachments)
        if not attachments and _should_infer_recent_attachments(request.content):
            recent_attachments = self.repository.get_recent_attachment_references(
                conversation_id=conversation_id,
                user_id=user_id,
            )
            attachments = _select_referenced_attachments(
                content=request.content,
                recent_attachments=recent_attachments,
            )

        message = self.repository.create_user_message(
            conversation_id=conversation_id,
            user_id=user_id,
            content=request.content,
            attachments=attachments,
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


def _should_infer_recent_attachments(content: str) -> bool:
    """判断用户是否在无附件消息中引用了当前会话上文文件。"""

    reference_keywords = ["上面", "上文", "前面", "刚才", "之前", "已上传", "上传的"]
    file_task_keywords = ["文件", "附件", "文章", "读取", "总结", "讲解", "内容", "分析", "分类", "归类", "重新"]
    has_file_task = any(
        keyword in content for keyword in file_task_keywords
    )
    has_history_reference = any(keyword in content for keyword in reference_keywords)
    return has_file_task and (has_history_reference or _extract_file_ordinal(content) is not None)


def _select_referenced_attachments(
    *,
    content: str,
    recent_attachments: list[MessageAttachment],
) -> list[MessageAttachment]:
    """按用户自然语言选择上文附件；未指定序号时默认使用全部最近附件。"""

    ordinal = _extract_file_ordinal(content)
    if ordinal is None:
        return recent_attachments
    index = ordinal - 1
    if index < 0 or index >= len(recent_attachments):
        return []
    return [recent_attachments[index]]


def _extract_file_ordinal(content: str) -> int | None:
    """从“第二个文件 / 第2个文件 / 2号文件”中解析一基序号。"""

    digit_match = re.search(r"第\s*(\d+)\s*[个份]?\s*(?:文件|附件)", content)
    if digit_match:
        return int(digit_match.group(1))
    numbered_match = re.search(r"(\d+)\s*号\s*(?:文件|附件)", content)
    if numbered_match:
        return int(numbered_match.group(1))

    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    chinese_match = re.search(r"第\s*([一二两三四五六七八九十])\s*[个份]?\s*(?:文件|附件)", content)
    if chinese_match:
        return chinese_digits[chinese_match.group(1)]
    return None
