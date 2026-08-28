# File Agent Runbook

本文记录当前项目的本地启动、验证方式和可用接口。后续如果端口、命令、环境依赖、启动顺序或接口能力发生变化，必须同步更新本文和 `README.md`。

整项目发布前的真实文件系统烟测、测试数据矩阵和逐项通过标准见
`docs/file-agent-manual-smoke-test.md`。本文只维护启动与运行方式，不能替代烟测手册。

## 1. Python 环境

后端使用用户当前已经配置好的 `/opt/homebrew/anaconda3/envs/py311/bin/python` 环境，不强制创建新虚拟环境，不强制切换到 `uv`、Poetry 或其他包管理方式。

当前已验证的运行方式是在项目根目录执行命令，避免进入 `apps/api` 后 shell 解析到不同的 Python 解释器。

安装后端依赖：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pip install -r requirements.txt
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
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest
```

Windows PowerShell 使用当前已配置的 Python 环境：

```powershell
python -m pytest
```

测试必须从仓库根目录和 `apps/api` 两种位置得到相同结果。Windows 下 pytest 会自动使用短临时根目录；
测试进程同时隔离项目 `.env` 中的真实受管目录和外部服务配置，避免普通单元测试扫描用户文件或连接外部服务。

当前期望结果：

```text
全部自动化测试通过；只保留有明确外部环境原因的 skipped 项
```

当前跳过项是需要真实外部执行器或独立环境的既有集成测试。Paddle、PyMuPDF 等依赖可能输出弃用或
环境兼容警告；只要没有失败项，不影响当前自动化验收结论。Windows 无符号链接权限时允许额外跳过
`test_path_policy_rejects_symlink_escape`，但必须显示明确的权限原因。

前端展示逻辑和正式构建分别执行：

```bash
cd apps/web
npm test
npm run build
```

`npm test` 使用 Node 原生测试和 Vite SSR 验证统一文件回执的阶段边界与搜索结果逻辑身份，不访问后端或外部服务。

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
docker compose up -d postgres neo4j
export DATABASE_URL='postgresql+psycopg2://file_agent:file_agent_dev@127.0.0.1:5432/file_agent'
export AUTO_CREATE_TABLES=false
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
```

本地 Neo4j 使用 `.env` 中的 `NEO4J_PASSWORD`，HTTP 管理页为 `http://127.0.0.1:7474`，Bolt 地址为
`bolt://127.0.0.1:7687`。启动后先执行 `docker compose ps`，确认 `file-agent-neo4j` 为 `healthy`；
Neo4j 容器只保存可重建图投影，不能替代 PostgreSQL 事实源。示例 compose 只绑定 `127.0.0.1`，且
`.env.example` 不提供默认 Neo4j 密码；复制配置后必须先填写强密码，不能把数据库端口直接暴露到公网或
局域网。

对当前 `.env` 指向的 PostgreSQL 执行 migration：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
```

Docker 生产环境中必须从容器内的仓库根目录 `/app` 显式指定 Alembic 配置文件；直接执行 `alembic upgrade head` 会因为找不到 `script_location` 失败：

```bash
docker compose -f deploy/docker-compose.production.yml exec -w /app api python -m alembic -c apps/api/alembic.ini upgrade head
```

如果已经在 `deploy/` 目录内执行 compose 命令，则使用：

```bash
docker compose exec -w /app api python -m alembic -c apps/api/alembic.ini upgrade head
```

当前 `.env` 中 `AUTO_CREATE_TABLES=false`，应通过 Alembic migration 管理数据库结构。

## 4. 启动后端服务

在项目根目录执行：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

也可以在 `apps/api` 目录执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

配置层会从当前目录向上查找 `.env`，因此上述两种方式都会读取项目根目录 `.env` 并连接 PostgreSQL。

从项目根目录启动后端时必须设置 `PYTHONPATH=apps/api`，否则 Python 只能在根目录查找 `app` 包。
如果在项目根目录执行 `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000` 且没有设置 `PYTHONPATH=apps/api`，会报 `ModuleNotFoundError: No module named 'app'`。

服务地址：

```text
http://127.0.0.1:8000
```

message、AgentRun 和 ToolInvocation 会写入当前 `DATABASE_URL` 指向的数据库。
上传文件会写入 `FILE_STORAGE_ROOT`，默认是 `./storage/uploads`。
`extract-document-text` 会把解析结果写入 `document_extraction_runs` 和 `document_pages`。

成功解析的活动工作副本会继续建立 `document_index_runs`、`document_chunks` 和 `evidence_spans`。
当前阶段不需要 GPU，默认配置为：

```dotenv
RETRIEVAL_MODE=lexical
CHINESE_TOKENIZER=jieba
DOCUMENT_CHUNK_MAX_CHARS=1200
DOCUMENT_CHUNK_OVERLAP_CHARS=120
DOCUMENT_INDEX_MAX_CHARS=50000000
DOCUMENT_INDEX_MAX_CHUNKS=50000
EMBEDDING_ENABLED=false
EMBEDDING_PROVIDER=disabled
TWO_STAGE_RETRIEVAL_ENABLED=true
RETRIEVAL_DOCUMENT_CANDIDATE_LIMIT=30
RETRIEVAL_DOCUMENT_DETAIL_LIMIT=12
RETRIEVAL_CHUNK_LIMIT_PER_DOCUMENT=3
RETRIEVAL_CHUNK_GLOBAL_LIMIT=24
RETRIEVAL_STATEMENT_TIMEOUT_MS=2000
```

`DOCUMENT_INDEX_MAX_CHARS` 和 `DOCUMENT_INDEX_MAX_CHUNKS` 是可调整的 worker 资源预算，不是上传业务
限制；超出预算时原件和工作副本保持不变，检索状态进入待处理，后续可由分批索引 worker 接管。
Jieba 在应用层生成中文词项，PostgreSQL 使用 `simple` FTS/GIN 和 `pg_trgm`；不要在 File Agent API
进程安装或加载 embedding 模型。后续如接独立 GPU 推理服务，先实现受控 provider 和异步回填任务，
再把 `RETRIEVAL_MODE` 调整为 `hybrid`。关闭或回填失败时必须继续使用词法索引。

阶段四的 `document_search_profiles` 是可重建的工作副本级瘦检索投影。工作副本固定属于唯一
`SYSTEM_SHARED` 系统工作区并使用 `shared/<root_key>` 存储前缀；用户 default workspace 只保存会话和
上传来源，不能导致同一原始文件被重复复制。执行 migration 后，上传导入、
重命名/移动、恢复、摘要完成和分类建议写入会在同一事务更新投影；旧数据可由
`DocumentSearchProfileService.backfill_profiles()` 或 `reconcile_profiles()` 幂等补齐。生产迁移完成后，
用以下两类对话烟测确认：上传后搜索文件名或分类主题；再搜索仅出现于原文中的短语，确认系统能返回
可打开的文件卡和页码/Sheet 位置。检索可以返回唯一共享工作目录中的 `ACTIVE` 文件，但不可出现其他
用户的个人会话或上传来源，也不可出现回收站或旧版本文件。

阶段四新增迁移为 `20260724_0001_create_document_search_profiles` 和
`20260724_0002_finalize_document_search_profiles`。部署前先确认只有一个 head：

```bash
python -m alembic -c apps/api/alembic.ini heads
```

在隔离的 PostgreSQL 开发库上，可验证升级、降级和再次升级；禁止在含有需要保留业务数据的库上直接
执行 downgrade：

```bash
python -m alembic -c apps/api/alembic.ini upgrade head
python -m alembic -c apps/api/alembic.ini downgrade 20260723_0001
python -m alembic -c apps/api/alembic.ini upgrade head
```

上传接口使用临时文件和分块流式写入，不会把整份文件一次性读入内存。以下参数只用于部署资源保护，
可以根据磁盘、并发和 worker 容量调整，不应被解释为学校业务文件的固定大小限制：

```dotenv
UPLOAD_MAX_FILE_SIZE_MB=1024
UPLOAD_CHUNK_SIZE_BYTES=1048576
UPLOAD_ALLOWED_EXTENSIONS=pdf,doc,docx,xls,xlsx,xlsm,txt,md,csv,png,jpg,jpeg,tif,tiff,bmp,webp
```

当前阶段只检查受支持扩展名、基础 MIME 一致性、Office 宏标记和文件加密状态。没有接入病毒扫描
引擎，回执和日志不得把上述检查表述为“已通过病毒扫描”。

服务端结构化日志会写入 `LOG_DIR`，默认 `./logs`。日志文件按天生成：

```text
logs/file-agent-YYYY-MM-DD.log
```

每行是一条 JSON，包含 `request_id`、`agent_run_id`、`user_id`、`conversation_id`、`tool_name`、`document_id`、`status`、`duration_ms` 和 `error_code` 等字段。启动时会按 `LOG_RETENTION_DAYS` 清理超过保留期的旧日志，默认保留 7 天。

文件检索会额外记录 `retrieval.route.selected`、`retrieval.query.parsed`、
`retrieval.scope.resolved`、`retrieval.stage1.*`、`retrieval.chunk_fallback.*`、
`retrieval.stage2.*`、`retrieval.evidence.*` 和 `retrieval.search.completed`。这些事件只保存
查询长度、不可逆短指纹、候选数、Chunk 数、证据数和结果数，不保存用户查询原文或文件正文。
历史工作副本自动补建索引时记录 `working_copy.search_repair.*` 和 `document.index.*`，用于区分
扫描未入队、解析失败、正文索引失败和文件级投影失败。

Windows CMD 从仓库根启动项目后，可以直接检查：

```cmd
findstr /I /C:"retrieval." /C:"working_copy.search_repair" logs\file-agent-*.log
```

如果显式配置了绝对 `LOG_DIR`，日志以该目录为准；相对 `LOG_DIR` 始终相对于启动对应进程时的当前
目录，因此推荐 API 和 worker 都从仓库根启动。

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
LLM_CLASSIFICATION_MODE=rule_only
LLM_CLASSIFICATION_ALLOW_FREE_PATHS=false
DOCUMENT_SUMMARY_PROVIDER=extractive
CLASSIFICATION_SUMMARY_PROVIDER=extractive
CHAT_DOCUMENT_SUMMARY_PROVIDER=llm
# 最终回执 LLM 只使用后端已验证的摘要，设为 disabled 时仍展示完整确定性回执。
AGENT_RECEIPT_SUMMARY_PROVIDER=llm
EVIDENCE_ANSWER_ENABLED=true
EVIDENCE_ANSWER_PROVIDER=llm
EVIDENCE_ANSWER_PROMPT_VERSION=evidence-answer-v1
EVIDENCE_ANSWER_SCHEMA_VERSION=evidence-answer-schema-v1
EVIDENCE_ANSWER_MAX_DOCUMENTS=12
EVIDENCE_ANSWER_MAX_ITEMS=48
EVIDENCE_ANSWER_MAX_INPUT_CHARS=120000
EVIDENCE_ANSWER_MAX_CALLS=3
EVIDENCE_ANSWER_REPAIR_CALLS=1
EVIDENCE_ANSWER_CACHE_ENABLED=true
ADAPTIVE_PLANNER_MODE=shadow
ADAPTIVE_PLANNER_ROLLOUT_PERCENT=0
ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT=100
ADAPTIVE_PLANNER_SCHEMA_VERSION=planner-decision-v1
OCR_ENABLED=true
OCR_PADDLE_MODEL_SOURCE=BOS
OCR_LLM_ENABLED=false
OCR_LLM_FALLBACK_QUALITY_THRESHOLD=0.68
DOCLING_ENABLED=true
DOCLING_FORMATS=pdf,docx
DOCLING_OCR_ENABLED=false
```

