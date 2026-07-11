# File Agent

面向学校/学工业务场景的对话式文件工作智能体。

File Agent 不是传统网盘，也不是只会问答的知识库系统。用户通过聊天框上传、读取、OCR、分类、检索、整理和处理文件；系统使用 LangGraph 驱动 Agent Runtime，通过白名单 Tool 执行文件处理，并用 ChangeSet、OperationPlan 和证据链保证每次操作可追溯、可确认、可审计。

## 文档

- `agent.md`：最高级开发规范，后续开发必须优先遵守。
- `docs/conversational-file-agent-development-blueprint.md`：总体开发蓝图。
- `docs/superpowers/plans/2026-06-24-file-agent-mvp-implementation-plan.md`：MVP 开发计划。
- `docs/database-schema.md`：数据库结构设计。
- `docs/api-contract.md`：API 契约。
- `docs/langgraph-runtime-issues.md`：LangGraph Runtime 当前问题与改造路线。
- `docs/skills-catalog.md`：项目内 Agent Skill 清单。
- `docs/runbook.md`：本地启动、验证和当前可用接口。

## 本地运行

后端使用当前已配置好的 `/opt/homebrew/anaconda3/envs/py311/bin/python` 环境，不强制创建新虚拟环境。完整运行说明见 `docs/runbook.md`。

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pip install -r requirements.txt
cp .env.example .env
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
cd apps/web && npm install && npm run dev
```

当前后端会自动读取项目根目录 `.env`。本机 `.env` 已配置为 PostgreSQL：`212.64.14.158:5432/fileAgent`，真实密码不提交到 Git。
后端服务数据库必须使用 PostgreSQL；如果未配置 `DATABASE_URL`，或配置为 SQLite，服务会直接启动失败。
从项目根目录启动后端时必须设置 `PYTHONPATH=apps/api`，否则 Python 无法找到 `apps/api/app` 包。
如果在项目根目录直接执行 `python -m uvicorn app.main:app ...` 且没有设置 `PYTHONPATH=apps/api`，会报 `ModuleNotFoundError: No module named 'app'`。
上传文件默认保存到 `FILE_STORAGE_ROOT=./storage/uploads`。
服务端结构化日志默认保存到 `LOG_DIR=./logs`，按天生成 `file-agent-YYYY-MM-DD.log`，启动时会删除超过 `LOG_RETENTION_DAYS=7` 天的日志。
旧版 `.xls` 解析依赖本机 LibreOffice/`soffice` 做临时 `.xlsx` 转换；未安装时系统会返回结构化失败，不覆盖原件。
默认不启用真实 LLM 调用；如需让对话阶段使用大模型理解用户需求，请在 `.env` 中配置 `LLM_ENABLED=true`、`LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_CHAT_MODEL`。当前 LLM 客户端使用 OpenAI-compatible Chat Completions 接口。
分类判定默认仍为 `LLM_CLASSIFICATION_MODE=rule_only`。如需让 LLM 在候选分类内做语义判定，可设置 `LLM_CLASSIFICATION_MODE=hybrid`；如需允许 LLM 自由提出新分类路径，还必须显式设置 `LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true`，该类结果只会以 `NEEDS_REVIEW` 保存，不会自动写入正式分类目录。

消息接口需要先注册、登录并携带 `Authorization: Bearer <access_token>`。示例见 `docs/runbook.md`。

当前服务地址：

```text
后端：http://127.0.0.1:8000
前端：http://127.0.0.1:5173
```

前端开发端口固定为 `5173`，如果端口被占用，请先停止占用进程，或同步调整 Vite 端口、`VITE_API_BASE_URL` 和后端 CORS 白名单。

## MVP 目标

```text
用户登录并进入 /chat
-> 用户发送文件工作指令并上传文件
-> LangGraph 创建 AgentRun
-> Agent 选择 Skill 并通过白名单 Tool 执行
-> 系统保存原件、版本和派生件
-> 系统解析、切分、提取证据、生成 embedding
-> 系统生成多标签分类、ChangeSet 和逐文件回执
-> 用户可进行证据问答、查看引用、提交反馈
-> 高风险操作先生成 OperationPlan，确认后才执行
-> admin/ops 处理反馈、重处理文件并维护模型配置
```

第一版不做完整 Neo4j 图谱、Graphiti 记忆、自动 Skill 演化或外部多智能体平台，但从第一版开始必须使用 LangGraph，并保留 AgentRun、Tool 调用、ChangeSet、OperationPlan 和审计边界。
