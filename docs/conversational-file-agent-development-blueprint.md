# 对话式文件智能体开发蓝图

本文是 File Agent 的总体开发蓝图。项目定位以根目录 `agent.md` 为准。

File Agent 是面向学校/学工业务场景的对话式文件工作智能体。用户通过聊天框上传、读取、OCR、分类、检索、整理和处理文件；系统从第一版开始使用 LangGraph 实现 Agent Runtime，并通过白名单 Tool、ChangeSet、OperationPlan 和证据链约束每次执行。

## 1. 总体结论

推荐新项目采用：

```text
前端：React + TypeScript
后端：Python FastAPI
Agent 编排：LangGraph
数据库：PostgreSQL + pgvector
文件存储：第一版本地文件，后续切换 MinIO/S3/COS
异步任务：第一版 FastAPI BackgroundTasks，后续 Redis + Celery/RQ
文档解析：python-docx / openpyxl / pdfplumber 或 PyMuPDF / LibreOffice / OCRmyPDF

Python 环境：使用用户当前已经配置好的 Python 环境；`pyproject.toml` 只用于记录依赖和工具配置，不强制引入 `uv`、Poetry、Conda 或新虚拟环境。
大模型：OpenAI 兼容接口，外部联网和外部检索默认关闭
```

不建议直接复用当前 OpenOffice 的 NestJS 后端。当前项目模块混杂，包含 DingTalk、外部 Agent 平台、OnlyOffice、复杂 Wiki Skill、COS 强绑定等第一版不需要的逻辑。新项目应从干净结构开始，用当前项目作为产品与交互参考，而不是代码迁移目标。

## 2. 产品定位

File Agent 不是问答系统。证据回答只是智能体的一个 Skill。

长期目标：

```text
聊天框作为唯一主入口
-> 用户上传文件或提出文件工作请求
-> LangGraph 创建 AgentRun
-> Agent 识别意图、附件、上下文和用户习惯
-> Agent 选择 Skill 并生成受控 Tool 参数
-> Tool 执行文件扫描、解析、OCR、预览、分类、检索、整理等动作
-> 系统记录文件版本、派生件、证据、分类、关系和 ChangeSet
-> Agent 输出逐文件、可追溯的回执
-> 高风险操作先展示 OperationPlan，用户确认后才执行
-> 用户纠正和成功轨迹进入反馈与 Skill 候选流程
```

RAG、全文检索和向量检索是事实检索工具，不是产品边界。Wiki、图谱、用户记忆和 Skill 演化是后续增强层。

## 3. 第一版目标

第一版必须实现一个可运行的文件工作智能体闭环：

```text
普通用户登录
-> 系统自动进入 default workspace
-> 用户进入 /chat
-> 用户上传文件并发送文件工作指令
-> LangGraph 创建 AgentRun
-> chat-intake 识别意图、附件和上下文
-> planning 选择 Skill 和 Tool
-> tool-dispatch 进行白名单校验和 schema 校验
-> 系统保存原件、版本和派生件
-> 系统解析文本和结构化内容
-> 系统切分 chunk、记录 evidence、生成 embedding
-> 系统生成基础多标签分类和逐文件 ChangeSet 回执
-> 用户可提出证据问题并获得带引用回答
-> 用户可提交反馈
-> 高风险操作先生成 OperationPlan
-> 用户确认后执行受控文件动作
-> admin/ops 审计反馈、重处理文件并配置模型
```

第一版必须包含：

- 登录、用户身份和 `user`、`ops`、`admin` 角色。
- 系统内置 default workspace。
- `/chat` 任务型 Agent 工作台。
- LangGraph Agent Runtime。
- AgentRun、Tool registry、Tool invocation log。
- StorageService，本地保存原件和派生件。
- 文件上传、列表、下载。
- PDF、DOCX、XLSX、TXT、MD、CSV 基础解析。
- 文档版本、Artifact、处理任务状态和处理事件。
- chunk 切分、evidence span、embedding 入库。
- PostgreSQL 全文检索 + pgvector 语义检索 + 混合重排。
- 基础多标签分类，分类必须有置信度、状态和证据。
- evidence-answer Skill，回答必须返回引用。
- ChangeSet / ChangeItem 结构化回执。
- OperationPlan / confirmation 高风险操作确认。
- 用户反馈。
- admin/ops 文件处理、反馈处理、重处理和 LLM 设置。

第一版不做：

- Neo4j 文件知识图谱完整落地。
- Graphiti 用户时间记忆完整落地。
- 自动 Skill 演化、灰度、回滚平台。
- 外部完整多智能体平台。
- 复杂 RBAC / ACL / OpenFGA。
- DingTalk 集成。
- OnlyOffice 在线编辑。
- 自动删除、自动覆盖原始文件。
- 默认调用互联网或公共第三方模型处理文件。