当前客户端调用 OpenAI-compatible `/chat/completions` 接口，并要求模型返回符合 `UserIntentPlan` 的 JSON 对象。上传阶段的 deterministic ingest 不依赖 LLM；对话阶段启用 LLM 后，会先理解用户需求，再通过白名单 Tool 读取 `document_insights` 或执行后续受控工具。

Adaptive Planner 使用独立的 `PlannerDecision` 契约，并且只能引用请求级 `CatalogSnapshot` 中存在、已启用且
被 SkillManifest 允许的 Tool。三种运行模式为：

```text
legacy：只运行现有 Planner。
shadow：现有 Planner 产生用户可见结果，Adaptive Planner 只生成并校验决策，不调用 Tool。
enabled：按用户稳定哈希和 ADAPTIVE_PLANNER_ROLLOUT_PERCENT 灰度启用 Adaptive Planner；
         未命中用户继续进入 Shadow，Adaptive 失败时先回退 Legacy，再回退确定性 Planner。
```

默认保持 `shadow` 且灰度比例为 0。只有生产 Shadow 指标满足
`docs/adaptive-planner-execution-loop-implementation-plan.md` 的安全门槛后，才允许逐步调整为
5%、25%、50%、100%。每次运行最多规划 3 轮、调用 5 次 Tool；同名 Tool 加规范化输入的重复调用会在
执行前拒绝，高风险 Tool 在未确认时会暂停当前步骤，不继续后续步骤。

部署本版本前必须执行迁移 `20260730_0001_add_adaptive_planner_catalog`。该迁移新增
`capability_suggestions`、`planner_shadow_comparisons`，并给 `agent_runs` 增加 Planner 模式、
schema 版本与 Catalog 指纹。服务启动时会交叉校验 `skills/*/manifest.json` 与代码白名单；存在未知
Tool 或缺少 `SKILL.md` 时会关闭式启动失败。

能力缺口建议的管理接口为：

```text
GET  /api/admin/capability-suggestions
GET  /api/admin/capability-suggestions/{suggestion_id}
POST /api/admin/capability-suggestions/{suggestion_id}/review
GET  /api/admin/planner-shadow/metrics
```

ops/admin 可以查看并评审；只有 admin 可以标记为接受或已实现。即使标记为接受，也不会自动生成代码、
注册 Tool、修改 SkillManifest 或扩大权限。前端入口为 `/admin/capability-suggestions`。Shadow 指标
接口只返回当前最新 Catalog/schema 批次的指纹、schema、决策、范围、风险与确认一致率以及错误计数；
Shadow 生成失败和校验失败也会进入分母。接口不返回 Prompt、正文或 Tool 输入，也不能通过该接口切换
灰度。

任务诊断的管理入口为 `/admin/agent-runs`，仅允许 ops/admin 使用。页面把 AgentRun、
ToolInvocation、FilesystemJob 和同一份 JSONL 结构化日志合并为中文时间线，显示处理阶段、状态、原因
和建议操作，不展示文件正文、绝对路径、密钥或完整 Prompt。接口为：

```text
GET /api/admin/agent-runs
GET /api/admin/agent-runs/{agent_run_id}/diagnostics
```

当自然语言任务启用 Adaptive 灰度时，Catalog 内的成熟检索、文件读取、分类、重命名计划和工作副本
删除/恢复/移动计划 Tool 执行后，都会把统一脱敏观察交回 Planner。观察只包含状态、数量、后端授权的
文件范围、证据/分类数量及确认或异步状态，不包含正文、文件名、路径和内部 ID。Planner 在最多 3 轮
规划、5 次 Tool 调用预算内决定结束、继续/切换 Tool 或澄清；相同 Tool 输入不会重复执行。生成
OperationPlan 或异步任务后，后端停止本轮副作用循环；`confirmed-file-action` 不在 Catalog 中，只能由
用户确认后的后端 API 调用。普通用户仍只看到现有任务回执、证据和计划卡片，不展示 Planner、Skill、Tool
或内部任务 ID。当前消息包含完整文件名时，该文件名直接构成硬范围，不依赖上一轮搜索上下文。
观察中的允许决策由后端强制校验，不能只依赖模型遵守 Prompt。只读 Tool 失败可在预算内换 Tool 或请求
澄清；有副作用 Tool 失败以及已生成 OperationPlan 的场景只允许结束或澄清。模型在重规划阶段异常时，
系统保留已有验证结果并结束，不会通过 Legacy Planner 重新执行原始副作用请求。

