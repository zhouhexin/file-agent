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
- `docs/adaptive-planner-execution-loop-implementation-plan.md`：Catalog 驱动 Planner、步骤级执行循环、能力建议和 Shadow 灰度的当前实施依据。
- `docs/langgraph-framework-decision.md`：选择 LangGraph 作为 Agent Runtime 底层编排框架的架构决策。
- `docs/file-rename-llm-validation-implementation-plan.md`：重命名差异风险、LLM 证据校验、降级和验收计划。
- `docs/classification-topic-summary-implementation-plan.md`：分类主题摘要优先的候选召回、原文证据校验、开源选型和Shadow上线方案。
- `docs/managed-original-working-copy-trash-implementation-plan.md`：受管原始目录、工作副本目录、回收站目录、重复上传确认和异步归档导入方案。
- `docs/stage-4-low-resource-two-stage-retrieval-plan.md`：阶段四 CPU-only 两阶段文件检索的边界、数据流与验收依据。
- `docs/stage-5-llm-efficient-evidence-answer-plan.md`：阶段五准确性优先的 LLM 证据回答、引用校验、缓存、前端和验收计划。
- `docs/stage-5-frontend-backend-acceptance-test-cases.md`：阶段五从前端提问到 PostgreSQL、Neo4j、文件系统和日志的验收用例。
- `docs/stage-6-natural-language-correction-shared-file-organization-plan.md`：阶段六自然语言分类纠正、正式分类和共享工作副本整理方案。
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
上传文件先保存到 `FILE_STORAGE_ROOT=./storage/uploads` 暂存目录并创建异步查重任务。无候选时自动异步归档；发现同名、相同内容或高度相似文件时，聊天页逐文件要求用户选择“继续上传”“使用已有文件”或“取消上传”。
文件生命周期固定使用三层名词：`受管原始目录`保存不可变原始文件，`工作副本目录`承载 Agent 的增删改查，`回收站目录`保存可恢复的工作副本删除结果。重命名和移动只改变工作副本路径，不新增 `DocumentVersion`；原始文件始终不变。普通用户可以在 `/chat` 通过自然语言处理同名冲突、移入回收站和恢复文件，所有物理动作都必须先展示并确认 OperationPlan。
所有用户共用唯一物理工作目录：受管资料和上传归档每个文件只导入一份，固定保存于 `shared/<root_key>`，不再按用户 default workspace 复制。所有普通用户可以检索和读取共享 `ACTIVE` 工作副本；用户 default workspace 仍只保存并隔离会话、个人附件上下文、上传来源、反馈和审计。共享目录上的改名、移动、回收站和恢复计划会明确提示其影响范围，仍须由发起用户确认。
服务端结构化日志默认保存到 `LOG_DIR=./logs`，按天生成 `file-agent-YYYY-MM-DD.log`，启动时会删除超过 `LOG_RETENTION_DAYS=7` 天的日志。
旧版 `.xls` 不再通过 `xlrd` 直读：系统必须先用 LibreOffice/`soffice` 在隔离临时目录和独立 profile 中转换为临时 `.xlsx`，校验输出后再由 `openpyxl` 解析全部工作表。转换器缺失或输出无效时返回结构化失败，原 `.xls` 字节不变，临时 `.xlsx` 不登记为上传原件。
上传采用分块流式写入，`UPLOAD_MAX_FILE_SIZE_MB` 是可按部署容量调整的资源保护上限，默认 1024 MB，并非固定业务限制。当前阶段只执行扩展名、基础 MIME、宏和加密风险检查，不实现、也不宣称已执行病毒扫描。
PDF、DOCX 默认启用本地 Docling 结构化解析，并把文档元素和位置写入 `document_elements`；Docling 不可用时自动回退现有解析器，扫描件仍由现有 OCR 链路处理。
文件重命名统一生成 `RENAME_WORKING_COPIES` OperationPlan，确认后由工作副本执行器执行；旧的受管原始文件 Native/F2 执行通道和上传暂存重命名通道不再对 Agent 开放。
上传附件通过查重后由独立 worker 归档到受管原始目录；`IMPORT` worker 以一次源文件读取完成哈希和原子复制，按 `shared/<root_key>/<源相对路径>` 登记 `ACTIVE` 工作副本，随后由 `ANALYSIS` worker 异步完成正文解析、双摘要、分类和 Chunk 索引。普通受管目录在启动扫描后先由 `SOURCE_ANALYSIS` 建立只读正文与证据索引，每个修订 READY 后自动创建低优先级 `MATERIALIZE` 任务，最终把全部文件同步到共享工作目录。同步未完成期间，检索合并活动工作副本和未物化源侧索引；用户命中的既有物化任务会被提升优先级，同一文件不并发复制。同步不会因不同目录中的同名文件停顿，也不会生成“待整理/待确认”物理目录；同名歧义只在上传、查询或实际使用相关文件时提示选择。后台双摘要默认使用 CPU-only Jieba + LexRank。普通用户不展示内部状态、Skill 或 Tool。对话找文件默认使用 CPU-only 两阶段检索，精确问答仍必须回到原文取证。后续重命名、移动和删除计划必须以 `working_copy_id` 为对象，不能再修改受管原始目录。
每个成功解析的工作副本内容版本会在发布前幂等建立 Chunk/Evidence。当前无 GPU 部署使用 Jieba + PostgreSQL `simple` FTS/GIN + `pg_trgm` 的 CPU 词法索引；`embedding vector(1536)` 只保留空扩展槽，默认 `EMBEDDING_ENABLED=false`，不会下载向量模型或要求应用服务器安装 GPU。后续可接独立 GPU provider 异步回填，不改变已有 Chunk、Evidence 和引用 ID。
默认不启用真实 LLM 调用；如需让对话阶段使用大模型理解用户需求，请在 `.env` 中配置 `LLM_ENABLED=true`、`LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_CHAT_MODEL`。当前 LLM 客户端使用 OpenAI-compatible Chat Completions 接口。
LLM 启用后，Adaptive Planner 默认运行在 `ADAPTIVE_PLANNER_MODE=shadow`：Legacy Planner 继续产生用户可见结果，Adaptive Planner 只能引用本次 `CatalogSnapshot` 中已启用的 Tool/Skill 并进行只读对比，不会产生第二次 Tool 调用。当前每次 AgentRun 最多规划 3 轮、实际调用 5 次 Tool；高风险步骤遇到确认边界会暂停而不是跳过。现有 Catalog 无法满足明确目标时，可以生成去重、脱敏的能力建议，由 ops/admin 在 `/admin/capability-suggestions` 评审；建议不会自动创建代码或启用 Tool/Skill。
Adaptive 灰度启用后，`hybrid-search` 会把结果数量、后端确认的实际查询条件、索引状态和受控文件 ID 交回 Planner，由 Planner 在三轮预算内决定结束、调整查询或继续读取证据。普通用户只看到最终文件结果和“本次查找采用的条件”，不显示 Skill、Tool 或规划预算。ops/admin 可在 `/admin/agent-runs` 查看中文任务诊断时间线；对应接口为 `GET /api/admin/agent-runs` 和 `GET /api/admin/agent-runs/{agent_run_id}/diagnostics`。
后台普通摘要和分类主题摘要分别由 `DOCUMENT_SUMMARY_PROVIDER=extractive`、`CLASSIFICATION_SUMMARY_PROVIDER=extractive` 控制；这两个默认值不需要 GPU 或模型服务。阶段五的用户问答和完整总结由 `EVIDENCE_ANSWER_PROVIDER=llm` 控制，并且只有 `LLM_ENABLED=true` 时才调用模型；回答必须绑定活动工作副本当前版本的 EvidenceSpan。LLM 关闭或校验失败时只返回带明确限制的原文摘录，不生成猜测答案。表格金额、计数和汇总继续由确定性 `analyze-spreadsheet` 计算。
分类判定默认仍为 `LLM_CLASSIFICATION_MODE=rule_only`。如需让 LLM 在候选分类内做语义判定，可设置 `LLM_CLASSIFICATION_MODE=hybrid`；如需允许 LLM 自由提出新分类路径，还必须显式设置 `LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true`，该类结果只会以 `NEEDS_REVIEW` 保存，不会自动写入正式分类目录。
Neo4j 图谱和图向量默认以 Shadow 模式开启。API 启动只创建 GRAPH 队列任务；一次性 bootstrap 和后续正式分类 Outbox 增量投影由独立 GRAPH worker 执行，连接失败不会阻塞 API、扫描或文件复制。具体步骤见 `docs/runbook.md`。

