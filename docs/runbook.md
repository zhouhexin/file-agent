# File Agent Runbook

本文记录当前项目的本地启动、验证方式和可用接口。后续如果端口、命令、环境依赖、启动顺序或接口能力发生变化，必须同步更新本文和 `README.md`。

## 1. Python 环境

后端使用用户当前已经配置好的 `python3` 环境，不强制创建新虚拟环境，不强制切换到 `uv`、Poetry、Conda 或其他包管理方式。

当前已验证的运行方式是在项目根目录执行命令，避免进入 `apps/api` 后 shell 解析到不同的 Python 解释器。

安装后端依赖：

```bash
python3 -m pip install -r requirements.txt
```

当前根目录 `requirements.txt` 包含后端运行、数据库 migration、测试和 PostgreSQL 连接所需依赖。`apps/api/pyproject.toml` 保留为后端包元数据；本地启动优先使用上面的 `requirements.txt` 安装命令。

首次配置本地环境时复制环境变量模板：

```bash
cp .env.example .env
```

后端启动和 migration 会自动读取项目根目录 `.env`。真实密码只保存在本地 `.env`，不要提交到 Git。

## 2. 运行测试

在项目根目录执行：

```bash
python3 -m pytest
```

当前期望结果：

```text
44 passed
```

如果出现 `urllib3` 或 `LangChainPendingDeprecationWarning`，目前属于环境兼容警告，不影响现有测试结果。

## 3. 数据库

当前后端已经持久化 user、default workspace、message、AgentRun 和 ToolInvocation。

当前本机后端数据库连接：

```text
postgresql+psycopg2://fileagent_user:<password>@212.64.14.158:5432/fileAgent
```

当前已验证该 PostgreSQL 实例可连接，返回数据库 `fileAgent`、用户 `fileagent_user`、PostgreSQL `16.14`。

后端服务数据库必须使用 PostgreSQL。未配置 `DATABASE_URL`，或将 `DATABASE_URL` 配置为 SQLite，服务会直接启动失败。测试代码可以继续使用隔离的内存 SQLite，但运行中的 API 服务不得使用 SQLite。

如需使用项目自带 Docker PostgreSQL + pgvector：

```bash
docker compose up -d postgres
export DATABASE_URL='postgresql+psycopg2://file_agent:file_agent_dev@127.0.0.1:5432/file_agent'
export AUTO_CREATE_TABLES=false
python3 -m alembic -c apps/api/alembic.ini upgrade head
```

对当前 `.env` 指向的 PostgreSQL 执行 migration：

```bash
python3 -m alembic -c apps/api/alembic.ini upgrade head
```

当前 `.env` 中 `AUTO_CREATE_TABLES=false`，应通过 Alembic migration 管理数据库结构。

## 4. 启动后端服务

在项目根目录执行：

```bash
PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

也可以在 `apps/api` 目录执行：

```bash
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

配置层会从当前目录向上查找 `.env`，因此上述两种方式都会读取项目根目录 `.env` 并连接 PostgreSQL。

服务地址：

```text
http://127.0.0.1:8000
```

message、AgentRun 和 ToolInvocation 会写入当前 `DATABASE_URL` 指向的数据库。
上传文件会写入 `FILE_STORAGE_ROOT`，默认是 `./storage/uploads`。
`extract-document-text` 会把解析结果写入 `document_extraction_runs` 和 `document_pages`。

## 4.1 LLM 配置

默认 `LLM_ENABLED=false`，消息入口会继续使用确定性 Planner，便于本地开发和测试稳定运行。

如需在对话阶段启用 LLM 理解用户需求，请在项目根目录 `.env` 中增加：

```text
LLM_ENABLED=true
LLM_PROVIDER=openai_compatible
LLM_API_KEY=<your-api-key>
LLM_BASE_URL=<openai-compatible-base-url>
LLM_CHAT_MODEL=<chat-model-name>
LLM_TIMEOUT_SECONDS=30
```

当前客户端调用 OpenAI-compatible `/chat/completions` 接口，并要求模型返回符合 `UserIntentPlan` 的 JSON 对象。上传阶段的 deterministic ingest 不依赖 LLM；对话阶段启用 LLM 后，会先理解用户需求，再通过白名单 Tool 读取 `document_insights` 或执行后续受控工具。