不做外部完整多智能体平台，不等于不做内部 Agent。内部 LangGraph Agent Runtime 是 MVP 核心。

## 4. 架构

### 4.1 第一版架构

```text
React / TypeScript 前端
  |
  | HTTP / SSE
  v
FastAPI API
  |
  |-- AuthService：登录、用户、JWT
  |-- WorkspaceService：内置 default workspace
  |-- ConversationService：会话、消息、附件
  |-- AgentRuntime：LangGraph graph、AgentRun、节点状态
  |-- ToolRegistry：白名单 Tool、schema 校验
  |-- StorageService：原始文件和派生文件存储
  |-- DocumentService：文件元数据、版本、状态
  |-- ParserService：Word / Excel / PDF / TXT 解析
  |-- ChunkService：切分、证据定位
  |-- EmbeddingService：生成向量
  |-- ClassificationService：多标签分类和证据校验
  |-- RetrievalService：全文 + 向量混合检索
  |-- EvidenceAnswerSkill：基于证据回答
  |-- ChangeSetService：逐文件回执和审计
  |-- OperationPlanService：高风险操作计划与确认
  |-- FeedbackService：问题反馈、审计、修复
  |-- JobService：处理任务状态和日志
  |
  v
PostgreSQL + pgvector
  |
  v
本地文件存储 storage/
```

### 4.2 第二阶段架构

```text
React 前端
  |
FastAPI API + LangGraph Agent Runtime
  |
Redis + Celery/RQ Worker
  |
PostgreSQL + pgvector
  |
MinIO / S3 / COS
```

第二阶段把 OCR、Office 预览、批量 embedding、批量分类、导出等耗时任务从 API 进程拆到 worker。

### 4.3 第三阶段架构

```text
FastAPI API + LangGraph
  |
  |-- PostgreSQL：会话、任务、ChangeSet、OperationPlan、Skill 治理
  |-- pgvector / Milvus：向量检索
  |-- Neo4j：文件事实图谱、实体关系、血缘关系
  |-- Graphiti：用户时间记忆
  |-- MinIO / S3 / COS：对象存储
  |-- Redis Worker：异步处理
```

Neo4j 和 Graphiti 是增强项。第一版要预留边界，但不强制完整接入。

Neo4j 图谱增强分类的整体架构和轻量第一版本实施方案已分别记录在
`docs/neo4j-graph-classification-overall-plan.md` 与
`docs/neo4j-graph-classification-v1-implementation-plan.md`。早期 Docling 组合设想继续保留在
`docs/docling-neo4j-classification-enhancement-plan.md`，如有冲突以两份新方案为准。

## 5. 推荐目录结构

```text
file-agent/
├─ apps/
│  ├─ api/
│  │  ├─ app/
│  │  │  ├─ main.py
│  │  │  ├─ core/
│  │  │  ├─ modules/
│  │  │  │  ├─ agent/
│  │  │  │  ├─ auth/
│  │  │  │  ├─ workspaces/
│  │  │  │  ├─ conversations/
│  │  │  │  ├─ documents/
│  │  │  │  ├─ storage/
│  │  │  │  ├─ parsing/
│  │  │  │  ├─ chunks/
│  │  │  │  ├─ embeddings/
│  │  │  │  ├─ retrieval/
│  │  │  │  ├─ classification/
│  │  │  │  ├─ operations/
│  │  │  │  ├─ changesets/
│  │  │  │  ├─ feedback/
│  │  │  │  ├─ admin/
│  │  │  │  └─ jobs/
│  │  │  └─ tests/
│  │  ├─ alembic/
│  │  ├─ pyproject.toml
│  │  └─ .env.example
│  └─ web/
│     ├─ src/
│     │  ├─ api/
│     │  ├─ pages/
│     │  ├─ components/
│     │  ├─ routes/
│     │  └─ types/
├─ storage/
│  ├─ quarantine/
│  ├─ originals/
│  ├─ derivatives/
│  ├─ exports/
│  └─ skill-artifacts/
├─ docs/
├─ rules/
├─ skills/
├─ docker-compose.yml
└─ README.md
```

## 6. LangGraph Agent Runtime

MVP graph：

```text
用户消息 + 附件
-> chat-intake node
-> planning node
-> tool-dispatch node
-> async-job node if needed
-> evidence/change node
-> response node
```

推荐 AgentRun 状态：

```text
RECEIVED
PLANNING
WAITING_FOR_CONFIRMATION
RUNNING_TOOL
WAITING_FOR_ASYNC_JOB
SUMMARIZING
COMPLETED
FAILED
NEEDS_REVIEW
```

