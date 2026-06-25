# File Agent Runbook

本文记录当前项目的本地启动、验证方式和可用接口。后续如果端口、命令、环境依赖、启动顺序或接口能力发生变化，必须同步更新本文和 `README.md`。

## 1. Python 环境

后端使用用户当前已经配置好的 `python3` 环境，不强制创建新虚拟环境，不强制切换到 `uv`、Poetry、Conda 或其他包管理方式。

当前已验证的运行方式是在项目根目录执行命令，避免进入 `apps/api` 后 shell 解析到不同的 Python 解释器。

## 2. 运行测试

在项目根目录执行：

```bash
python3 -m pytest
```

当前期望结果：

```text
20 passed
```

如果出现 `urllib3` 或 `LangChainPendingDeprecationWarning`，目前属于环境兼容警告，不影响现有测试结果。

## 3. 数据库

当前后端已经持久化 user、default workspace、message、AgentRun 和 ToolInvocation。

默认本地开发库：

```text
sqlite+pysqlite:///./storage/file_agent_dev.db
```

如需使用 PostgreSQL + pgvector：

```bash
docker compose up -d postgres
export DATABASE_URL='postgresql+psycopg2://file_agent:file_agent_dev@127.0.0.1:5432/file_agent'
export AUTO_CREATE_TABLES=false
python3 -m alembic -c apps/api/alembic.ini upgrade head
```

本地 SQLite 也可以执行 migration：

```bash
python3 -m alembic -c apps/api/alembic.ini upgrade head
```

当前开发阶段 `AUTO_CREATE_TABLES` 默认为 `true`，用于让本地原型服务直接启动。正式环境应设置为 `false` 并使用 Alembic migration。

如果本地旧 SQLite 开发库缺少新字段，可以执行 migration；仍有 schema 冲突时可以删除 `storage/file_agent_dev.db` 后重新启动服务。

## 4. 启动后端服务

在项目根目录执行：

```bash
PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

服务地址：

```text
http://127.0.0.1:8000
```

message、AgentRun 和 ToolInvocation 会写入当前 `DATABASE_URL` 指向的数据库。

## 5. 启动前端服务

首次启动前安装依赖：

```bash
cd apps/web
npm install
```

启动前端开发服务：

```bash
npm run dev
```

前端地址：

```text
http://127.0.0.1:5173
```

Vite 开发端口已固定为 `5173`，不会自动切换到其他端口。如果该端口被占用，请先停止占用进程；确实需要改端口时，必须同步更新 Vite 配置、`VITE_API_BASE_URL` 和后端 CORS 白名单。

前端当前能力：

```text
注册用户
登录用户
保存 access_token 到 localStorage
启动时调用 /api/auth/me 校验登录态
进入 /chat
发送一条消息到 AgentRun
展示 AgentRun 状态、intent 和 Tool 调用列表
退出登录
```

默认 API 地址：

```text
http://127.0.0.1:8000/api
```

如需改后端地址，可设置：

```bash
VITE_API_BASE_URL=http://127.0.0.1:8000/api npm run dev
```

## 6. 当前可用接口

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

期望返回：

```json
{"status":"ok"}
```

查看 MVP Tool 白名单：

```bash
curl http://127.0.0.1:8000/api/agent/tools
```

注册用户：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"password123","display_name":"张三"}'
```

登录并获取 token：

```bash
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"zhangsan","password":"password123"}'
```

查看当前用户：

```bash
curl http://127.0.0.1:8000/api/auth/me \
  -H 'Authorization: Bearer <access_token>'
```

发送用户消息并启动一次持久化 LangGraph AgentRun：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取并分类这批文件","attachments":[{"document_id":"doc-1"}]}'
```

当前期望行为：

```text
message.role = user
message.user_id = 当前登录用户 id
agent_run.status = COMPLETED
agent_run.intent = CLASSIFY_FILES
agent_run.user_id = 当前登录用户 id
tool_invocations = document-convert, metadata-extract, multi-label-classify, change-report
```

查询 AgentRun：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id>
```

查询 Tool 调用：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id>/tool-invocations
```

非法附件示例：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取文件","attachments":[{"filename":"bad.pdf"}]}'
```

当前期望返回 HTTP `422`，因为附件缺少 `document_id`。

## 7. 当前限制

- 当前接口不接真实大模型，Planner 使用 `DeterministicPlanner`。
- 当前已持久化 user、default workspace、message、AgentRun 和 ToolInvocation，但还没有接文件、ChangeSet 和 OperationPlan 表。
- 当前 Tool handler 是结构化占位实现，不读取真实文件，不写真实文件，不做真实解析、分类或检索。
- 当前已有最小 JWT 鉴权，但没有 refresh token、复杂 RBAC、ACL 或 admin 权限体系。
- 当前前端只有最小注册、登录和 Chat 验证页面，没有文件上传、会话列表、admin 页面或正式视觉设计。

## 8. 维护规则

以下任一内容发生变化时，必须同步更新本文和 `README.md`：

- 启动命令。
- 服务端口或 host。
- Python 环境或依赖安装方式。
- 前端依赖安装方式或启动命令。
- 测试命令。
- 新增或删除可直接调用的接口。
- 当前限制被解除，例如接入数据库、真实文件解析、大模型 Planner 或鉴权。