消息接口需要先注册、登录并携带 `Authorization: Bearer <access_token>`。示例见 `docs/runbook.md`。

除 API 外，三层文件生命周期至少需要独立启动 worker 和 scheduler；需要近实时同步时再启动 watcher：

Windows CMD 可直接执行 `scripts\start-file-agent-workers.cmd`。脚本会先读取项目根 `.env`，把当前
Windows 机器的 `MANAGED_ROOT_*` 路径同步到数据库并真实验证目录可读；预检失败时不会启动任何
worker。预检通过后分别启动 scheduler、扫描 worker、生命周期 worker、一个 `SOURCE_ANALYSIS` worker、
两个 `MATERIALIZE,IMPORT` worker、一个 `ANALYSIS` worker 和一个 `GRAPH` worker；增加
`--with-watcher` 才会额外启动 watcher。若 Python 不在 PATH，先设置 `FILE_AGENT_PYTHON` 为解释器
绝对路径。脚本无论从哪个当前目录调用都会先切换到仓库根，因此相对
`WORKING_COPY_STORAGE_ROOT=./storage/working-copies` 始终指向仓库内目录。共享开发数据库已有
WorkingCopy 记录但当前机器物理文件缺失时，下一次扫描会重新调度导入并从不可变原件修复本地副本。
以下是 macOS/Linux 的等价分终端命令：

```bash
# 可在不同进程中分别设置 FILESYSTEM_WORKER_QUEUES。SCAN 每完成一批只提交
# SOURCE_ANALYSIS；源侧索引完成即可检索和回答，随后由 MATERIALIZE 后台完成全量工作副本同步。
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=DUPLICATE_CHECK,ARCHIVE \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=SOURCE_ANALYSIS \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=MATERIALIZE,IMPORT \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=ANALYSIS \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=GRAPH \
  /opt/homebrew/anaconda3/envs/py311/bin/python -m app.modules.managed_files.worker
PYTHONPATH=apps/api FILESYSTEM_WORKER_QUEUES=FILE_OPERATION \
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

登录数据库
```
psql -h 172.17.16.2 -U fileagent_user -d fileAgent
```
