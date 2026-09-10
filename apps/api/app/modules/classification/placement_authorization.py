"""分类落位的服务端授权边界。

该模块不相信客户端传入的 actor、授权模式或任意 LLM 文本；聊天授权必须回读当前用户真实消息，
结构化入口则把已鉴权的专用提交事件本身作为明确请求。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import (
    ClassificationPlacementOperation,
    Message,
    OperationPlan,
    User,
)
from app.modules.classification.placement_schemas import (
    PlacementAction,
    PlacementAuthorizationContext,
    PlacementAuthorizationMode,
    PlacementCommand,
)


_SUGGESTION_ONLY_SIGNALS = (
    "先不要",
    "不要执行",
    "不要移动",
    "只建议",
    "仅建议",
    "给个建议",
    "看看怎么",
    "生成方案",
)
_EXECUTION_PATTERNS = {
    PlacementAction.SET_PRIMARY: (
        re.compile(r"(?:设为|设置为|改为|更正为).{0,20}(?:主分类|分类)"),
        re.compile(r"(?:主分类|分类).{0,12}(?:设为|设置为|改为|更正为)"),
        re.compile(r"(?:归类到|分类到|归入).+"),
        re.compile(r"(?:将|把).{0,30}(?:主分类|分类|归类)(?:为|到).+"),
        re.compile(r"(?:接受|确认).{0,20}(?:主分类|分类)"),
        re.compile(r"(?:主分类|分类).{0,16}(?:是对的|正确|没问题)"),
        re.compile(r"(?:这个|该).{0,12}(?:是对的|正确|没问题)"),
        re.compile(r"重新分类.{0,80}(?:整理|移动|归位)"),
    ),
    PlacementAction.MOVE: (
        re.compile(r"(?:移动到|移入|挪到|放到|归位到).+"),
        re.compile(r"按.{0,20}(?:确认)?分类(?:整理|归位|移动)"),
    ),
}


class PlacementAuthorizationError(ValueError):
    """授权证据不存在、已变化或超出窄范围时返回稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlacementAuthorizationService:
    """从已认证请求或持久化用户消息构造不可伪造的授权上下文。"""

    def __init__(self, db: Session, *, policy_version: str = "placement-auth-v1") -> None:
        self.db = db
        self.policy_version = policy_version

    def authorize_structured_request(
        self,
        *,
        command: PlacementCommand,
        current_user: User,
        workspace_id: str,
        client_id: str,
        request_id: str,
        source_event_ref: str,
        conversation_id: str | None = None,
    ) -> PlacementAuthorizationContext:
        """专用 UI/API/MCP 提交经鉴权后即构成本次 SET_PRIMARY/MOVE 授权。"""

        return self._context(
            command=command,
            actor_user_id=current_user.id,
            workspace_id=workspace_id,
            client_id=client_id,
            request_id=request_id,
            source_event_ref=source_event_ref,
            scope="CLASSIFICATION_PLACEMENT",
            mode=PlacementAuthorizationMode.EXPLICIT_REQUEST,
            conversation_id=conversation_id,
        )

    def authorize_chat_message(
        self,
        *,
        command: PlacementCommand,
        current_user: User,
        message_id: str,
        workspace_id: str,
        client_id: str,
        request_id: str,
    ) -> PlacementAuthorizationContext:
        """回读真实用户消息；只建议、否定执行或含糊表达不能授权。"""

        message = (
            self.db.query(Message)
            .filter(
                Message.id == message_id,
                Message.user_id == current_user.id,
                Message.role == "user",
            )
            .one_or_none()
        )
        if message is None:
            raise PlacementAuthorizationError(
                "AUTHORIZATION_SOURCE_NOT_FOUND",
                "找不到当前用户的原始请求消息",
            )
        content = str(message.content or "").strip()
        if any(signal in content for signal in _SUGGESTION_ONLY_SIGNALS):
            raise PlacementAuthorizationError(
                "EXPLICIT_EXECUTION_NOT_REQUESTED",
                "用户只请求建议，没有授权执行分类移动",
            )
        if not any(pattern.search(content) for pattern in _EXECUTION_PATTERNS[command.action]):
            raise PlacementAuthorizationError(
                "EXPLICIT_EXECUTION_NOT_REQUESTED",
                "原始用户消息没有明确要求执行该分类操作",
            )
        return self._context(
            command=command,
            actor_user_id=current_user.id,
            workspace_id=workspace_id,
            client_id=client_id,
            request_id=request_id,
            source_event_ref=f"message:{message.id}",
            scope="CHAT_CLASSIFICATION_PLACEMENT",
            mode=PlacementAuthorizationMode.EXPLICIT_REQUEST,
            conversation_id=message.conversation_id,
        )

    def authorize_execution(
        self,
        *,
        plan: OperationPlan,
        operation: ClassificationPlacementOperation,
        context: PlacementAuthorizationContext,
    ) -> None:
        """所有执行器的硬入口；验证冻结请求、actor、workspace 和窄范围动作。"""

        mismatched = (
            operation.operation_plan_id != plan.id
            or operation.actor_user_id != str(context.actor_user_id)
            or operation.workspace_id != str(context.workspace_id)
            or operation.client_id != context.client_id
            or operation.request_id != context.request_id
            or operation.request_digest != context.request_digest
        )
        if mismatched:
            raise PlacementAuthorizationError(
                "AUTHORIZATION_SNAPSHOT_MISMATCH",
                "执行上下文与冻结操作不一致",
            )
        if operation.operation_type not in {
            "INITIAL_ORGANIZE",
            "SET_PRIMARY",
            "MOVE",
            "RESTORE",
            "RECONCILE",
        }:
            raise PlacementAuthorizationError(
                "OPERATION_NOT_AUTHORIZED",
                "操作类型不在分类落位白名单内",
            )
        if context.authorization_mode == PlacementAuthorizationMode.EXPLICIT_REQUEST:
            if operation.operation_type not in {"SET_PRIMARY", "MOVE"}:
                raise PlacementAuthorizationError(
                    "AUTHORIZATION_SCOPE_EXCEEDED",
                    "明确分类请求不能授权删除、覆盖、改名、恢复或其他动作",
                )
            if plan.authorization_mode != "EXPLICIT_REQUEST" or plan.status not in {
                "AUTHORIZED",
                "EXECUTING",
            }:
                raise PlacementAuthorizationError(
                    "PLAN_NOT_AUTHORIZED",
                    "内部计划尚未取得直接请求授权",
                )

    def _context(
        self,
        *,
        command: PlacementCommand,
        actor_user_id: str,
        workspace_id: str,
        client_id: str,
        request_id: str,
        source_event_ref: str,
        scope: str,
        mode: PlacementAuthorizationMode,
        conversation_id: str | None,
    ) -> PlacementAuthorizationContext:
        """统一构造服务端事实；无效 UUID 或空审计引用立即失败。"""

        try:
            return PlacementAuthorizationContext(
                actor_user_id=UUID(actor_user_id),
                workspace_id=UUID(workspace_id),
                client_id=client_id,
                request_id=request_id,
                scope=scope,
                source_event_ref=source_event_ref,
                request_digest=command.request_digest(),
                authorization_mode=mode,
                policy_version=self.policy_version,
                authorized_at=datetime.now(timezone.utc),
                conversation_id=(UUID(conversation_id) if conversation_id else None),
            )
        except (TypeError, ValueError) as exc:
            raise PlacementAuthorizationError(
                "INVALID_AUTHORIZATION_CONTEXT",
                "服务端授权上下文不完整",
            ) from exc
