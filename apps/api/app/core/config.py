"""应用配置。

配置集中在这里读取，避免业务模块直接访问环境变量；后续接入更多部署环境时只需要调整配置层。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


DEFAULT_JWT_SECRET_KEY = "file-agent-dev-secret"
DEFAULT_JWT_ALGORITHM = "HS256"
DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24


class Settings(BaseModel):
    """File Agent 后端运行配置。"""

    database_url: str
    auto_create_tables: bool = False
    jwt_secret_key: str = DEFAULT_JWT_SECRET_KEY
    jwt_algorithm: str = DEFAULT_JWT_ALGORITHM
    access_token_expire_minutes: int = DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES


def find_dotenv_file() -> Path | None:
    """从当前目录开始向上查找 `.env`，兼容项目根目录和 apps/api 目录启动。"""

    for directory in [Path.cwd(), *Path.cwd().parents]:
        env_path = directory / ".env"
        if env_path.exists():
            return env_path
    return None


def load_dotenv_file() -> None:
    """读取最近的上级 `.env`，仅填充当前进程尚未设置的环境变量。"""

    env_path = find_dotenv_file()
    if env_path is None:
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        # 跳过空行和注释，避免把说明文本误当作配置项。
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def require_postgresql_database_url() -> str:
    """读取并校验 PostgreSQL 数据库连接串，禁止静默回退到 SQLite。"""

    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required. 请在项目根目录 .env 中配置 PostgreSQL 连接。")
    if not database_url.startswith("postgresql"):
        raise RuntimeError("DATABASE_URL must use PostgreSQL，禁止使用 SQLite 作为服务数据库。")
    return database_url


@lru_cache
def get_settings() -> Settings:
    """读取环境变量并返回缓存后的配置对象。"""

    load_dotenv_file()

    return Settings(
        database_url=require_postgresql_database_url(),
        auto_create_tables=os.getenv("AUTO_CREATE_TABLES", "false").lower() == "true",
        jwt_secret_key=os.getenv("JWT_SECRET_KEY", DEFAULT_JWT_SECRET_KEY),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", DEFAULT_JWT_ALGORITHM),
        access_token_expire_minutes=int(
            os.getenv(
                "ACCESS_TOKEN_EXPIRE_MINUTES",
                str(DEFAULT_ACCESS_TOKEN_EXPIRE_MINUTES),
            ),
        ),
    )
