# File Agent

面向学校/学工业务场景的对话式文件工作智能体。

File Agent 不是传统网盘，也不是只会问答的知识库系统。用户通过聊天框上传、读取、OCR、分类、检索、整理和处理文件；系统使用 LangGraph 驱动 Agent Runtime，通过白名单 Tool 执行文件处理，并用 ChangeSet、OperationPlan 和证据链保证每次操作可追溯、可确认、可审计。

## 文档

- `agent.md`：最高级开发规范，后续开发必须优先遵守。
- `docs/automatic-organization-conversational-access-implementation-plan.md`：当前阶段“上传后自动整理、通过对话访问文件”的直接实施与验收依据。
- `docs/conversational-file-agent-development-blueprint.md`：总体开发蓝图。
- `docs/superpowers/plans/2026-06-24-file-agent-mvp-implementation-plan.md`：MVP 开发计划。
- `docs/database-schema.md`：数据库结构设计。
- `docs/api-contract.md`：API 契约。
- `docs/langgraph-runtime-issues.md`：LangGraph Runtime 当前问题与改造路线。
- `docs/langgraph-framework-decision.md`：选择 LangGraph 作为 Agent Runtime 底层编排框架的架构决策。
- `docs/file-rename-llm-validation-implementation-plan.md`：重命名差异风险、LLM 证据校验、降级和验收计划。
- `docs/classification-topic-summary-implementation-plan.md`：分类主题摘要优先的候选召回、原文证据校验、开源选型和Shadow上线方案。
- `docs/managed-original-working-copy-trash-implementation-plan.md`：受管原始目录、工作副本目录、回收站目录、重复上传确认和异步归档导入方案。
- `docs/stage-4-low-resource-two-stage-retrieval-plan.md`：阶段四 CPU-only 两阶段文件检索的边界、数据流与验收依据。
- `docs/skills-catalog.md`：项目内 Agent Skill 清单。
- `docs/neo4j-graph-classification-overall-plan.md`：Neo4j 图谱增强分类整体方案。
- `docs/neo4j-graph-classification-v1-implementation-plan.md`：轻量第一版本实施和验收方案。
- `docs/neo4j-graph-classification-v2-implementation-plan.md`：真实图谱验证、相似文件语义召回和 Shadow 评测方案。
- `docs/runbook.md`：本地启动、验证和当前可用接口。
- `docs/file-agent-manual-smoke-test.md`：整项目真实文件系统手工烟测步骤、通过标准和记录模板。

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

Windows PowerShell 在仓库根目录使用当前 Python 环境运行后端测试：

```powershell
python -m pytest
```

测试套件会隔离 `.env` 中的真实受管目录和外部服务，并为 Windows 自动选择短 pytest 临时根；不需要为了
跑单元测试关闭 Neo4j 容器或修改正式受管目录配置。

