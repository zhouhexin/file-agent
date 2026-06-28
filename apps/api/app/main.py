"""File Agent 后端的 FastAPI 应用入口。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import init_database
from app.modules.agent.router import agent_runs_router, router as agent_router
from app.modules.auth.router import router as auth_router
from app.modules.changesets.router import router as changesets_router
from app.modules.conversations.router import router as conversations_router
from app.modules.files.router import router as files_router

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

# 允许本地 Vite 前端跨端口调用 API；生产环境应改为正式域名白名单。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agent_router)
app.include_router(agent_runs_router)
app.include_router(auth_router)
app.include_router(changesets_router)
app.include_router(conversations_router)
app.include_router(files_router)


@app.get("/api/health")
def health() -> dict[str, str]:
    """返回最小健康检查结果，用于本地和部署后的冒烟验证。"""

    return {"status": "ok"}
