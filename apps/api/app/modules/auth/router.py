"""认证 HTTP 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.db.models import User
from app.modules.auth.dependencies import get_current_user
from app.modules.auth.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.modules.auth.service import AuthService

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register", response_model=UserResponse)
def register(request: RegisterRequest, db: Session = Depends(get_db)) -> UserResponse:
    """注册用户并创建 default workspace。"""

    return AuthService(db).register(request)


@router.post("/login", response_model=TokenResponse)
def login(request: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """登录并返回 bearer access token。"""

    return AuthService(db).login(request)


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)) -> UserResponse:
    """返回当前登录用户信息。"""

    return AuthService.to_user_response(current_user)
