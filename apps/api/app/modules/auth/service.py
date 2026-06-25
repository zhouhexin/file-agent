"""认证业务服务。"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.modules.auth.repository import AuthRepository
from app.modules.auth.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse


class AuthService:
    """处理注册、登录和当前用户响应。"""

    def __init__(self, db: Session) -> None:
        """注入数据库会话。"""

        self.db = db
        self.repository = AuthRepository(db)

    def register(self, request: RegisterRequest) -> UserResponse:
        """注册用户并创建默认工作区。"""

        existing = self.repository.get_user_by_username(request.username)
        if existing is not None:
            raise HTTPException(status_code=409, detail="Username already exists")

        user = self.repository.create_user(
            username=request.username,
            password_hash=hash_password(request.password),
            display_name=request.display_name,
        )
        self.repository.ensure_default_workspace(user)
        self.db.commit()
        self.db.refresh(user)
        return self.to_user_response(user)

    def login(self, request: LoginRequest) -> TokenResponse:
        """校验用户名和密码，成功后返回 access token。"""

        user = self.repository.get_user_by_username(request.username)
        if user is None or not verify_password(request.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid username or password")

        self.repository.ensure_default_workspace(user)
        self.db.commit()
        self.db.refresh(user)
        return TokenResponse(
            access_token=create_access_token(user_id=user.id, role=user.role),
            user=self.to_user_response(user),
        )

    @staticmethod
    def to_user_response(user: User) -> UserResponse:
        """把 ORM User 转为对外响应，避免泄漏 password_hash。"""

        return UserResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            default_workspace_id=user.default_workspace_id,
        )
