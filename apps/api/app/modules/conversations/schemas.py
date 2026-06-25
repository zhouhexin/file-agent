"""会话消息接口使用的请求和响应 schema。

当前 schema 只覆盖 MVP 的消息入口：用户发送文本和附件引用，然后由后端启动一次 AgentRun。
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field

from app.modules.agent.state import AgentRunResult


class MessageAttachment(BaseModel):
    """用户消息中引用的已上传文档。

    第一阶段只传 `document_id`，真实上传和权限校验会在后续 documents 模块接入。
    """

    document_id: str = Field(min_length=1)


class SendMessageRequest(BaseModel):
    """发送给文件智能体的用户消息请求体。"""

    content: str = Field(min_length=1)
    attachments: List[MessageAttachment] = Field(default_factory=list)


class ConversationMessage(BaseModel):
    """内存态 message 记录。

    后续接入数据库后，这个结构会映射到 messages 表。
    """

    id: str
    conversation_id: str
    user_id: str
    role: str
    content: str
    attachments: List[MessageAttachment]


class SendMessageResponse(BaseModel):
    """发送消息后的响应，包括 message 和本次 AgentRun 结果。"""

    message: ConversationMessage
    agent_run: AgentRunResult
