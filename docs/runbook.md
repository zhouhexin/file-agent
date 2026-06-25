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
12 passed
```

如果出现 `urllib3` 或 `LangChainPendingDeprecationWarning`，目前属于环境兼容警告，不影响现有测试结果。

## 3. 数据库

当前后端已经持久化 message、AgentRun 和 ToolInvocation。

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

## 4. 启动后端服务

在项目根目录执行：

```bash
PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

服务地址：

```text
http://127.0.0.1:8000
```

当前没有前端服务。message、AgentRun 和 ToolInvocation 会写入当前 `DATABASE_URL` 指向的数据库。

## 5. 当前可用接口

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

发送用户消息并启动一次持久化 LangGraph AgentRun：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -d '{"content":"帮我读取并分类这批文件","attachments":[{"document_id":"doc-1"}]}'
```

当前期望行为：

```text
message.role = user
agent_run.status = COMPLETED
agent_run.intent = CLASSIFY_FILES
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
  -d '{"content":"帮我读取文件","attachments":[{"filename":"bad.pdf"}]}'
```

当前期望返回 HTTP `422`，因为附件缺少 `document_id`。

## 6. 当前限制

- 当前接口不接真实大模型，Planner 使用 `DeterministicPlanner`。
- 当前已持久化 message、AgentRun 和 ToolInvocation，但还没有接完整用户、workspace、文件、ChangeSet 和 OperationPlan 表。
- 当前 Tool handler 是结构化占位实现，不读取真实文件，不写真实文件，不做真实解析、分类或检索。
- 当前没有鉴权，`user_id` 使用占位值 `user-memory`。

## 7. 维护规则

以下任一内容发生变化时，必须同步更新本文和 `README.md`：

- 启动命令。
- 服务端口或 host。
- Python 环境或依赖安装方式。
- 测试命令。
- 新增或删除可直接调用的接口。
- 当前限制被解除，例如接入数据库、真实文件解析、大模型 Planner 或鉴权。