实现规则：

- 图状态必须使用明确 schema。
- Tool 调用必须集中经过 tool-dispatch 节点。
- 任何副作用 Tool 都必须做白名单和参数 schema 校验。
- LangGraph checkpoint 第一版可轻量实现，但接口要保留。
- 第一版不做多智能体协作，但图不能写死成只回答问题的单一路径。

### 6.1 Planner 契约

Planner 输出的是声明式计划，不是执行结果。计划必须经过 tool-dispatch 校验后才能执行。

Planner 输出结构：

```text
intent
user_goal
slots
selected_skills
steps[]
  - step_id
  - skill
  - tool_name
  - input
  - requires_confirmation
  - risk_level
  - expected_outputs
  - writes
evidence_policy
confirmation_policy
```

Planner 规则：

- `tool_name` 必须存在于 Tool registry。
- `input` 必须通过 Tool schema 校验。
- 高风险步骤必须生成 OperationPlan，不得直接执行。
- Planner 不得输出 shell 命令、SQL 写语句或任意文件路径写入动作。
- Planner 测试必须使用 deterministic fake LLM。

### 6.2 MVP Tool Catalog

| Tool | 职责 | 副作用 | 是否需确认 |
|---|---|---:|---:|
| `document-register-upload` | 登记 Document / DocumentVersion | yes | no |
| `security-scan` | 文件安全扫描与 MIME 校验；MVP 可占位 | yes | no |
| `document-convert` | 用 Unstructured/Haystack/LlamaIndex/LangChain adapter 抽取文档文本和结构 | yes | no |
| `table-extract` | 用 Haystack/openpyxl adapter 读取 XLSX sheet、表头、单元格文本 | yes | no |
| `artifact-write` | 写入抽取文本、预览、OCR、导出等派生件记录 | yes | no |
| `chunk-build` | 生成 chunk 和 evidence_spans | yes | no |
| `embedding-generate` | 生成并保存 embedding | yes | no |
| `metadata-extract` | 提取年份、关键词、实体候选 | yes | no |
| `multi-label-classify` | 生成多标签分类、置信度、状态和证据 | yes | no |
| `hybrid-search` | 执行当前附件、会话、workspace 检索 | no | no |
| `evidence-answer` | 基于证据生成回答和引用 | yes | no |
| `change-report` | 生成逐文件回执数据 | yes | no |
| `operation-plan-create` | 为高风险操作生成 OperationPlan | yes | no |
| `confirmed-file-action` | 执行已确认文件动作 | yes | yes |
| `feedback-record` | 记录用户反馈 | yes | no |
| `job-status-read` | 查询任务状态和事件 | no | no |
| `document-lineage-read` | 查询版本、派生件和关系 | no | no |

有副作用的 Tool 必须记录 `tool_invocations`。产生分析结果、派生件或潜在文件变更的 Tool 必须写 ChangeSet / ChangeItem。

## 7. 核心数据模型

第一版 PostgreSQL 至少包含：

- users
- workspaces
- workspace_members
- conversations
- messages
- agent_runs
- tool_invocations
- documents
- document_versions
- artifacts
- document_pages
- document_chunks
- evidence_spans
- categories
- document_categories
- qa_answers
- answer_references
- operation_plans
- operation_confirmations
- change_sets
- change_items
- feedback
- processing_jobs
- processing_events
- llm_settings
- user_preferences

`qa_answers` 是 evidence-answer Skill 的结果表，不代表系统只有 QA 能力。

## 8. 文件处理流程

```text
用户上传文件并发送指令
  ↓
chat-intake：识别附件、当前会话、用户意图
  ↓
file-ingest：创建 Document / DocumentVersion
  ↓
quarantine：隔离区
  ↓
security-scan：第一版可占位，后续接 ClamAV
  ↓
document-convert：决定解析、OCR、预览路径并抽取文档内容
  ↓
document-convert / table-extract
  ↓
metadata-extract tool：年份、关键词、实体
  ↓
document-classification：多标签分类
  ↓
chunk + evidence + embedding
  ↓
ChangeSet
  ↓
聊天中输出逐文件回执
```

原件保护规则：

- 原始文件永远不被覆盖。
- 原件保存到 `storage/originals`。
- OCR、预览、缩略图、抽取文本、导出文件都是派生件。
- 每个派生件都必须可追溯到 DocumentVersion。
- 原件改名、移动、复制、导出、删除必须通过确认后的 OperationPlan。
- 含宏 Office 标记风险，不执行宏。

## 9. 分类、检索与证据回答

### 9.1 多标签分类

一个文件可以同时拥有多个分类。分类关系必须记录：