当前后端会自动读取项目根目录 `.env`。本机 `.env` 已配置为 PostgreSQL：`212.64.14.158:5432/fileAgent`，真实密码不提交到 Git。
后端服务数据库必须使用 PostgreSQL；如果未配置 `DATABASE_URL`，或配置为 SQLite，服务会直接启动失败。
从项目根目录启动后端时必须设置 `PYTHONPATH=apps/api`，否则 Python 无法找到 `apps/api/app` 包。
如果在项目根目录直接执行 `python -m uvicorn app.main:app ...` 且没有设置 `PYTHONPATH=apps/api`，会报 `ModuleNotFoundError: No module named 'app'`。
上传文件先保存到 `FILE_STORAGE_ROOT=./storage/uploads` 暂存目录并创建异步查重任务。无重复候选时自动异步归档；发现相同或高度相似文件时，聊天页逐文件要求用户选择“继续上传”“使用已有文件”或“取消上传”。
文件生命周期固定使用三层名词：`受管原始目录`保存不可变原始文件，`工作副本目录`承载 Agent 的增删改查，`回收站目录`保存可恢复的工作副本删除结果。重命名和移动只改变工作副本路径，不新增 `DocumentVersion`；原始文件始终不变。普通用户可以在 `/chat` 通过自然语言处理同名冲突、移入回收站和恢复文件，所有物理动作都必须先展示并确认 OperationPlan。
所有用户共用唯一物理工作目录：受管资料和上传归档每个文件只导入一份，固定保存于 `shared/<root_key>`，不再按用户 default workspace 复制。用户 default workspace 仍只保存会话、上传来源和审计；普通用户的可见性校验不会因物理共享而放宽。共享目录上的改名、移动、回收站和恢复计划会明确提示其影响范围，仍须由发起用户确认。
服务端结构化日志默认保存到 `LOG_DIR=./logs`，按天生成 `file-agent-YYYY-MM-DD.log`，启动时会删除超过 `LOG_RETENTION_DAYS=7` 天的日志。
旧版 `.xls` 不再通过 `xlrd` 直读：系统必须先用 LibreOffice/`soffice` 在隔离临时目录和独立 profile 中转换为临时 `.xlsx`，校验输出后再由 `openpyxl` 解析全部工作表。转换器缺失或输出无效时返回结构化失败，原 `.xls` 字节不变，临时 `.xlsx` 不登记为上传原件。
上传采用分块流式写入，`UPLOAD_MAX_FILE_SIZE_MB` 是可按部署容量调整的资源保护上限，默认 1024 MB，并非固定业务限制。当前阶段只执行扩展名、基础 MIME、宏和加密风险检查，不实现、也不宣称已执行病毒扫描。
PDF、DOCX 默认启用本地 Docling 结构化解析，并把文档元素和位置写入 `document_elements`；Docling 不可用时自动回退现有解析器，扫描件仍由现有 OCR 链路处理。
文件重命名统一生成 `RENAME_WORKING_COPIES` OperationPlan，确认后由工作副本执行器执行；旧的受管原始文件 Native/F2 执行通道和上传暂存重命名通道不再对 Agent 开放。
上传附件通过查重后由独立 worker 归档到受管原始目录；导入 worker 在隐藏临时文件上完成解析、双摘要、分类和首次命名，再把文件原子提交到最终工作副本路径。后台双摘要默认使用 CPU-only Jieba + LexRank 抽取原文关键句，即使全局 LLM 已启用也不会自动发送上传正文；只有用户明确要求总结或讲解时才使用独立的聊天摘要 LLM Provider。低置信度命名保留原上传文件名并请求确认；目标名称冲突时先询问是否同时保留，不会自动增加版本后缀或覆盖文件。普通用户只看到整理后的文件名、分类和需要本人决定的事项，不展示内部状态、Skill 或 Tool。对话找文件默认使用 CPU-only 两阶段检索：先按最终文件名、分类、元数据和普通文档摘要召回少量当前工作副本，必要时以原文 Chunk 词法索引补召回，再只在候选版本内定位证据。该流程不调用 embedding、GPU、LLM 或 Graph；用户只看到文件卡、分类、概览、命中原因和位置，并能打开有权限的结果文件。精确问答仍必须回到原文取证。后续重命名、移动和删除计划必须以 `working_copy_id` 为对象，不能再修改受管原始目录。
每个成功解析的工作副本内容版本会在发布前幂等建立 Chunk/Evidence。当前无 GPU 部署使用 Jieba + PostgreSQL `simple` FTS/GIN + `pg_trgm` 的 CPU 词法索引；`embedding vector(1536)` 只保留空扩展槽，默认 `EMBEDDING_ENABLED=false`，不会下载向量模型或要求应用服务器安装 GPU。后续可接独立 GPU provider 异步回填，不改变已有 Chunk、Evidence 和引用 ID。
默认不启用真实 LLM 调用；如需让对话阶段使用大模型理解用户需求，请在 `.env` 中配置 `LLM_ENABLED=true`、`LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_CHAT_MODEL`。当前 LLM 客户端使用 OpenAI-compatible Chat Completions 接口。
后台普通摘要和分类主题摘要分别由 `DOCUMENT_SUMMARY_PROVIDER=extractive`、`CLASSIFICATION_SUMMARY_PROVIDER=extractive` 控制；这两个默认值不需要 GPU 或模型服务。`CHAT_DOCUMENT_SUMMARY_PROVIDER=llm` 只在用户明确提出总结类任务且全局 LLM 已启用时生效。确需让后台摘要调用模型时，必须把对应 Provider 显式改为 `llm`。
分类判定默认仍为 `LLM_CLASSIFICATION_MODE=rule_only`。如需让 LLM 在候选分类内做语义判定，可设置 `LLM_CLASSIFICATION_MODE=hybrid`；如需允许 LLM 自由提出新分类路径，还必须显式设置 `LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true`，该类结果只会以 `NEEDS_REVIEW` 保存，不会自动写入正式分类目录。
Neo4j 图谱增强分类默认关闭。第二版本支持目录角色 Profile、完整正文本地 Embedding、固定 `VectorCypherRetriever`、`off/shadow/enabled`、投影运行审计和分类反馈样本；无标注阶段只允许小范围展示建议，连接失败会自动回退现有分类。具体步骤见 `docs/runbook.md`。

消息接口需要先注册、登录并携带 `Authorization: Bearer <access_token>`。示例见 `docs/runbook.md`。

除 API 外，三层文件生命周期至少需要独立启动 worker 和 scheduler；需要近实时同步时再启动 watcher：

```bash
# 可在不同进程中分别设置 FILESYSTEM_WORKER_QUEUES。SCAN 每完成一批就提交
# IMPORT 任务，因此扫描 worker 与导入 worker 同时运行时，工作副本无需等待全量扫描结束。
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=IMPORT,FILE_OPERATION \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.file_lifecycle.scheduler
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.file_lifecycle.watcher
```

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
-> 系统解析、切分、提取证据并建立 CPU 词法索引；embedding 默认关闭、后续可扩展
-> 系统生成多标签分类、ChangeSet 和逐文件回执
-> 用户可进行证据问答、查看引用、提交反馈
-> 高风险操作先生成 OperationPlan，确认后才执行
-> admin/ops 处理反馈、重处理文件并维护模型配置
```

第一版不做完整 Neo4j 图谱、Graphiti 记忆、自动 Skill 演化或外部多智能体平台，但从第一版开始必须使用 LangGraph，并保留 AgentRun、Tool 调用、ChangeSet、OperationPlan 和审计边界。
