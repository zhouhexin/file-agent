#!/bin/sh
set -eu

echo "==> 执行数据库迁移"
python -m alembic -c apps/api/alembic.ini upgrade head

echo "==> 启动 File Agent API"
exec python -m uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips='*'
