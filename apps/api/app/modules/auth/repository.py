"""认证和默认工作区持久化仓库。"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.models import User, Workspace


class AuthRepository:
    """封装用户和 default workspace 的数据库操作。"""

    def __init__(self, db: Session) -> None:
        """保存请求级数据库会话。"""

        self.db = db

    def get_user_by_username(self, username: str) -> User | None:
        """按 username 查询用户。"""

        return self.db.query(User).filter(User.username == username).one_or_none()

    def get_user_by_id(self, user_id: str) -> User | None:
        """按 id 查询用户。"""

        return self.db.get(User, user_id)

    def create_user(self, username: str, password_hash: str, display_name: str) -> User:
        """创建普通用户。"""

        user = User(
            username=username,
            password_hash=password_hash,
            display_name=display_name or username,
            role="user",
        )
        self.db.add(user)
        self.db.flush()
        return user

    def ensure_default_workspace(self, user: User) -> Workspace:
        """确保用户拥有 default workspace。

        如果用户已有 default_workspace_id，则直接返回；否则创建默认工作区并回写用户。
        """

        if user.default_workspace_id:
            workspace = self.db.get(Workspace, user.default_workspace_id)
            if workspace is not None:
                return workspace

        workspace = Workspace(
            name="Default Workspace",
            owner_id=user.id,
            is_default=True,
        )
        self.db.add(workspace)
        self.db.flush()
        user.default_workspace_id = workspace.id
        self.db.flush()
        return workspace