`AGENT_RECEIPT_SUMMARY_PROVIDER=llm` 会在最终回执节点增加一段自然语言说明，但该调用仅收到后端验证的
结果类型和状态。文件名、数量、相对路径、页码、工作表、单元格、OperationPlan 明细仍由确定性 Tool
结果与现有聊天卡片展示；证据回答不会被该通用回执模型二次改写。设为 `disabled` 时只保留确定性回执，
无需执行数据库迁移。

上传导入和分类阶段的持久化双摘要默认使用 `extractive` Provider：本地 Jieba 分词后以有候选上限的
LexRank 选择可定位原文句子，不下载模型、不要求 GPU，也不会因为 `LLM_ENABLED=true` 自动外发正文。
阶段五开始，用户明确提出“总结、讲解、询问正文事实”等任务时，优先通过
`EVIDENCE_ANSWER_PROVIDER=llm` 调用带引用校验的证据回答服务；设置为 `disabled` 时只返回确定性
证据摘录。`CHAT_DOCUMENT_SUMMARY_PROVIDER` 只保留旧兼容路径。只有部署已经获得文件正文模型处理
授权时，才可以把
`DOCUMENT_SUMMARY_PROVIDER` 或 `CLASSIFICATION_SUMMARY_PROVIDER` 显式改为 `llm`。旧配置值
`openai_compatible` 会兼容映射为 `llm`，但新配置统一使用 `llm`。

阶段五将 Chunk 索引升级为 `document-chunk-index-v2`，Evidence quote 覆盖完整 Chunk。升级代码和
数据库迁移后必须同时启动 scheduler、`RECONCILE,SCAN` worker、`SOURCE_ANALYSIS,ANALYSIS` worker 与
包含 `MATERIALIZE,IMPORT` 队列的生命周期 worker；历史 v1 索引会被
识别为待修复并重建。重建完成前全文总结明确返回 `INDEX_PENDING`，不会把旧 500 字符证据冒充完整
总结。

OCR 第一阶段使用本地 PaddleOCR 作为默认 Provider。图片文件会直接进入 OCR；PDF 原生文本为空时会先渲染页面，再进入 OCR，并把识别文本写入 `document_pages.text_content`。`OCR_PADDLE_MODEL_SOURCE` 默认是 `BOS`，服务会在加载 PaddleOCR 前设置 `PADDLE_PDX_MODEL_SOURCE=BOS`，让 PaddleOCR 使用百度 BOS 模型下载源。如需启用 LLM OCR 兜底，必须显式设置 `OCR_LLM_ENABLED=true` 且 `LLM_ENABLED=true`；系统会在本地 OCR 质量低于 `OCR_LLM_FALLBACK_QUALITY_THRESHOLD` 时按页调用多模态模型，不默认外发整份文件。

PDF、DOCX 默认使用 Docling 进行本地结构化解析，并将标题、章节、正文、页眉页脚和位置元素写入 `document_elements`。`DOCLING_OCR_ENABLED=false` 时，扫描件继续使用上述 PaddleOCR/LLM OCR 链路；Docling 缺失、转换失败或正文为空时自动回退现有 PyMuPDF/python-docx 解析器。首次启用或升级 Docling 后，解析器配置指纹会变化，相关文件下一次读取时会生成新的解析运行，旧解析结果继续保留用于历史审计。

升级到结构化解析版本后，在仓库根目录执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m alembic -c apps/api/alembic.ini upgrade head
```

分类 LLM 判定由 `LLM_CLASSIFICATION_MODE` 单独控制：

```text
rule_only：默认值，只使用 taxonomy 候选召回和规则建议。
hybrid：LLM 只能从候选 category_id 中选择 0~3 个分类。
review_only：仅当规则结果为“其他”、低置信度或需要复核时调用 LLM。
```

如果需要允许 LLM 自由生成分类路径，必须同时设置：

```text
LLM_CLASSIFICATION_MODE=hybrid
LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true
```

自由生成的分类路径不会自动进入正式 taxonomy，也不会写入正式 `document_categories`。系统会把它保存为 `source=llm_free_path`、`status=NEEDS_REVIEW` 的建议，等待人工确认、纠正或后续维护 taxonomy v2。

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
展示普通用户任务状态、逐文件整理回执和必要确认，不展示 AgentRun、intent、Skill 或 Tool 内部载荷
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
{
  "status": "ok",
  "knowledge_graph": {
    "status": "disabled",
    "reason": "GRAPH_DISABLED",
    "graphrag_package": "not_installed"
  }
}
```

查看 MVP Tool 白名单：

```bash
curl http://127.0.0.1:8000/api/agent/tools \
  -H 'Authorization: Bearer <ops-or-admin-access-token>'
```

Tool 白名单和 AgentRun/ToolInvocation 详情属于内部审计信息，仅 `ops`、`admin` 可访问；普通用户通过
消息接口的 `task_result` 查看整理结果。

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

读取附件原始内容，用于前端点击附件后预览或下载：

```bash
curl -X GET http://127.0.0.1:8000/api/files/<document_id>/content \
  -H 'Authorization: Bearer <access_token>' \
  --output downloaded-file
```

发送用户消息并启动一次持久化 LangGraph AgentRun：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取并分类这批文件","attachments":[{"document_id":"doc-1"}]}'
```

读取会话详情，用于前端刷新后恢复历史消息、附件和 AgentRun 回复：

```bash
curl -X GET http://127.0.0.1:8000/api/conversations/conv-1 \
  -H 'Authorization: Bearer <access_token>'
```

当前期望行为：

```text
message.role = user
message.user_id = 当前登录用户 id
agent_run.status = COMPLETED
agent_run.intent = CLASSIFY_FILES
agent_run.user_id = 当前登录用户 id
tool_invocations = 每个附件各 1 次 extract-document-text
分类依据 = document_pages 中的完整正文，不使用 300 字 text_preview
```

如果 `LLM_ENABLED=true` 且用户需求是总结或查看已上传文件基础信息，当前期望行为：

```text
agent_run.intent = SUMMARIZE_DOCUMENTS 或模型识别出的结构化 intent
selected_skills = llm-understanding, document-insight-read
tool_invocations = read-document-insights
graph_state_json.user_intent_plan = LLM 返回的结构化意图
```

如果用户需求是读取正文、解析 PDF/Excel 内容或 OCR 图片，当前期望行为：

```text
LLM_ENABLED=true 时：agent_run.intent = EXTRACT_DOCUMENT_TEXT 或模型识别出的结构化 intent
deterministic 模式下用户明确说“读取/解析/正文/内容/OCR”时：agent_run.intent = EXTRACT_DOCUMENT_TEXT
“读取并分类 / 解析并归类”等组合意图优先按正文读取处理，分类作为 document_results 的输出要求
LLM 模式 selected_skills = llm-understanding, document-text-extract
deterministic 模式 selected_skills = chat-intake, document-text-extract, document-classification, change-report
tool_invocations = 每个附件各 1 次 extract-document-text
tool_invocations.status = Tool 输出 ok=false 或 status=FAILED 时记为 FAILED
document_extraction_runs 默认复用同一文件最近一次成功解析结果；用户明确说“重新解析 / 重新读取 / 重新处理 / 重跑”时才新建解析运行
document_pages 只在首次成功解析或强制重处理时写入；默认复用不会重复写页
graph_state_json.document_results 写入逐文件解析状态、字符数、分类建议、evidence_items、错误
document_classification_runs / document_category_suggestions 写入本次 AgentRun 的结构化分类建议和 evidence_items
change_sets / change_items 写入本次处理明细和 evidence_items；复用时记录 TEXT_REUSED、DOCUMENT_PAGES_REUSED、CATEGORY_SUGGESTION_REUSED
final_response = 已处理 N 个文件，并逐文件返回解析状态、多个分类建议、置信度、页码/Sheet 和原文 quote。
```

当前运行时分类统一使用项目内生成后的 JSON 分类配置：

```text
apps/api/app/modules/classification/taxonomies/unified_school_file_classification.json
```

该配置由预置 `school_file_classification.json` 与受管目录清洗快照共同生成。当前 taxonomy version 为 `2026-08-v3`，已依次合并 2026-07-15 记录快照、2026-07-18 挂载卷实时快照，并为全部 58 个候选分类补齐安全 `organization_path`。`DocumentClassificationService` 对上传文件和受管文件始终加载这一套分类，不再因为存在 `PATH_AS_CATEGORY` 根而切换为 `managed_global_categories`。目录中的 `CATEGORY`、`DEPARTMENT` 只增强已有稳定分类 ID 的别名和正向信号；年份、临时目录和集合目录不会成为业务分类。分类 matcher 会基于分类名、别名、正向信号、负向信号和一级域上下文生成 Top N 候选；`match_document_text` 仍作为 rule-only 兼容入口，最多保留前 5 个分类建议。对话链路通过 `DocumentClassificationService` 从 `document_pages.text_content` 读取完整正文，Graph 不直接读取全文或调用底层 matcher。分类建议会同时保存在本次 AgentRun 的 `graph_state_json.document_results`、用户回执、`document_classification_runs` 和 `document_category_suggestions` 中。

如需从 Excel 重新生成分类 JSON，可执行：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python scripts/convert_taxonomy_excel.py \
  --file "/path/to/文件归类(1).xlsx" \
  --sheet Sheet2 \
  --output apps/api/app/modules/classification/taxonomies/school_file_classification.json
```

