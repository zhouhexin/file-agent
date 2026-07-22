"""会话相关 HTTP 路由。

当前只实现消息入口，用来把用户请求接入 LangGraph Agent Runtime。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.agent.user_receipt import build_user_task_receipt
from app.modules.auth.dependencies import get_current_user
from app.modules.conversations.schemas import ConversationDetailResponse, SendMessageRequest, SendMessageResponse
from app.modules.conversations.service import ConversationMessageService

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.get("/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation_detail(
    conversation_id: str,
    limit: int = Query(default=10, ge=1, le=50),
    before_message_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ConversationDetailResponse:
    """读取当前用户会话详情，用于页面刷新后恢复聊天记录。"""

    return ConversationMessageService(db=db).get_conversation_detail(
        conversation_id=conversation_id,
        user_id=current_user.id,
        limit=limit,
        before_message_id=before_message_id,
    )


@router.post("/{conversation_id}/messages", response_model=SendMessageResponse)
def send_message_to_agent(
    conversation_id: str,
    request: SendMessageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SendMessageResponse:
    """接收登录用户消息，执行 AgentRun 后只返回普通用户任务投影。"""

    execution = ConversationMessageService(db=db).send_user_message(
        conversation_id=conversation_id,
        request=request,
        user_id=current_user.id,
    )
    # 普通消息路由必须在后端完成显式投影，不能把内部 AgentRun 交给前端自行隐藏。
    return SendMessageResponse(
        message=execution.message,
        task_result=build_user_task_receipt(execution.agent_run),
    )
