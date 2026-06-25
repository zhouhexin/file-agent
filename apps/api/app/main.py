"""File Agent 后端的 FastAPI 应用入口。"""

from fastapi import FastAPI
from contextlib import asynccontextmanager

from app.core.database import init_database
from app.modules.agent.router import agent_runs_router, router as agent_router
from app.modules.conversations.router import router as conversations_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化开发数据库表。

    正式部署应先执行 Alembic migration；这里保证当前本地原型服务可直接运行。
    """

    init_database()
    yield


# 这里故意保持应用入口很薄，具体业务边界交给各模块路由维护，
# 避免 Agent Runtime 扩展后把 main.py 变成混杂的调度中心。
app = FastAPI(title="File Agent API", lifespan=lifespan)
app.include_router(agent_router)
app.include_router(agent_runs_router)
app.include_router(conversations_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    """返回最小健康检查结果，用于本地和部署后的冒烟验证。"""

    return {"status": "ok"}
