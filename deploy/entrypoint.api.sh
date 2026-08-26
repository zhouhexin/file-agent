#!/bin/sh
set -eu

APP_RUNTIME="${APP_RUNTIME:-api}"
umask 027

case "$APP_RUNTIME" in
  migrate)
    echo "==> 执行唯一数据库迁移"
    exec python -m alembic -c apps/api/alembic.ini upgrade head
    ;;
  api)
    python /app/deploy/scripts/verify_runtime.py
    echo "==> 启动 File Agent API"
    exec python -m uvicorn app.main:app \
      --host 0.0.0.0 \
      --port 8000 \
      --proxy-headers \
      --forwarded-allow-ips='*'
    ;;
  filesystem-worker)
    python /app/deploy/scripts/verify_runtime.py --managed-root
    echo "==> 启动文件系统 worker：${FILESYSTEM_WORKER_QUEUES:-未配置队列}"
    exec python -m app.modules.managed_files.worker
    ;;
  scheduler)
    python /app/deploy/scripts/verify_runtime.py --managed-root
    echo "==> 启动受管目录对账调度器"
    exec python -m app.modules.file_lifecycle.scheduler
    ;;
  watcher)
    python /app/deploy/scripts/verify_runtime.py --managed-root
    echo "==> 启动受管目录 watcher"
    exec python -m app.modules.file_lifecycle.watcher
    ;;
  *)
    echo "未知 APP_RUNTIME：$APP_RUNTIME" >&2
    exit 64
    ;;
esac
