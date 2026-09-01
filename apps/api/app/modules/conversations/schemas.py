"""会话消息接口使用的请求和响应 schema。

当前 schema 只覆盖 MVP 的消息入口：用户发送文本和附件引用，然后由后端启动一次 AgentRun。
"""

from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field, field_validator

from app.modules.agent.user_receipt import UserTaskReceipt
from app.modules.files.upload_paths import normalize_upload_relative_path


class MessageAttachment(BaseModel):
    """用户消息中引用的已上传文档。

    第一阶段只传 `document_id`，真实上传和权限校验会在后续 documents 模块接入。
    """

    document_id: str = Field(min_length=1)
    relative_path: str | None = Field(default=None, max_length=1024)

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str | None) -> str | None:
        """消息只能保存经过校验的展示路径，不能接受目录穿越或绝对路径。"""

        return normalize_upload_relative_path(value)


class SendMessageRequest(BaseModel):
    """发送给文件智能体的用户消息请求体。"""

    content: str = Field(min_length=1)
    attachments: List[MessageAttachment] = Field(default_factory=list)


class ConversationMessageAttachment(BaseModel):
    """即时消息响应维持稳定的最小附件引用契约。"""

    document_id: str


class ConversationMessage(BaseModel):
    """内存态 message 记录。

    后续接入数据库后，这个结构会映射到 messages 表。
    """

    id: str
    conversation_id: str
    user_id: str
    role: str
    content: str
    attachments: List[ConversationMessageAttachment]


class ConversationAttachmentSummary(BaseModel):
    """历史消息附件摘要，用于前端刷新后恢复附件展示。"""

    document_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: str
    ingest_status: str
    deduplicated: bool = False
    working_copy_id: str | None = None
    working_copy_status: str | None = None
    file_availability: str = "UNAVAILABLE"
    availability_message: str | None = None
    can_open: bool = False
    can_restore: bool = False
    relative_path: str | None = None


class ConversationHistoryMessage(BaseModel):
    """会话详情中的历史消息，只携带普通用户任务投影。"""

    id: str
    conversation_id: str
    user_id: str
    role: str
    content: str
    attachments: List[ConversationAttachmentSummary]
    metadata: List[dict[str, Any]] = Field(default_factory=list)
    task_result: UserTaskReceipt | None = None


class ConversationPagination(BaseModel):
    """会话历史分页信息，供前端向上滚动继续加载更早消息。"""

    has_more: bool
    oldest_message_id: str | None = None
    limit: int


class ConversationDetailResponse(BaseModel):
    """会话详情响应，用于前端刷新后恢复聊天记录。"""

    id: str
    user_id: str
    title: str
    status: str
    messages: List[ConversationHistoryMessage]
    pagination: ConversationPagination


class ClearConversationResponse(BaseModel):
    """清空会话可见消息后的结果。

    清空只影响聊天记录展示；文件、工作副本与 Agent 审计记录仍被保留，
    以避免“删除对话”意外删除用户已经整理的文件。
    """

    conversation_id: str
    cleared_message_count: int


class SendMessageResponse(BaseModel):
    """发送消息后的普通用户响应，不暴露 AgentRun 或 Tool 内部载荷。"""

    message: ConversationMessage
    task_result: UserTaskReceipt