2026-06-25 已完成真实模型 smoke test：临时启用 `LLM_ENABLED=true` 后，`MiniMax-M3` 可完成“总结我刚才上传的文件”请求，AgentRun 返回 `COMPLETED`，ToolInvocation 为 `read-document-insights`，且 `graph_state_json.user_intent_plan` 已写入。

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
选择文件并上传到 /api/files/upload
上传后自动执行 deterministic ingest：去重、基础分类、关键词提取
展示已上传文件名、大小、处理状态和去重结果
发送前可删除上传文件，并同步删除后端文件
发送一条消息到 AgentRun
发送消息时携带真实 document_id
发送后附件进入对话并锁定，不再允许删除
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
  -d '{"username":"zhangsan","password":"password123","display_name":"张三","email":"zhangsan@example.com"}'
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

上传文件并获取 `document_id`：

```bash
curl -X POST http://127.0.0.1:8000/api/files/upload \
  -H 'Authorization: Bearer <access_token>' \
  -F 'file=@/path/to/file.pdf'
```

删除尚未进入对话的上传文件：

```bash
curl -X DELETE http://127.0.0.1:8000/api/files/<document_id> \
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

如果 `LLM_ENABLED=true` 且用户需求是总结或查看已上传文件基础信息，当前期望行为：

```text
agent_run.intent = SUMMARIZE_DOCUMENTS 或模型识别出的结构化 intent
selected_skills = llm-understanding, document-insight-read
tool_invocations = read-document-insights
graph_state_json.user_intent_plan = LLM 返回的结构化意图
```

如果 `LLM_ENABLED=true` 且用户需求是读取正文、解析 PDF/Excel 内容或 OCR 图片，当前期望行为：

```text
agent_run.intent = EXTRACT_DOCUMENT_TEXT 或模型识别出的结构化 intent
selected_skills = llm-understanding, document-text-extract
tool_invocations = extract-document-text
document_extraction_runs 写入 1 条解析运行
document_pages 写入解析文本
graph_state_json.document_results 写入逐文件解析状态、字符数、分类建议、证据、错误
final_response = 已处理 N 个文件，并逐文件返回解析状态和分类建议。
```

当前对话阶段的基础分类不会写入独立 `document_categories` 表；分类建议只保存在本次 AgentRun 的 `graph_state_json.document_results` 和用户回执中。后续接入正式分类 Skill 后，再补充长期分类表、证据跨度和版本治理。

当前新增文件解析 Tool：

```text
read-original-file：读取当前用户上传原始文件的安全元信息，不返回本地路径或二进制内容
extract-document-text：解析 txt/md/csv/xlsx/docx/pdf/image，并将文本写入 document_pages
```

PDF、Excel、docx 和图片 OCR 依赖：

```text
PyMuPDF
openpyxl
python-docx
Pillow
pytesseract
```

图片 OCR 还需要系统安装 Tesseract OCR；如果缺少依赖或 OCR 引擎不可用，Tool 会返回结构化错误，不会读取任意路径。

当前对话触发解析仍按单文件或当前附件列表中的第一个文件执行；多附件批量 Planner、逐文件部分失败汇总和 map/reduce 后续单独实现。

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

- 当前已接入 OpenAI-compatible LLM 意图理解；默认 `LLM_ENABLED=false` 时仍使用 `DeterministicPlanner`。
- 当前已持久化 user、default workspace、message、AgentRun、ToolInvocation、Document、document_insights、document_extraction_runs 和 document_pages，但还没有接 ChangeSet 和 OperationPlan 表。
- 当前已支持读取当前用户自己的原始文件元信息和解析文本内容；其他多数 Tool handler 仍是结构化占位实现。
- 当前已有最小 JWT 鉴权，但没有 refresh token、复杂 RBAC、ACL 或 admin 权限体系。
- 当前前端已有最小注册、登录、Chat、文件上传和附件删除流程，没有会话列表、admin 页面或正式视觉设计。

## 8. 维护规则

以下任一内容发生变化时，必须同步更新本文和 `README.md`：

- 启动命令。
- 服务端口或 host。
- Python 环境或依赖安装方式。
- 前端依赖安装方式或启动命令。
- 测试命令。
- 新增或删除可直接调用的接口。
- 当前限制被解除，例如接入数据库、真实文件解析、大模型 Planner 或鉴权。