预置分类或受管目录快照更新后，生成下一版统一 taxonomy：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python scripts/build_unified_taxonomy.py \
  --base apps/api/app/modules/classification/taxonomies/school_file_classification.json \
  --inventory rules/classification-source-inventory/managed-downloads-2026-07-v1.json \
  --inventory rules/classification-source-inventory/managed-downloads-2026-07-v2.json \
  --output apps/api/app/modules/classification/taxonomies/unified_school_file_classification.json \
  --version 2026-08-v3
```

`--inventory` 可以重复传入，构建器按参数顺序增量合并并自动去重。生成新版本时保留历史快照参数，再在末尾追加新快照，禁止直接把 `UNKNOWN`、`TEMPORARY`、`COLLECTION` 或年份目录提升为业务分类。

当前新增文件解析 Tool：

```text
read-original-file：读取当前用户上传原始文件的安全元信息，不返回本地路径或二进制内容
extract-document-text：解析 txt/md/csv/xls/xlsx/doc/docx/pdf/image，并将文本写入 document_pages；旧版 `.doc/.xls` 先通过 LibreOffice 隔离转换并发布为版本级持久化派生件，再由对应解析器读取，不覆盖原件
```

旧版 `.doc` 使用“持久派生件”链路：首次读取时通过 LibreOffice Headless 转换为 `.docx`，保存到
`FILE_STORAGE_ROOT/derivatives/office/` 并登记到 `document_artifacts`。后续解析、Docling、分类、摘要、
问答和重命名字段提取复用同一派生件；原始下载与真实改名仍操作 `.doc` 原件。用户说“重新解析”时
复用有效派生件，只重建解析结果；说“重新转换”时才同时绕过派生件缓存。

旧版 `.xls` 使用同一 LibreOffice 安全边界：输入副本、输出目录和 LibreOffice profile 相互隔离，输出
必须通过 OOXML 和 openpyxl 双重校验后，原子发布到 `FILE_STORAGE_ROOT/derivatives/office/`，并以
`CONVERTED_XLSX` 关联当前 `DocumentVersion`。正文抽取、Profile、统计分析和公式校验复用同一派生件；
派生件不是新上传原件，原 `.xls` 字节始终不变。

配置：

```dotenv
LEGACY_OFFICE_CONVERSION_ENABLED=true
LEGACY_OFFICE_CONVERTER=libreoffice
LIBREOFFICE_EXECUTABLE=
LEGACY_OFFICE_CONVERSION_TIMEOUT_SECONDS=90
LEGACY_OFFICE_MAX_FILE_SIZE_MB=100
LEGACY_OFFICE_DERIVATIVE_DIR=derivatives/office
```

LibreOffice 安装与路径：

```text
Windows: 安装 LibreOffice 64 位版，优先使用
         C:\Program Files\LibreOffice\program\soffice.com
macOS:   安装 LibreOffice.app，默认发现
         /Applications/LibreOffice.app/Contents/MacOS/soffice
Linux:   使用系统包管理器安装 libreoffice，默认发现 /usr/bin/soffice
```

`LIBREOFFICE_EXECUTABLE` 留空时按“PATH -> 平台默认目录”查找。Windows 优先 `soffice.com`，便于获得
可靠退出码；路径包含空格无需手工加引号。LibreOffice 暂时不可用时，系统仍可复用同规则且通过完整
校验的历史派生件；必须新转换却不可用时，`.doc` 可按既有规则受控降级，`.xls` 返回结构化失败，不能
用文件名或其他库冒充完整正文解析，也不会覆盖原件。

PDF、Excel、doc/docx 和图片 OCR 依赖：

```text
PyMuPDF
openpyxl
python-docx
Pillow
paddleocr
textutil 或 LibreOffice
LibreOffice（旧版 .doc 和 .xls 转换所需；.xls 不再使用 xlrd）
```

图片 OCR 和扫描 PDF OCR 默认使用 PaddleOCR CPU Provider；旧版 `.doc` 优先使用 LibreOffice 生成持久化 `.docx` 派生件，失败后再使用现有纯文本回退；旧版 `.xls` 由 LibreOffice 生成持久化 `.xlsx` 派生件后交给 openpyxl，并由各表格 Tool 复用。如果缺少依赖、没有可复用派生件或转换失败，Tool 会返回结构化错误，不会读取任意路径。

当前对话触发解析已支持多个附件顺序执行。单个文件 Tool 异常会记录为该文件的失败 `document_results.errors`，后续文件继续处理；并发执行、LangGraph map/reduce、步骤级重试和恢复后续单独实现。

查询 AgentRun：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id> \
  -H 'Authorization: Bearer <ops-or-admin-access-token>'
```

查询 Tool 调用：

```bash
curl http://127.0.0.1:8000/api/agent-runs/<agent_run_id>/tool-invocations \
  -H 'Authorization: Bearer <ops-or-admin-access-token>'
```

非法附件示例：

```bash
curl -X POST http://127.0.0.1:8000/api/conversations/conv-1/messages \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <access_token>' \
  -d '{"content":"帮我读取文件","attachments":[{"filename":"bad.pdf"}]}'
```

当前期望返回 HTTP `422`，因为附件缺少 `document_id`。

## 7. 工作副本重命名执行器

受管原始目录只负责保存不可变原始文件。用户对附件或受管目录提出重命名请求时，后端先把确定范围映射为活动工作副本，再生成 `RENAME_WORKING_COPIES` OperationPlan。未完成异步导入时返回 `WAITING_FOR_ASYNC_JOB`，不得改动上传暂存或受管原始文件。

命名字段解析仍使用：

```text
FILE_RENAME_PARSE_MODE=hybrid
FILE_RENAME_MAX_BATCH_SIZE=20
FILE_RENAME_EXECUTION_TIMEOUT_SECONDS=60
```

`FILE_RENAME_PARSE_MODE` 控制命名字段的解析来源，与执行器配置相互独立：

- `hybrid`：Docling 与原生解析器生成候选并逐字段仲裁，默认用于生产。
- `native`：只使用原生解析器，Docling 质量异常时用于紧急回退。
- `docling`：只使用 Docling，主要用于对比测试和问题定位；Docling 不可用时仍安全回退原生解析。

`FILE_RENAME_EXECUTOR`、F2 和旧 Native 受管文件执行器属于历史兼容代码，当前 Agent Runtime 不再创建或确认 `RENAME_FILES` / `RENAME_UPLOADED_FILES`。即使 `.env` 保留旧配置，也不能绕过工作副本白名单。

工作副本执行前必须同时校验计划中的当前相对路径、`DocumentVersion` 和内容 SHA-256。重命名和移动不创建新版本，但会写入 `WorkingCopyPathRecord`、ChangeSet、ChangeItem 和逐文件回执。计划创建时记录为 `PLANNED`，执行时推进到 `RUNNING`，最终为 `COMPLETED`、`FAILED` 或 `STALE`。

