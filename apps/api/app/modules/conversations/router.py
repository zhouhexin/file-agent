"""会话相关 HTTP 路由。

当前只实现消息入口，用来把用户请求接入 LangGraph Agent Runtime。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.conversations.schemas import SendMessageRequest, SendMessageResponse
from app.modules.conversations.service import ConversationMessageService

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
def send_message_to_agent(
    conversation_id: str,
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SendMessageResponse:
    """接收用户消息并启动一次内存态 AgentRun。

    这里暂不接大模型和认证；数据库只用于持久化 message、AgentRun 和 ToolInvocation。
    """

    return ConversationMessageService(db=db).send_user_message(
        conversation_id=conversation_id,
        request=request,
        user_id=current_user.id,
    )
