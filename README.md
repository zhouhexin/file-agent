# File Agent

面向学校/学工业务场景的对话式文件工作智能体。

File Agent 不是传统网盘，也不是只会问答的知识库系统。用户通过聊天框上传、读取、OCR、分类、检索、整理和处理文件；系统使用 LangGraph 驱动 Agent Runtime，通过白名单 Tool 执行文件处理，并用 ChangeSet、OperationPlan 和证据链保证每次操作可追溯、可确认、可审计。

## 文档

- `agent.md`：最高级开发规范，后续开发必须优先遵守。
- `docs/conversational-file-agent-development-blueprint.md`：总体开发蓝图。
- `docs/superpowers/plans/2026-06-24-file-agent-mvp-implementation-plan.md`：MVP 开发计划。
- `docs/database-schema.md`：数据库结构设计。
- `docs/api-contract.md`：API 契约。
- `docs/skills-catalog.md`：项目内 Agent Skill 清单。
- `docs/runbook.md`：本地启动、验证和当前可用接口。

## 本地运行

后端使用当前已配置好的 `python3` 环境，不强制创建新虚拟环境。完整运行说明见 `docs/runbook.md`。

```bash
python3 -m pytest
python3 -m alembic -c apps/api/alembic.ini upgrade head
PYTHONPATH=apps/api python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

当前服务地址：

```text
http://127.0.0.1:8000
```

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