可通过以下 API 创建或查询工作副本路径计划：

```text
POST /api/operations/plans
GET  /api/operations/plans/{plan_id}
POST /api/operations/plans/{plan_id}/confirm
```

自动提取缺少年份或正文标题时，该工作副本只返回 `NEEDS_REVIEW`，不会进入执行计划。旧的
`file_rename_review_items -> RENAME_FILES` 即时更正链路已经退役；人工指定名称也必须携带稳定
`working_copy_id` 创建新的 `RENAME_WORKING_COPIES` 计划，不能复用历史待复核项直接改文件。

自动生成建议发生目标冲突时，不得自动分配 `_第二版`，也不得覆盖已有工作副本。系统先保留新文件的
原上传文件名，并通过普通用户回执询问：同时保留、保留已有、替换现有工作副本或删除现有工作副本。
只有用户明确选择“同时保留”后，后续确认流程才可以固化稳定版本后缀；替换或删除只能针对活动工作
副本生成 OperationPlan，不得修改不可变受管原件。

旧版 `.xls` 不使用 `xlrd`。LibreOffice 转换或表格正文解析失败时，如果文件名同时包含可验证年份和
可清理标题，重命名服务仍可使用表格文件名回退生成待确认建议，并继续保留失败的 ExtractionRun。
文件名回退只适用于 `.xls/.xlsx/.xlsm/.csv/.tsv`，可清理前导“附件”、括号日期、末尾提交单位加
八位日期、`new` 标记和“摸底统计表”中的“摸底”。该回退不能伪造正文证据，也不能用于分类结论。

历史 F2/Native 原地重命名测试不属于当前工作副本验收范围；不得通过启用集成测试把旧执行器重新接回生产确认入口。

### 7.1 上传附件工作副本重命名

聊天消息携带上传附件并要求重命名时，Planner 会把后端已解析的 `document_ids` 交给
`generate-rename-suggestions`。Tool 沿 `DocumentVersion -> UploadArchiveRecord -> ManagedFile -> WorkingCopy`
解析活动工作副本，并生成 `RENAME_WORKING_COPIES` OperationPlan。确认前不会修改文件；确认后只更新工作副本路径、工作副本 Document 展示名和同一版本的存储路径。

文件物理位置始终位于：

```text
WORKING_COPY_STORAGE_ROOT/<working_root_relative_path>/<new_basename>
```

目标目录和路径完全由后端计算，OperationPlan 的重命名输入只接受 basename 和稳定 `working_copy_id`。
当前阶段不根据分类选择目录。受管原始文件、上传暂存文件和内容版本均保持不变；成功和失败都写入
`confirmed-file-action` ToolInvocation、ChangeSet、工作副本路径记录和逐文件结果。

每次上传都创建独立的上传 Document 和暂存对象。删除尚未发送到消息的上传仅取消该上传生命周期并异步清理暂存文件；发现重复候选时，由用户逐文件选择继续上传、使用同工作区已有文件或取消上传。未经确认不得自动合并、覆盖或删除原始文件。

## 8. Neo4j 图谱增强分类

图谱分类和图向量默认以 Shadow 模式开启。PostgreSQL、taxonomy v2、分类反馈和受管目录扫描结果仍是事实源；Neo4j 只保存
可重建分类层级、目录角色、可信或弱分类关系及文档级聚合向量。全文和分块正文不会写入 Neo4j。
连接、依赖或本地模型不完整时服务显示 `DEGRADED` 并继续使用基础分类和 Jieba/GIN 检索。

本地或非 Docker 环境安装可选依赖：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python -m pip install -r requirements-graph.txt
```

Windows 11 全功能 CPU 生产部署使用 `deploy/docker-compose.production.yml`，该 Compose 已安装图谱依赖、
预下载 384 维本地 embedding 模型，并启动 Neo4j 5.26 Community 和独立 `graph-worker`。其他部署形态仍可
使用独立 Neo4j 主机。连接配置：

```text
GRAPH_CLASSIFICATION_ENABLED=true
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<password>
NEO4J_DATABASE=neo4j
NEO4J_QUERY_TIMEOUT_SECONDS=3
NEO4J_SYNC_ENABLED=true
GRAPH_CLASSIFICATION_MAX_HOPS=1
GRAPH_CLASSIFICATION_TOP_K=8
GRAPH_CLASSIFICATION_MODE=shadow
GRAPH_EMBEDDING_ENABLED=true
GRAPH_EMBEDDING_PROVIDER=local
GRAPH_EMBEDDING_MODEL_PATH=/absolute/path/to/local/model
GRAPH_EMBEDDING_MODEL_NAME=<model-name>
GRAPH_EMBEDDING_VERSION=document-semantic-v1
GRAPH_EMBEDDING_DIMENSION=384
GRAPH_VECTOR_INDEX_NAME=document_version_embedding_v1
GRAPH_VECTOR_TOP_K=12
GRAPH_VECTOR_MIN_SCORE=0.0
GRAPH_PROJECTION_WORKER_ENABLED=true
GRAPH_FEEDBACK_COLLECTION_ENABLED=true
GRAPH_CLASSIFICATION_ROLLOUT_PERCENT=10
GRAPH_FEEDBACK_EVAL_MIN_SAMPLES=100
MANAGED_PATH_CLASSIFICATION_PROFILE_DIR=./rules/managed-root-classification
MANAGED_PATH_DEFAULT_MODE=NONE
MANAGED_PATH_VECTOR_PILOT_LIMIT=1000
MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT=20
MANAGED_FILE_CLASSIFICATION_BATCH_SIZE=20
MATERIALIZE_ALL_MANAGED_FILES=true
MATERIALIZE_WORKING_COPY_BACKGROUND_PRIORITY=100
MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE=true
MATERIALIZE_WORKING_COPY_PRIORITY=20
```

受管目录文件分类不超过 `MANAGED_FILE_CLASSIFICATION_SYNC_LIMIT` 时在当前 AgentRun
内同步完成；超过阈值时创建 `CLASSIFY_MANAGED_FILES` 文件系统任务。部署环境必须同时运行
filesystem worker：

```bash
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.managed_files.worker
```

三层文件生命周期上线后，生产环境应拆分队列，避免归档或导入占满普通任务资源：

Windows CMD 开发环境可以直接运行 `scripts\start-file-agent-workers.cmd`。脚本会先执行同步预检：
读取项目根 `.env`，用当前机器的 `MANAGED_ROOT_*` 更新数据库中的运行时目录路径，真实打开每个目录
验证可读性，并停用旧版本误登记的 `scan_batch_size` 等伪目录。只有预检成功后，才会以独立窗口启动
scheduler 和五个合并后的 worker：`RECONCILE,SCAN`，
`DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT`，`SOURCE_ANALYSIS,ANALYSIS`，
`STRUCTURED_EXTRACTION` 和 `GRAPH`。它适合本地开发
与烟测；生产环境仍可按以下命令基于容量分别部署更多 worker。

Windows 必须从本机仓库根目录使用 Windows 路径执行，例如：

```cmd
cd /d E:\PycharmProject\file-agent
set "FILE_AGENT_PYTHON=D:\anaconda\envs\myenv\python.exe"
scripts\start-file-agent-workers.cmd
```

不能在 Windows CMD 中执行 macOS 的 `/Users/.../scripts/start-file-agent-workers.cmd`。如果预检返回
`MANAGED_ROOT_NOT_FOUND`，应修正 Windows 项目根 `.env` 对应目录；只有
`MANAGED_ROOT_PERMISSION_DENIED` 才表示当前 Windows 账户确实无目录枚举权限。预检失败会在创建任何
子进程前退出，不能通过管理员启动或跳过预检掩盖错误配置。程序只从当前仓库根及其上级查找 `.env`，
不会读取 Downloads 中的同名文件。

当 PostgreSQL 开发数据库在 macOS 和 Windows 间复用、但
`WORKING_COPY_STORAGE_ROOT=./storage/working-copies` 是各自本地目录时，数据库可能已有 WorkingCopy
记录而当前机器缺少物理文件。扫描 worker 会识别该不一致并重新激活原 IMPORT 任务；生命周期 worker
在原件哈希仍与已导入版本一致时重新物化同一相对路径。它不会创建第二条 WorkingCopy，不会覆盖内容
不同的已有文件，也不会自动恢复用户已移入回收站的副本。

```bash
PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=lifecycle-1 \
  FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE,FILE_OPERATION,MATERIALIZE,IMPORT \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=source-analysis-1 \
  FILESYSTEM_WORKER_QUEUES=SOURCE_ANALYSIS,ANALYSIS \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=structured-extraction-1 \
  FILESYSTEM_WORKER_QUEUES=STRUCTURED_EXTRACTION \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=graph-1 \
  FILESYSTEM_WORKER_QUEUES=GRAPH \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api FILESYSTEM_WORKER_ID=reconcile-scan-1 \
  FILESYSTEM_WORKER_QUEUES=RECONCILE,SCAN \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker

PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.file_lifecycle.scheduler

PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
  -m app.modules.file_lifecycle.watcher
```

API 启动钩子、scheduler 和 watcher 都只创建 `filesystem_jobs`。实际 SHA-256 查重、归档、扫描、源侧分析、后台物化、布局修复和暂存清理由 worker 完成。受管目录扫描对新增文件不再预先完整哈希；每批立即创建 `SOURCE_ANALYSIS` 任务，源侧分析完成后即可通过摘要和正文索引检索、回答，并自动创建低优先级 `MATERIALIZE_WORKING_COPY`，逐步把全部受管文件同步到共享工作目录。全量同步未完成期间，检索同时覆盖活动工作副本和未物化但已分析的源文件；用户查询、阅读或选择到的最终相关源文件会复用同一幂等任务并提升优先级，回答不等待物理复制。物化复用源侧页面和索引，不重复 LibreOffice 转换；上传归档的即时副本仍由 `IMPORT` 兼容处理。尚未完成源侧分析的文件只能参与元数据候选，涉及正文或总结时必须先完成分析，不能编造内容结论。`REPAIR_WORKING_COPY_LAYOUT` 会先把旧根前缀以及历史“待整理/待确认”路径迁到 `shared/<root_key>/<源相对路径>`，并写入 `SYSTEM_LAYOUT_REPAIR` 路径记录。GRAPH worker 完成一次性 Neo4j bootstrap 和正式分类 Outbox 增量投影，API 重启不再同步执行 `sync_all()`。任务通过租约和幂等键恢复，每个任务最多尝试三次；达到上限后保持 `FAILED`。ops/admin 可在 `/admin/failed-files` 查看失败文件，状态接口为：

部署本次全量同步逻辑后必须重启 API、scheduler、`RECONCILE,SCAN`、
`SOURCE_ANALYSIS,ANALYSIS` 和包含 `MATERIALIZE,IMPORT` 的生命周期 worker；只重启 API 会创建扫描任务，
但不会实际分析或复制文件。

本次布局修复没有新增数据库列或表，不需要新增 Alembic migration；更新代码后必须重启 scheduler、
`RECONCILE`、`IMPORT`、`ANALYSIS` 和 `GRAPH` worker。不要手工移动 `待整理`、`待确认` 或旧根目录，
也不要直接修改 `working_copy_roots.relative_storage_path`。迁移任务会同时移动物理文件并更新
`WorkingCopy`、当前 `DocumentVersion`、`FileObject` 和 `working_copy_path_records`。部署后可用以下
只读 SQL 核对任务与目标前缀：

```sql
SELECT job_type, status, result_json, error_message
FROM filesystem_jobs
WHERE job_type IN ('REPAIR_WORKING_COPY_LAYOUT', 'GRAPH_BOOTSTRAP_PROJECTION', 'PROJECT_GRAPH_OUTBOX')
ORDER BY created_at DESC;

SELECT root_key, relative_storage_path, status
FROM working_copy_roots
ORDER BY root_key;
```

共享根应统一为 `shared/<root_key>`；布局任务失败时保留原件不变，并应先根据任务事件修复工作副本目录
权限或未知占位文件，再由 ops/admin 显式重处理，不能直接删除冲突文件。

```text
GET /api/jobs/{job_id}
GET /api/jobs/{job_id}/events
GET /api/admin/failed-files
```

部署挂载必须保持以下权限边界：

- API、Agent Runtime 和普通 Tool 对受管原始目录只读，不能挂载 `MANAGED_ROOT_ARCHIVE_WRITE_PATH`。
- 归档 worker 可以使用 `MANAGED_ROOT_ARCHIVE_WRITE_PATH`，但只允许追加，不允许覆盖、改名、移动或删除已有原始文件。
- import worker 读取受管原始目录并写 `WORKING_COPY_STORAGE_ROOT`。
- 文件操作 worker 只写 `WORKING_COPY_STORAGE_ROOT` 和 `TRASH_STORAGE_ROOT`。
- `TRASH_AUTO_PURGE_ENABLED` 在 MVP 必须保持 `false`。

首次上线顺序必须是：执行 `python -m alembic -c apps/api/alembic.ini upgrade head` 并确认当前 head
至少为 `20260722_0001`，准备三个目录并校验权限，启动分队列 worker，再启动 scheduler/watcher，
最后启动 API。API 健康不代表首次全量导入已经完成，应通过 job 状态和事件确认。

worker 使用 `MANAGED_FILE_CLASSIFICATION_BATCH_SIZE` 分页读取文件，并隔离单文件失败。
普通用户可通过 `GET /api/filesystem-jobs/{job_id}` 查询自己创建的任务；任务完成后会回写
原 AgentRun、分类建议、ChangeSet 和逐文件回执，聊天页会自动轮询并刷新。

首次上线顺序：

1. 保持默认 `GRAPH_CLASSIFICATION_MODE=shadow` 发布 API；Shadow 结果不得改变用户可见分类。
2. 执行数据库迁移：`python -m alembic -c apps/api/alembic.ini upgrade head`。
3. 安装图谱依赖，准备本地 Embedding 模型并验证 Neo4j 网络连接。
4. 为需要弱标签治理的受管根创建 `rules/managed-root-classification/<root_key>.json`；没有 Profile 的弱标签目录保持 `UNKNOWN`。
5. 执行首次事实投影：

   ```bash
   PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
     -m app.modules.knowledge_graph.cli sync-all
   ```

6. 访问 `GET /api/health`，确认 `knowledge_graph.status=ok`、`graphrag_package=available` 和 `embedding_package=available`。
7. 确认 `GRAPH_CLASSIFICATION_ENABLED=true`、`GRAPH_EMBEDDING_ENABLED=true`、`GRAPH_CLASSIFICATION_MODE=shadow`，重启并完成分类 smoke test。
8. 分层生成首批最多 1,000 份文档向量：

   ```bash
   PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python \
     -m app.modules.knowledge_graph.cli sync-embeddings --limit 1000
   ```

9. Shadow 链路稳定后才设置 `GRAPH_CLASSIFICATION_MODE=enabled`；初始仅按 `GRAPH_CLASSIFICATION_ROLLOUT_PERCENT` 小范围展示建议。
10. 用户在分类证据展开区明确选择“正确、错误、更正”后，反馈写入 PostgreSQL；未操作不计样本。
11. 有效反馈达到 `GRAPH_FEEDBACK_EVAL_MIN_SAMPLES` 后冻结分层评测集，离线回放通过并人工批准后才能扩大范围。

分类反馈接口：

```text
POST /api/classification/suggestions/{suggestion_id}/feedback
GET  /api/classification/feedback/summary
```

`sync-all` 和 `sync-embeddings` 都会写入 `graph_projection_runs`。单文件向量失败不会阻塞同批其他文件；
相同 SHA-256、模型、版本和维度全部一致时复用已有向量。

如果 Neo4j 查询超时或不可用，分类服务会记录 `classification.graph_query.degraded`，并自动回退到现有
规则/LLM 分类，不中断上传、解析、OCR 和其他文件。立即回滚只需要：

```text
GRAPH_CLASSIFICATION_ENABLED=false
GRAPH_CLASSIFICATION_MODE=off
GRAPH_EMBEDDING_ENABLED=false
NEO4J_SYNC_ENABLED=false
```

第二版本仍不启用自动实体构图、自由 Cypher、Text2Cypher 或 GraphRAG 文件问答。`VectorCypherRetriever`
只使用后端固定遍历模板，普通用户响应不会暴露相似来源文件身份。

## 9. 正式上线范围、配置与 Adaptive Planner 灰度

本节定义当前版本可以对普通用户正式提供的功能、部署时应修改的配置，以及 Adaptive Planner 从
Shadow 到真实执行的反馈闭环。这里的“正式上线”指功能具备明确权限、审计、失败降级和人工处置路径，
不等同于把所有实验性开关同时设为 `enabled`。

### 9.1 可正式提供的用户能力

普通用户可以正式使用以下能力：

- 登录、聊天会话和带明确文字任务的附件上传。
- PDF、DOC、DOCX、XLS、XLSX、TXT、MD、CSV 与常见图片的上传、异步查重、归档、工作副本导入、
  解析、OCR、摘要、Chunk 和 Evidence 建立。
- 受管原始目录扫描、定时对账，以及部署了 watcher 后的近实时发现；所有普通读写对象均为工作副本，
  原件不被 Agent 修改。
- 基于 Jieba、PostgreSQL `simple` FTS/GIN 与 `pg_trgm` 的两阶段文件检索，并区分“已验证相关”与
  “可能相关”结果。
- 基于当前活动版本原文 Evidence 的文件解释、总结和问答；证据不足时明确说明不能得出结论。
- 多标签分类建议，以及用户对分类建议的接受、拒绝和更正。
- 工作副本的重命名、移动、移入回收站和恢复；高风险动作必须先创建并确认 OperationPlan，路径变化
  写入 `working_copy_path_records`。
- 管理员查看 AgentRun、ToolInvocation、ChangeSet、后台文件任务、失败任务和中文诊断时间线。

本次上线不得对外承诺以下能力：通用向量语义检索、图谱结果自动写入正式分类、自动永久删除、
自动创建或启用 Tool/Skill、以及默认向外部模型发送后台文件摘要。Neo4j 和 Adaptive Planner 可以按本节
灰度方式作为增强能力上线，但不能绕过原有的确定性检索、分类和确认边界。

### 9.2 生产配置文件与基础配置

Docker Compose 部署时，只创建并维护 `deploy/.env`：它由 `deploy/.env.production.example` 复制生成，
包含真实密码、受管目录和服务地址，绝不能提交 Git。直接在服务器运行 API 与 worker 时，使用仓库根
目录的 `.env`；根目录 `.env.example` 仅作字段模板，不填写真实值。

当前正式部署模板面向 Windows 11、6 核 CPU、32GB 内存，受管目录为 `E:/workdata`，并启用全部本地图片
结构化抽取能力。详细资源、模型固化、联网构建和离线导入步骤见
`docs/windows11-full-cpu-docker-deployment-plan.md` 与 `deploy/README.md`。

生产部署至少核对以下配置：

```env
CADDY_SITE_ADDRESS=你的正式域名
POSTGRES_PASSWORD=随机强密码
JWT_SECRET_KEY=随机强密钥

