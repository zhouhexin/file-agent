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
