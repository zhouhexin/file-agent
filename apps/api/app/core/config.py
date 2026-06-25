"""应用配置。

配置集中在这里读取，避免业务模块直接访问环境变量；后续接入更多部署环境时只需要调整配置层。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


class Settings(BaseModel):
    """File Agent 后端运行配置。"""

    database_url: str = "sqlite+pysqlite:///./storage/file_agent_dev.db"
    auto_create_tables: bool = True
    jwt_secret_key: str = "file-agent-dev-secret"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24


def load_dotenv_file() -> None:
    """读取项目根目录 `.env`，仅填充当前进程尚未设置的环境变量。"""

    env_path = Path.cwd() / ".env"
    if not env_path.exists():
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


@lru_cache
def get_settings() -> Settings:
    """读取环境变量并返回缓存后的配置对象。"""

    load_dotenv_file()

    return Settings(
        database_url=os.getenv("DATABASE_URL", Settings().database_url),
        auto_create_tables=os.getenv("AUTO_CREATE_TABLES", "true").lower() == "true",
        jwt_secret_key=os.getenv("JWT_SECRET_KEY", Settings().jwt_secret_key),
        jwt_algorithm=os.getenv("JWT_ALGORITHM", Settings().jwt_algorithm),
        access_token_expire_minutes=int(
            os.getenv(
                "ACCESS_TOKEN_EXPIRE_MINUTES",
                str(Settings().access_token_expire_minutes),
            ),
        ),
    )
