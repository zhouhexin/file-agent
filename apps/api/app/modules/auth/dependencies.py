"""认证依赖。"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.security import TokenDecodeError, decode_access_token
from app.db.models import User
from app.modules.auth.repository import AuthRepository


def get_current_user(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> User:
    """从 Authorization Bearer token 中解析当前用户。"""

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = decode_access_token(token)
    except TokenDecodeError as exc:
        raise HTTPException(status_code=401, detail="Invalid bearer token") from exc

    user = AuthRepository(db).get_user_by_id(str(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_ops_or_admin(current_user: User = Depends(get_current_user)) -> User:
    """限制内部运行审计接口只允许 ops 和 admin 角色访问。

    普通用户只能消费经过脱敏的任务回执，不能通过独立审计接口绕过投影边界读取
    Planner、Skill、ToolInvocation 或原始 Tool 输出。
    """

    if current_user.role not in {"ops", "admin"}:
        raise HTTPException(status_code=403, detail="Insufficient role")
    return current_user