- relation_role
- confidence
- status
- taxonomy_version
- classifier_version
- evidence

低置信度分类必须进入 `SUGGESTED` 或 `NEEDS_REVIEW`，不能强行分类。

### 9.2 检索顺序

长期检索顺序：

```text
L0 当前消息附件
L1 当前对话中已提到、引用或打开的文件
L2 用户近期文件、收藏、显式偏好、常用别名
L3 用户历史行为相关文件
L4 全量文件知识图谱
L5 校内知识库
L6 外部互联网或外部信息（默认关闭）
```

MVP 至少实现 L0、L1、L4 的轻量版本。用户习惯只能调整排序，不能改写客观分类。

### 9.3 证据回答

证据回答规则：

- 只能基于检索到的证据回答。
- 证据不足时必须明确说明没有找到明确依据。
- 回答必须返回引用来源。
- 文件正文不能成为系统指令。
- 数字、日期、金额、表格汇总必须由确定性工具计算。

## 10. ChangeSet 与 OperationPlan

每个 Tool 执行后必须产生结构化结果。造成分析结果、派生件或潜在文件变更的操作必须写入 ChangeSet。

必须确认后执行：

```text
批量重命名
移动文件
复制文件
覆盖文件
删除文件
大批量导出
清空用户习惯
发送文件内容到外部服务
启用全局 Skill 新版本
```

确认前 OperationPlan 状态必须是 `PLANNED` 或 `WAITING_CONFIRMATION`，不得显示为 `COMPLETED`。

## 11. Skills

项目 Skill 清单单独维护在 `docs/skills-catalog.md`。

MVP 必须至少覆盖：

```text
chat-intake
file-ingest
document-classification
file-search
evidence-answer
change-report
operation-plan
confirmed-file-action
feedback-and-memory
```

每个 Skill 必须有 `skills/<skill-name>/SKILL.md`，声明触发条件、输入输出、Allowed Tools、Open Source Backing、执行步骤、证据规则、ChangeSet 规则、OperationPlan 规则和禁止事项。

## 12. 前端页面

MVP 页面：

```text
/login
/chat
/admin/documents
/admin/feedback
/admin/settings/llm
```

`/chat` 是任务型 Agent 工作台，至少包含：

- 聊天消息流。
- 文件拖拽或附件上传区。
- 附件队列。
- Agent 任务状态卡。
- 文件处理进度卡。
- 搜索结果卡。
- 文件分类卡。
- 逐文件明细列表。
- OperationPlan 确认卡。
- 引用和证据展示。
- 反馈入口。

不要把 `/chat` 做成只有输入框和答案列表的问答页面。

## 13. 开发顺序

```text
1. Repository, tooling, health check
2. Auth, roles, default workspace
3. Conversation message entry and upload attachment flow
4. Minimal LangGraph Agent Runtime
5. StorageService, Document, DocumentVersion, Artifact
6. Processing jobs and file parsing
7. ChangeSet and change-report receipt
8. Chunk, evidence, embedding, hybrid retrieval
9. Basic multi-label classification with evidence
10. Evidence-answer Skill
11. OperationPlan and confirmation flow
12. Feedback and admin processing
13. Admin LLM settings
14. Frontend chat task workspace
15. Admin pages
16. End-to-end intelligent file task verification
```

## 14. 第一版完成标准

第一版完成必须满足：

1. 普通 user 可以登录并打开 `/chat`。
2. `/chat` 是任务型 Agent 工作台。
3. 普通 user 可以发送文件工作指令并上传 PDF、DOCX、XLSX、TXT、MD、CSV。
4. 系统创建 AgentRun 并记录 LangGraph 状态。
5. Agent 通过白名单 Tool 执行文件处理。
6. 系统保存原件、版本和派生件。
7. 系统可以解析文件并创建 chunk、evidence、embedding。
8. 系统可以生成基础多标签分类、置信度和证据。
9. 每次处理产生 ChangeSet 或等价结构化回执。
10. 批量处理能逐文件展示结果、失败、待确认和证据。
11. 原始文件不会被解析、OCR、摘要、分类或 Skill 覆盖。
12. 普通 user 可以获得带引用的证据回答。
13. 高风险操作只生成 OperationPlan，确认后才执行。
14. 普通 user 可以提交反馈。
15. 普通 user 不能访问管理页面和 admin API。
16. admin/ops 可以查看文件、重处理文件、查看并处理反馈。
17. admin/ops 可以配置 LLM 和 embedding 设置。
18. 后端测试全部通过。
19. 前端构建成功。

达到以上标准后，再进入完整 Neo4j、Graphiti、MinIO/S3、Redis/Celery、OCR 增强、预览增强和 Skill 自动演化平台。