LLM_ENABLED=true
LLM_API_KEY=你的密钥
LLM_BASE_URL=你的兼容接口地址
LLM_CHAT_MODEL=你的模型名称
LLM_TIMEOUT_SECONDS=30

MANAGED_ROOT_WORKDATA=宿主机受管原始目录
MANAGED_ROOT_WORKDATA_DISPLAY_NAME=工作资料库
MANAGED_ROOT_WORKDATA_CLASSIFICATION_MODE=NONE
MANAGED_ROOT_VOLUME_MODE=ro

UPLOAD_MAX_SIZE=按网关和服务器容量设置
LOG_LEVEL=INFO
LOG_RETENTION_DAYS=7
```

以下安全默认值应保持不变：

```env
DOCUMENT_SUMMARY_PROVIDER=extractive
CLASSIFICATION_SUMMARY_PROVIDER=extractive
LLM_CLASSIFICATION_MODE=rule_only
LLM_CLASSIFICATION_ALLOW_FREE_PATHS=false
EMBEDDING_ENABLED=false
EMBEDDING_PROVIDER=disabled
```

生产 Compose 已将 Web API 固定为同域 `/api`。只有部署为前后端不同域名时，才需要额外修改前端
`VITE_API_BASE_URL` 和后端 CORS 白名单；这不是普通 `.env` 配置能够单独解决的问题。

首次发布或升级代码后，仍必须执行 Alembic migration 并按第 8 节启动 API、各队列 worker 和 scheduler。

### 9.3 Adaptive Planner 配置与灰度

首次生产发布保持 100% Shadow：

```env
ADAPTIVE_PLANNER_MODE=shadow
ADAPTIVE_PLANNER_ROLLOUT_PERCENT=0
ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT=100
ADAPTIVE_PLANNER_SCHEMA_VERSION=planner-decision-v1
```

在 Shadow 模式下，Legacy Planner 产生用户可见结果和真实 Tool 调用；Adaptive Planner 只生成并校验
`PlannerDecision`，不会执行第二次 Tool。管理员通过以下接口查看当前 Catalog/schema 下的只读对比指标：

```text
GET /api/admin/planner-shadow/metrics
GET /api/admin/agent-runs
GET /api/admin/agent-runs/{agent_run_id}/diagnostics
```

进入 5% 真实执行灰度时修改为：

```env
ADAPTIVE_PLANNER_MODE=enabled
ADAPTIVE_PLANNER_ROLLOUT_PERCENT=5
```

稳定哈希命中的 5% 用户由 Adaptive Planner 生成实际 Tool 计划；未命中用户仍走 Legacy 可见执行并保留
Adaptive Shadow 对比。因此剩余用户天然构成对照组，不能为了扩大比例关闭 Shadow 记录。扩大顺序固定为：

```text
Shadow 100%
-> enabled 5%
-> enabled 25%
-> enabled 50%
-> enabled 100%
```

任一安全问题出现时立即回退，不需要回滚数据库：

```env
ADAPTIVE_PLANNER_MODE=legacy
```

安全问题包括未知或禁用 Tool、未授权文件范围、跳过 OperationPlan 确认、重复相同 Tool 输入，或把
“可能相关”候选作为已经证实的文件事实。扩大比例前还应比较 5% 组与 Shadow/Legacy 对照组的完成率、
失败率、P95 耗时、无意义澄清率、重复调用率和同一问题二次重述率。

### 9.4 Adaptive Planner 的反馈、定位与修复闭环

分类建议已有“接受、拒绝、更正”反馈。普通对话结果还需要增加独立的通用任务反馈闭环，不能把
分类反馈误作 Planner 质量反馈。后续实现应新增 `agent_run_feedbacks`，至少记录：

```text
agent_run_id
user_id
rating: HELPFUL / NOT_HELPFUL
issue_type
comment
created_at
resolved_at
resolution_type
resolution_note
```

`issue_type` 固定为以下受控枚举，普通用户只看到中文描述，不能看到 Tool 或 Skill 名称：

```text
WRONG_FILE_SCOPE
MISSED_RELEVANT_FILE
FALSE_RELEVANCE
WRONG_TOOL_OR_ACTION
UNNECESSARY_CLARIFY
REPEATED_ACTION
UNSAFE_ACTION
ANSWER_NOT_SUPPORTED
OTHER
```

每一条负反馈必须关联原 `agent_run_id`。管理员从诊断页读取原始请求、Planner 模式、Catalog 指纹、
PlannerDecision、实际 ToolInvocation、文件范围、检索条件、异步任务状态和最终回执，再按根因处理：

| 根因 | 修复位置 |
|---|---|
| Planner 选错 Tool 或无意义澄清 | Catalog 描述、PlannerDecision 约束、Prompt、绑定规则 |
| Tool 正确但找错或漏掉文件 | 查询解析、短语策略、两阶段检索、范围澄清 |
| 文件范围错误 | 文档选择、同名选择、后端 scope 校验 |
| 有证据但回答错误 | evidence-answer Prompt、结构校验、引用校验 |
| Tool 或后台任务失败 | 对应 Tool handler、生命周期任务、存储或索引链路 |
| 当前 Catalog 缺少能力 | 生成 Capability Suggestion，人工开发、测试、注册后启用 |

用户反馈不得直接修改生产 Prompt、Tool、Skill 或 taxonomy。每个确认的问题都必须脱敏沉淀为回放案例，
包含用户请求、附件/文件范围、期望 PlannerDecision、期望 Tool 序列、期望安全边界和期望回执类别；
修复后增加 deterministic fake 回归测试，重新回到 Shadow 验证。只有“回放通过、Shadow 不再复现、
5% 灰度未出现同类反馈”同时满足，才允许扩大灰度比例。

进入下一档灰度前必须满足：安全问题为零；每条负反馈均能定位到 AgentRun 并有处置结论；修复样本已经
进入回归测试；并且检索、同名选择、文件解释、总结、分类解释、重命名、删除和恢复等关键场景均有覆盖。

### 9.5 Neo4j 与通用向量检索的上线边界

Neo4j 图谱增强分类先使用 Shadow 配置，详见第 8 节；只有真实 Neo4j smoke、投影重试、故障降级和
分类反馈评测通过后，才将 `GRAPH_CLASSIFICATION_MODE` 改为 `enabled` 并按
`GRAPH_CLASSIFICATION_ROLLOUT_PERCENT` 小范围展示分类建议。图谱结果始终先保持 `SUGGESTED` 或
`NEEDS_REVIEW`，不得自动写入正式分类。

通用向量语义检索目前不是一个可直接开启的生产开关。`EMBEDDING_ENABLED=true` 之前必须完成独立的
embedding Provider、异步回填、模型版本与维度管理、pgvector 索引、词法与向量混排、权限范围校验、
故障降级和冻结查询集评测。完成前，正式主检索仍为本地 Jieba + PostgreSQL FTS/GIN + `pg_trgm`。

### 9.6 文件检索完整性提示

聊天检索卡和 `POST /api/search` 会返回 `search_completeness`，让用户判断当前检索结果能否视作找全。
该字段由后端直接查询活动工作副本、当前检索资料和本轮检索状态生成，不能由 LLM 或前端自行推断。

| 状态 | 用户含义 | 是否可说“已找全” |
|---|---|---:|
| `COMPLETE` | 当前唯一范围内的活动文件均已具备当前索引，本轮未降级且未触及候选保护上限 | 是，仅限当前条件 |
| `PROCESSING` | 部分活动文件仍在准备解析或索引 | 否 |
| `PARTIAL` | 索引降级、已知文件失败，或匹配候选达到保护上限 | 否 |
| `UNVERIFIABLE` | 附件尚未形成活动工作副本，或用户仍需确认查找范围 | 否 |

这里的“找全”只覆盖后端已确认的文件范围和当前检索条件，不等同于“所有业务上可能相关的文件都被
理解并找到”。检索卡会同时显示范围、可检索数量和缺口数量；用户应在 `PROCESSING` 或 `PARTIAL`
状态下等待处理完成或补充条件后再次检索。

### 9.7 置信门控首次自动分类发布

迁移到 `20260827_0001` 后，生产环境必须先保持以下安全默认值：

```dotenv
AUTO_PRIMARY_CLASSIFICATION_ENABLED=false
AUTO_INITIAL_PLACEMENT_ENABLED=false
AUTO_CLASSIFICATION_SHADOW_MODE=true
AUTO_CLASSIFICATION_POLICY_VERSION=auto-placement-top1-test-v1
AUTO_CLASSIFICATION_CALIBRATION_VERSION=unpublished
AUTO_CLASSIFICATION_TARGET_PRECISION=0.99
AUTO_CLASSIFICATION_FULL_TAXONOMY_ENABLED=true
AUTO_CLASSIFICATION_GLOBAL_FALLBACK_POLICY=conservative-v1
AUTO_CLASSIFICATION_FALLBACK_THRESHOLD=0.90
AUTO_CLASSIFICATION_FALLBACK_MARGIN=0.20
```

Shadow 会写 `document_organization_decisions`，但不会移动已有 `ACTIVE` 文件，也不会创建
`AUTO_APPLIED` 关系。运维抽检必须排除 `feature_snapshot_json.shadow_only=true` 的结果，或将其单独
作为“如果启用会怎样”的回放数据统计。

只有冻结评测集达到方案中的精度门槛、校准版本已经发布且同名冲突/失败恢复测试通过后，才允许按以下
顺序灰度新上传文件：

1. 先设置已发布的 `AUTO_CLASSIFICATION_CALIBRATION_VERSION`，保持 Shadow 观察。
2. 开启 `AUTO_PRIMARY_CLASSIFICATION_ENABLED=true`，仍保持首次物理落位关闭。
3. 小流量实例同时设置 `AUTO_INITIAL_PLACEMENT_ENABLED=true` 和
   `AUTO_CLASSIFICATION_SHADOW_MODE=false`；此时只有新上传归档进入 `ORGANIZING`。
4. 观察 `AUTO_ORGANIZED`、`NEEDS_REVIEW`、`TARGET_NAME_CONFLICT`、失败率和
   `ORGANIZING -> ACTIVE` 时延，再逐步扩大实例或流量。

紧急停止新的自动落位只需把 `AUTO_INITIAL_PLACEMENT_ENABLED=false` 或
`AUTO_PRIMARY_CLASSIFICATION_ENABLED=false` 并重启 Worker。该操作不会移动或回写既有
`AUTO_APPLIED` 文件；既有活动文件的后续路径变化仍必须经过 OperationPlan。

## 10. 当前限制

- 当前已接入 OpenAI-compatible LLM 意图理解；默认 `LLM_ENABLED=false` 时仍使用 `DeterministicPlanner`。
- Adaptive Planner 已具备 Catalog 校验、步骤级绑定、3 轮规划预算、Shadow 对比和稳定灰度开关，但尚未达到生产 Shadow 观察期与默认启用门槛；当前默认只读 Shadow，不能直接改为 100% enabled。
- 现有 Tool 已统一经过输出模型校验，但部分旧 Tool 仍使用迁移期通用输出模型；进入 Adaptive 主路径的核心 Tool 需要继续收敛为各自严格业务 output schema。
- 当前已持久化 user、default workspace、message、AgentRun、ToolInvocation、Document、document_insights、document_extraction_runs、document_pages、document_classification_runs、document_category_suggestions、document_category_feedback、change_sets、change_items、operation_plans 和 operation_confirmations。
- OperationPlan 已支持工作副本重命名、移动、移入回收站和恢复；普通用户可以在 `/chat` 通过自然语言生成同名冲突、移入回收站和恢复计划，并在页面确认后执行。重命名、移动不新增工作副本版本，所有路径变更写入 `working_copy_path_records`。受管原始目录保持不变；自动永久删除仍未开放。
- 没有白名单执行器的 OperationPlan 不能确认，计划保持 `WAITING_CONFIRMATION`，不会伪造 `EXECUTED`。
- 当前已支持读取当前用户自己的原始文件元信息和解析文本内容；其他多数 Tool handler 仍是结构化占位实现。
- 当前已有最小 JWT 鉴权，但没有 refresh token、复杂 RBAC、ACL 或 admin 权限体系。
- 当前前端已有注册、登录、Chat、异步上传状态、逐文件重复确认卡和通用 OperationPlan 确认卡；同名文件处理、移入回收站及恢复均可从对话完成，独立文件管理界面仍待补充。

## 11. 维护规则

以下任一内容发生变化时，必须同步更新本文和 `README.md`：

- 启动命令。
- 服务端口或 host。
- Python 环境或依赖安装方式。
- 前端依赖安装方式或启动命令。
- 测试命令。
- 新增或删除可直接调用的接口。
- 当前限制被解除，例如接入数据库、真实文件解析、大模型 Planner 或鉴权。
