"""File Agent 后端的 FastAPI 应用入口。"""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.database import SessionLocal, init_database
from app.core.logging import cleanup_old_logs, log_context, log_event, new_request_id
from app.modules.agent.router import agent_runs_router, router as agent_router
from app.modules.auth.router import router as auth_router
from app.modules.changesets.router import router as changesets_router
from app.modules.chunks.router import router as chunks_router
from app.modules.classification.router import router as classification_router
from app.modules.conversations.router import router as conversations_router
from app.modules.files.router import router as files_router
from app.modules.file_rename.router import router as file_rename_router
from app.modules.file_lifecycle.router import router as file_lifecycle_router
from app.modules.file_lifecycle.scheduler import enqueue_reconciliation_jobs
from app.modules.managed_files.router import router as managed_files_router
from app.modules.operations.router import router as operations_router
from app.modules.retrieval.router import router as retrieval_router
from app.modules.knowledge_graph.classification_context import close_graph_resources
from app.modules.knowledge_graph.health import graph_health
from app.modules.knowledge_graph.projection_service import sync_graph_projection_if_enabled


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化开发数据库表。

    正式部署应先执行 Alembic migration；这里保证当前本地原型服务可直接运行。
    """

    init_database()
    cleanup_old_logs()
    settings = get_settings()
    if settings.managed_root_reconcile_on_startup and settings.filesystem_async_jobs_enabled:
        # 启动钩子只提交持久化任务；全量扫描、归档和复制由独立 worker 完成。
        with SessionLocal() as db:
            enqueue_reconciliation_jobs(db=db)
            db.commit()
    if settings.neo4j_sync_enabled:
        with SessionLocal() as db:
            sync_graph_projection_if_enabled(db=db, settings=settings)
    try:
        yield
    finally:
        close_graph_resources()


# 这里故意保持应用入口很薄，具体业务边界交给各模块路由维护，
# 避免 Agent Runtime 扩展后把 main.py 变成混杂的调度中心。
app = FastAPI(title="File Agent API", lifespan=lifespan)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """为每个请求生成 request_id，并记录 API 请求耗时和异常。"""

    request_id = request.headers.get("X-Request-ID") or new_request_id()
    start = time.perf_counter()
    with log_context(request_id=request_id):
        log_event(
            "api.request.started",
            method=request.method,
            path=request.url.path,
            message="API 请求开始",
        )
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            log_event(
                "api.request.failed",
                level="ERROR",
                status="FAILED",
                duration_ms=duration_ms,
                error_code=exc.__class__.__name__,
                method=request.method,
                path=request.url.path,
                message=str(exc),
            )
            raise

        duration_ms = int((time.perf_counter() - start) * 1000)
        response.headers["X-Request-ID"] = request_id
        log_event(
            "api.request.completed",
            status="COMPLETED" if response.status_code < 500 else "FAILED",
            duration_ms=duration_ms,
            error_code=None if response.status_code < 400 else f"HTTP_{response.status_code}",
            method=request.method,
            path=request.url.path,
            http_status=response.status_code,
            message="API 请求完成",
        )
        return response

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
app.include_router(chunks_router)
app.include_router(classification_router)
app.include_router(conversations_router)
app.include_router(files_router)
app.include_router(file_rename_router)
app.include_router(file_lifecycle_router)
app.include_router(managed_files_router)
app.include_router(operations_router)
app.include_router(retrieval_router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """返回最小健康检查结果，用于本地和部署后的冒烟验证。"""

    settings = get_settings()
    return {
        "status": "ok",
        "knowledge_graph": graph_health(settings),
    }
