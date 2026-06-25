"""应用配置。

配置集中在这里读取，避免业务模块直接访问环境变量；后续接入更多部署环境时只需要调整配置层。
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    """File Agent 后端运行配置。"""

    database_url: str = "sqlite+pysqlite:///./storage/file_agent_dev.db"
    auto_create_tables: bool = True


@lru_cache
def get_settings() -> Settings:
    """读取环境变量并返回缓存后的配置对象。"""

    return Settings(
        database_url=os.getenv("DATABASE_URL", Settings().database_url),
        auto_create_tables=os.getenv("AUTO_CREATE_TABLES", "true").lower() == "true",
    )
