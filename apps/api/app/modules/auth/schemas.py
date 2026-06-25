"""认证模块请求和响应 schema。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """用户注册请求。"""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=6)
    display_name: str = ""


class LoginRequest(BaseModel):
    """用户登录请求。"""

    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    """对外返回的用户信息，不包含 password_hash。"""

    id: str
    username: str
    display_name: str
    role: str
    default_workspace_id: Optional[str]


class TokenResponse(BaseModel):
    """登录成功返回的 bearer token 和用户信息。"""

    access_token: str
    token_type: str = "bearer"
    user: UserResponse
