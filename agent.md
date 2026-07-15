# File Agent Development Rules

本文件是本项目后续开发的最高级项目规范。任何 agent、AI 助手或开发者在修改代码前都必须先阅读并遵守本文件，以及本文件引用的开发文档。

## 1. 文档优先级

遇到冲突时按以下顺序判断：

1. 用户在当前对话中的明确指令。
2. 本文件 `agent.md`。
3. `/Users/zhouhexin/Downloads/conversational_file_agent_implementation_plan_v1.md`，作为产品定位和长期架构参考。
4. `docs/conversational-file-agent-development-blueprint.md`。
5. `docs/superpowers/plans/2026-06-24-file-agent-mvp-implementation-plan.md`。
6. `docs/api-contract.md`。
7. `docs/database-schema.md`。
8. `README.md`。

如果旧文档把项目表述为“问答系统”或“知识库问答”，必须按本文件解释为“文件智能体中的证据回答能力”，不能把它作为项目定位。

如果旧文档写“第一版不做智能体”，必须按本文件解释为“第一版不做外部完整多智能体平台和自动 Skill 演化”，但第一版仍必须保留内部 Agent Runtime、Tool 边界、OperationPlan、ChangeSet 和审计能力的架构位置。

## 2. 项目定位

File Agent 是面向学校/学工业务场景的对话式文件工作智能体，不是传统网盘，也不是只会问答的聊天机器人。

用户通过聊天框上传、读取、OCR、分类、检索、整理和处理文件。系统通过 Agent Runtime 理解用户意图、选择 Skill、生成受控 Tool 参数、调用白名单 Tool、校验证据、输出逐文件处理回执，并在高风险操作前生成待确认的 OperationPlan。

证据回答是 File Agent 的一个能力，不是产品边界。RAG、全文检索和向量检索是 Agent 的事实检索工具；最终目标是围绕文件工作的任务型智能体。

长期目标：

```text
聊天框作为唯一主入口
-> 用户上传文件或提出文件工作请求
-> Agent 识别意图、附件、上下文和用户习惯
-> Agent 选择 Skill 并生成受控 Tool 调用
-> Tool 执行文件扫描、解析、OCR、预览、分类、检索、整理等动作
-> 系统记录文件版本、派生件、证据、分类、关系和 ChangeSet
-> Agent 输出逐文件、可追溯的回执
-> 高风险操作先展示 OperationPlan，用户确认后才执行
-> 用户纠正和成功轨迹进入反馈与 Skill 候选流程
```

第一版可以采用较轻的实现，但架构和命名不得把系统锁死为 QA Service。

## 3. 核心原则

### 3.1 聊天框是主入口

普通用户不需要理解目录层级、分类语法、处理流水线或数据库结构。用户可以直接说：

```text
帮我读取并分类这批文件。
这是一批扫描件，帮我 OCR 并告诉我哪些页面不清楚。
找我去年活动相关的奖学金材料。
把刚上传的 Excel 所有工作表整理成摘要。
给这些文件生成标准化文件名建议，但先不要改。
找和这份国家励志奖学金申请表有关的证明材料。
```

系统必须把这些请求作为任务处理，而不是只当成问答。

### 3.2 LLM 只负责理解与编排

LLM 可以：

- 理解用户意图。
- 提取查询条件和任务参数。
- 选择 Skill。
- 生成受控 Tool 参数。
- 汇总经过验证的证据。
- 生成操作计划。
- 提出 Skill 改进候选。

LLM 不可以：

- 直接访问文件系统、Shell 或数据库写接口。
- 绕过 Tool 修改文件。
- 将文件正文、OCR 文本、网页文本视为系统指令。
- 直接启用或修改生产 Skill。
- 编造文件名、页码、表格单元格、分类、数字或结论。

### 3.3 Tool 白名单与 schema 校验

Agent 只能调用白名单 Tool。每个 Tool 必须有：

- 名称和职责。
- 输入 schema。
- 输出 schema。
- 可访问资源范围。
- 失败和降级策略。
- 是否会产生 ChangeSet。
- 是否需要用户确认。
- 安全禁止事项。

所有 Tool 参数必须经过 schema 校验。任何文件写入、数据库写入、外部服务调用或批量操作都不能由 LLM 直接执行。

### 3.4 每次执行必须有回执

每次 Agent 执行任务后，至少说明：

1. 处理对象与数量。
2. 已执行的动作。
3. 每个文件的处理状态。
4. 每个文件的分类、关键词、年份、实体和证据。
5. 生成的 OCR、预览、缩略图、抽取文本或导出文件。
6. 原始文件是否发生变化。
7. 待确认、低置信度、失败、跳过事项。
8. 可继续执行的下一步。

批量任务不能只返回统计，必须逐文件展示明细。

## 4. 技术路线

第一版可以使用相对轻量的技术栈，但必须保持向完整文件智能体演进的边界。

MVP 推荐实现：

- 前端：React + TypeScript。
- 后端：Python FastAPI。
- Agent 编排：从第一版开始使用 LangGraph，先实现最小状态图，后续逐步扩展节点和边。
- 数据库：PostgreSQL + pgvector。
- 文件存储：第一版本地 `storage/`，通过 StorageService 抽象。
- 异步任务：第一版 FastAPI BackgroundTasks，后续 Redis + Celery/RQ。
- 文档解析：PDF、DOCX 默认优先使用本地 Docling 生成结构化文档元素，并保留 `python-docx`、`PyMuPDF` 作为失败回退；TXT/MD/CSV 直接读取，XLSX 使用 `openpyxl`，旧版 `.doc` 通过 macOS `textutil` 或服务器 LibreOffice 转换后抽取正文。扫描件 OCR 默认仍由现有 PaddleOCR/LLM OCR 兜底负责，避免与 Docling 重复 OCR。
- 大模型与 embedding：OpenAI 兼容接口，默认外部联网和外部检索关闭。
- Python 包管理：使用用户当前已经配置好的 Python 环境；可以用 `pyproject.toml` 记录依赖和工具配置，但不得强制切换到 `uv`、Poetry、Conda 或新建虚拟环境，除非用户后续明确要求。
- 测试：pytest；LLM 和 embedding 测试必须使用 deterministic fake。

长期目标允许演进为：

- Next.js + React + TypeScript。
- FastAPI + Pydantic。
- LangGraph。
- Redis + Celery。
- MinIO/S3。
- Neo4j 文件知识图谱。
- Graphiti 用户时间记忆。
- Apache Tika、OCRmyPDF、LibreOffice Headless、ClamAV。
- OpenTelemetry + Prometheus + Grafana。

不要把项目改成 NestJS、Prisma、MySQL、COS 强绑定或外部 Agent 平台代理。

## 5. MVP 范围

### 5.1 MVP 必须包含

MVP 必须体现“智能体”而不只是“问答”：

- 登录、JWT、用户身份和 `user`、`ops`、`admin` 角色。
- 系统内置 `default workspace`，普通用户不手动创建项目。
- 对话主入口，支持用户发送任务指令和上传附件。
- 最小 Agent Runtime：`AgentRun`、LangGraph 状态图、Tool 白名单、Tool 调用记录、错误处理。
- 最小 Skill 边界：chat-intake、file-ingest、document-classification、file-search、evidence-answer、change-report、operation-plan、confirmed-file-action、feedback-and-memory。
- 本地 StorageService，保存原件和派生件。
- 文件上传、列表、下载。
- PDF、DOC、DOCX、XLSX、TXT、MD、CSV 的基础解析。
- 文档版本、处理任务状态和处理事件。
- chunk 切分、evidence span、embedding 入库。
- PostgreSQL 全文检索 + pgvector 语义检索 + 混合重排。
- 基于证据的回答能力。
- 多标签分类的基础实现或可审计占位：一个文件允许多个分类，每个分类必须有置信度、状态和证据。
- ChangeSet 和 ChangeItem：记录 Tool 执行造成的分析结果、派生件和潜在文件变更。
- OperationPlan：改名、移动、覆盖、删除、批量导出、外部服务发送等高风险操作必须先生成计划，确认后才执行。
- 用户反馈。
- admin/ops 的文件处理、反馈处理、重处理和 LLM 设置页面。

### 5.2 MVP 可以轻量化

以下能力可以先用轻量实现，但接口和数据模型要为后续演进留出边界：

- Agent Runtime 必须从第一版开始使用 LangGraph。MVP 可以只实现最小图，不必一开始做复杂多分支、多智能体或自动恢复流程。
- 文件图谱可以先用 PostgreSQL 表表达，不必第一天接 Neo4j。
- 用户记忆可以先只支持显式偏好，不必第一天接 Graphiti。
- Skill 可以先以代码模块和规则文档实现，不必第一天做自动演化平台。
- OCR、预览和缩略图可以作为第二阶段，但原件与派生件模型第一版就要存在。
- 多标签分类可以先做规则/关键词/LLM 校验的混合实现，不必第一天做到完整分类平台。

### 5.3 MVP 不做

MVP 不做以下能力：

- 复杂 RBAC、ACL、OpenFGA。
- 自动删除、自动覆盖原始文件。
- 自动发布新的正式分类目录。
- 自动启用新的全局 Skill。
- 默认调用互联网或公共第三方模型处理文件。
- 外部完整多智能体平台。
- OnlyOffice 在线编辑。
- DingTalk 集成。

“不做外部完整多智能体平台”不等于“不做 Agent”。内部 Agent Runtime、Tool 调用、OperationPlan 和 ChangeSet 是 MVP 架构的一部分。

## 6. 推荐目录结构

按以下结构组织项目：

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
│     └─ package.json
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

`rules/` 和 `skills/` 可以先放文档和 schema，不必第一版就做自动演化。

## 7. Agent Runtime 规则

MVP 必须用 LangGraph 实现一个受控 Agent 运行模型：

```text
用户消息 + 附件
-> LangGraph run starts
-> chat-intake node
-> planning node: intent + slots + selected skills
-> tool-dispatch node: Tool plan + schema validation
-> async-job node if needed
-> evidence/change node: ChangeSet / OperationPlan / evidence result
-> response node: receipt
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

Agent 运行记录至少保存：

- `agent_run_id`
- `conversation_id`
- `user_id`
- `intent`
- `status`
- `selected_skills`
- `tool_invocations`
- `error_message`
- `created_at`
- `updated_at`

Tool 调用记录至少保存：

- `tool_name`
- `input_json`
- `output_json`
- `status`
- `started_at`
- `finished_at`
- `changeset_id`
- `operation_plan_id`

LangGraph 实现规则：

- 图状态必须使用明确 schema，不允许把任意 LLM 输出直接透传给 Tool。
- Agent Runtime 必须明确区分三层数据边界：
  - `AgentGraphState`：可持久化业务状态，只保存本次任务的输入、附件引用、上下文摘要、planner_mode、tool_plan、执行结果、错误、业务对象 ID 和最终回复。
  - `AgentRuntimeContext`：运行时依赖，只保存 Planner、Tool Registry、Context Loader、LLM Intent Service，以及后续 Storage、Queue、DB Factory、Settings 等服务对象。
  - `Persistent Stores`：数据库、对象存储、向量库、图数据库等长期事实存储，保存文件、解析结果、证据、分类、ChangeSet、OperationPlan 和审计记录。
- `document_results` 属于 `AgentGraphState` 中的逐文件运行结果容器，只能保存本次 AgentRun 的轻量摘要、状态、分类建议、证据摘要、警告和错误；正式长期事实仍应进入 `Persistent Stores`，不能用 State 快照替代 `document_pages`、`document_categories`、ChangeSet 或 Evidence 表。
- `result_summary` 属于 `AgentGraphState` 中的通用结果聚合容器，由 `evidence_or_change` 节点一次性从 Tool 输出中整理生成，用于保存表格分析、文件读取、分类读取、能力清单、分类目录、受管目录文件列表和普通对话摘要等响应所需的结构化业务结果；`response` 节点必须消费 `result_summary`，不得在每个响应分支里重复扫描原始 `tool_results`。
- 受管目录文件被读取时，必须先创建或复用当前用户的不可变内容快照。`managed_file_snapshots` 以 `user_id + managed_file_id + source_sha256` 标识内容版本，属于 `Persistent Stores`；内容未变化时必须复用既有 Document、FileObject、ExtractionRun 和 DocumentPage，内容变化时创建新版本并保留旧版本供历史对话和 ChangeSet 复核。
- `AgentGraphState.document_results` 只能保存受管文件的 `managed_file_id`、`root_key`、逻辑相对路径、快照状态和内容哈希等轻量摘要，不得保存 `ManagedRoot.container_path`、宿主机绝对路径或文件正文。批量受管文件处理中单个文件失败不得回滚其他文件，必须返回逐文件结果并用 `PARTIAL` 表达部分成功。
- `planner`、`registry`、`context_loader`、`llm_intent_service`、数据库 Session、LLM client、API key、HTTP client 等运行对象不得写入 `AgentGraphState`、checkpoint 或 `graph_state_json`。
- LangGraph 节点需要运行依赖时，必须通过 `AgentRuntimeContext` 或等价的运行时上下文机制获取，不能把服务对象塞进 State。
- 会话附件范围必须先由后端 `ConversationAttachmentContextService` 或等价服务解析成确定的 `document_ids`，并标记 `uploaded` / `inferred_context` 与真实上传 `batch_id`；Planner、LLM 和 Graph 节点不得自行猜测“刚刚上传”“上面文件”“第二个文件”对应的文件集合。
- 绑定用户、数据库会话或请求上下文的运行依赖必须通过 factory 在每次 AgentRun 中重新构造；尤其是 Tool Registry 不得作为长期单例复用，避免旧 `user_id` 或旧数据库会话泄漏到新请求。
- 节点职责必须单一，例如 intake、planning、tool dispatch、async wait、evidence validation、response receipt。
- Tool 调用必须集中经过 tool-dispatch 节点，不能让各节点绕过白名单直接执行副作用。
- LangGraph checkpoint 可以第一版先用轻量实现，但接口要保留，后续可接数据库持久化和任务恢复。
- 第一版不要求多智能体协作，但不得把图写成只能回答问题的单一路径。

### 7.1 Planner 输出契约

`planning` 节点只能输出声明式计划，不能直接执行副作用。计划必须经过 `tool-dispatch` 节点校验后才能调用 Tool。

Planner 输出至少包含：

```json
{
  "intent": "CLASSIFY_FILES",
  "user_goal": "读取并分类刚上传的文件",
  "slots": {
    "document_ids": ["document-uuid"],
    "requested_outputs": ["classification", "receipt"]
  },
  "selected_skills": ["chat-intake", "file-ingest", "document-classification", "change-report"],
  "steps": [
    {
      "step_id": "step-1",
      "skill": "file-ingest",
      "tool_name": "document-convert",
      "input": {
        "document_id": "document-uuid"
      },
      "requires_confirmation": false,
      "risk_level": "low",
      "expected_outputs": ["pages", "metadata", "artifacts"],
      "writes": ["document_pages", "artifacts", "change_items"]
    }
  ],
  "evidence_policy": {
    "require_page_or_cell": true,
    "allow_no_evidence_answer": false
  },
  "confirmation_policy": {
    "operation_plan_required": false
  }
}
```

Planner 规则：

- `intent` 必须来自应用层枚举，例如 `INGEST_FILES`、`CLASSIFY_FILES`、`SEARCH_FILES`、`EVIDENCE_ANSWER`、`SUGGEST_RENAME`、`CONFIRMED_OPERATION`。
- `tool_name` 必须存在于 Tool registry。
- `input` 必须通过该 Tool 的 schema 校验。
- `requires_confirmation = true` 的步骤不得直接执行，必须转为 OperationPlan。
- 计划里的 `writes` 只能声明预期写入对象，不能绕过 Tool 直接写数据库。
- Planner 不得生成 shell 命令、SQL 写语句、文件路径写入动作或外部请求。
- Planner 测试必须使用 deterministic fake LLM。

### 7.2 MVP Tool Catalog

MVP Tool 必须明确命名、输入 schema、输出 schema、副作用和确认要求。第一版至少实现或占位以下 Tool：

| Tool | 职责 | 副作用 | 是否需确认 |
|---|---|---:|---:|
| `document-register-upload` | 将已上传文件登记为 Document / DocumentVersion | yes | no |
| `security-scan` | 文件安全扫描与 MIME 校验；MVP 可用占位实现 | yes | no |
| `document-convert` | 用 Unstructured/Haystack/LlamaIndex/LangChain adapter 抽取文档文本和结构 | yes | no |
| `table-extract` | 用 Haystack/openpyxl adapter 读取 XLSX sheet、表头、单元格文本 | yes | no |
| `artifact-write` | 写入抽取文本、预览、OCR、导出等派生件记录 | yes | no |
| `chunk-build` | 基于页面或 sheet 生成 chunk 和 evidence_spans | yes | no |
| `embedding-generate` | 调用 embedding 服务并写入向量 | yes | no |
| `metadata-extract` | 提取年份、关键词、实体候选 | yes | no |
| `multi-label-classify` | 生成多标签分类、置信度、状态和证据 | yes | no |
| `hybrid-search` | 执行当前附件、当前会话、workspace 范围检索 | no | no |
| `evidence-answer` | 基于证据生成回答和引用 | yes | no |
| `change-report` | 聚合 ChangeSet，生成逐文件回执数据 | yes | no |
| `operation-plan-create` | 为高风险操作生成 OperationPlan | yes | no |
| `confirmed-file-action` | 执行已确认的改名、移动、复制、导出等动作 | yes | yes |
| `feedback-record` | 记录用户反馈 | yes | no |
| `job-status-read` | 查询处理任务状态和事件 | no | no |
| `document-lineage-read` | 查询 DocumentVersion、Artifact 和关系 | no | no |

Tool 输出规则：

- 有副作用的 Tool 必须写 `tool_invocations`。
- 造成分析结果、派生件或潜在文件变更的 Tool 必须写 ChangeSet / ChangeItem。
- 高风险 Tool 只能由已确认的 OperationPlan 驱动。
- Tool 业务输出 `ok=false` 或 `status=FAILED` 时，`tool_invocations.status` 必须记录为 `FAILED`，不能为了表示 handler 正常返回结构化结果而误记为 `COMPLETED`。
- Tool 返回给 LLM 的内容必须是结构化摘要，不得泄漏密钥、本地绝对路径或未授权文件内容。

## 8. 数据库规则

数据库实现必须兼容 `docs/database-schema.md`，但需要扩展以支持智能体能力。

基础规则：

- 主键统一 UUID。
- 时间字段统一 `timestamptz`。
- JSON 字段使用 `jsonb`。
- 默认 embedding 维度为 `vector(1536)`；如果模型维度不是 1536，必须在首次迁移前同步调整。
- 数据库 MVP 阶段不做行级安全策略，访问控制由后端服务层实现。

除原文档表结构外，MVP 至少应补充或预留：

- `agent_runs`
- `tool_invocations`
- `operation_plans`
- `operation_confirmations`
- `change_sets`
- `change_items`
- `artifacts`
- `categories`
- `document_categories`
- `user_preferences` 或等价显式记忆表

如果为了阶段化实施暂不创建全部表，必须在设计文档或迁移说明中明确替代方案和后续迁移路径。

ChangeSet 和 ChangeItem 是智能体审计核心，不能被简单日志替代：

```text
change_sets
- id
- conversation_id
- user_id
- operation_type
- status
- created_at
- completed_at

change_items
- id
- changeset_id
- target_document_id
- change_type
- before_value_json
- after_value_json
- source
- confidence
- evidence_json
- execution_status
- created_at
```

当前阶段读取、解析和读取并分类链路必须生成真实 ChangeSet。`TEXT_EXTRACTED`、`DOCUMENT_PAGES_CREATED`、`CATEGORY_SUGGESTED` 和 `DOCUMENT_PROCESSING_FAILED` 写入 `change_items`；默认复用既有成功解析结果时必须写入 `TEXT_REUSED`、`DOCUMENT_PAGES_REUSED` 和 `CATEGORY_SUGGESTION_REUSED`。其中 `CATEGORY_SUGGESTED` 和 `CATEGORY_SUGGESTION_REUSED` 都只代表分类建议，不代表正式写入 `document_categories`。

用户只说“分类/归类/整理”且带附件时，也必须走 `extract-document-text -> document_pages -> document-classification -> ChangeSet` 真实链路，不得回落到 `document-convert -> metadata-extract -> multi-label-classify -> change-report` 占位链路。分类依据必须来自 `document_pages.text_content` 的完整正文，不能用短 `text_preview` 代替。

运行日志是诊断辅助，不能替代 AgentRun、ToolInvocation、ChangeSet 或数据库审计。后端必须保留轻量结构化文件日志：

- 每个请求必须有 `request_id`，并通过 `X-Request-ID` 响应头返回；如果请求自带 `X-Request-ID`，优先沿用。
- 每个 AgentRun 日志必须带 `agent_run_id`；能取得上下文时必须同时带 `user_id`、`conversation_id`。
- Tool、文件和分类相关日志必须尽量带 `tool_name`、`document_id`、`status`、`duration_ms`、`error_code`。
- 日志必须写入服务器本地文件，默认 `LOG_DIR=./logs`，按天生成 `file-agent-YYYY-MM-DD.log`。
- 日志默认保留 7 天，启动时必须清理超过 `LOG_RETENTION_DAYS` 的历史日志文件。
- 日志内容必须是 JSONL；不得写入文件正文、OCR 全文、API key、JWT、密码或大段 LLM prompt。
- 至少记录四类事件：API 请求与异常、Agent 节点进入/退出/耗时、Tool 调用输入摘要/结果状态/耗时、文件解析/OCR/分类/ChangeSet 的成功与失败原因。

`change_type` 至少覆盖：

```text
TEXT_EXTRACTED
OCR_ARTIFACT_CREATED
PREVIEW_CREATED
KEYWORD_ADDED
YEAR_ADDED
ENTITY_ADDED
CATEGORY_ADDED
CATEGORY_REMOVED
RELATION_ADDED
RELATION_REMOVED
FILENAME_CHANGED
FILE_MOVED
FILE_COPIED
FILE_DELETED
EXPORT_CREATED
MEMORY_ADDED
MEMORY_REMOVED
SKILL_CANDIDATE_CREATED
```

## 9. API 规则

现有 `docs/api-contract.md` 可作为基础，但接口设计必须支持“发消息给 Agent”而不是只支持 QA。

MVP 必须提供等价能力：

```text
POST /api/conversations
POST /api/conversations/{conversation_id}/messages
POST /api/conversations/{conversation_id}/documents/upload
GET  /api/agent-runs/{agent_run_id}
GET  /api/jobs/{job_id}
GET  /api/jobs/{job_id}/events
GET  /api/documents/{document_id}
GET  /api/documents/{document_id}/download
GET  /api/documents/{document_id}/chunks
GET  /api/documents/{document_id}/lineage
POST /api/search
POST /api/operations/plans
POST /api/operations/plans/{plan_id}/confirm
GET  /api/changesets/{changeset_id}
POST /api/feedback
```

如果保留 `POST /api/conversations/{conversation_id}/qa`，它只能作为 `evidence-answer` 的兼容接口，不能成为主入口。新功能应优先围绕消息、AgentRun、Tool、ChangeSet 和 OperationPlan 设计。

错误统一返回：

```json
{
  "error": {
    "code": "BAD_REQUEST",
    "message": "Invalid request"
  }
}
```

权限边界：

- `user` 可以在聊天入口上传文件、发起任务、查看自己的处理回执、确认自己的操作计划、提交反馈。
- `user` 不能访问 admin 接口，不能处理反馈，不能修改 LLM 设置，不能绕过确认执行高风险操作。
- `ops` 和 `admin` 可以查看文件处理状态、触发重处理、处理反馈、配置模型。

## 10. 文件处理与原件保护

原件保护是不可削弱规则：

- 上传文件先进入 `storage/quarantine/` 或等价隔离区。
- 安全扫描和 MIME 检测通过后进入 `storage/originals/`。
- 原始文件永远不被 OCR、预览、摘要、分类或 Skill 覆盖。
- Docling 等结构化解析器生成的标题、章节、正文、页眉页脚、页码和位置元素必须持久化到 `document_elements`，并通过 `extraction_run_id` 关联 `document_pages`；结构化元素属于 Persistent Stores，不得写入 `AgentGraphState`。解析复用必须同时校验文件内容版本、解析器版本和配置指纹，Docling 不可用或结果为空时必须回退现有解析器。
- OCR、预览、缩略图、抽取文本、导出结果都是派生件。
- 每个派生件都必须可追溯到 `DocumentVersion`。
- 原件改名、移动、复制、导出、删除必须通过确认后的 OperationPlan。
- 含宏 Office 标记风险，不执行宏。
- 加密文件标记需人工处理或提供密码，不尝试破解。

文件处理状态建议：

```text
RECEIVED
QUARANTINED
SCANNING
ROUTED
PARSING
OCR_PROCESSING
EXTRACTING_METADATA
CLASSIFYING
BUILDING_INDEX
READY
NEEDS_REVIEW
FAILED
```

第一版不接安全扫描工具时，仍要保留状态和抽象边界。

## 11. 分类、证据与检索

### 11.1 多标签分类

一个文件可以同时有多个分类，不能只保留最高分。

当前阶段分类目录可以先来自项目内配置文件，例如 `apps/api/app/modules/classification/taxonomies/school_file_classification.json`。配置文件是分类目录的 source of truth；不得为了预置静态分类目录提前强制落库，除非已经有后台分类管理、版本启停和审计需求。

分类目录必须采用 taxonomy v2 结构：分类节点保留 `name` / `children` 向后兼容，同时新增稳定 `id`、`description`、`aliases`、`positive_signals`、`negative_signals` 和 `examples`。`id` 用作后续候选召回、反馈和正式分类关系的稳定标识，不得使用显示名称作为长期外键；修改分类定义时必须递增 `taxonomy_version`。

分类匹配器的职责是候选召回，不是最终语义裁决。`recall_category_candidates` 必须基于文件名、标题、全文、别名、正向信号和负向信号生成 Top N 候选，并返回 `category_id`、`category_path`、`rule_score`、`matched_signals`、`negative_signals` 和 `candidate_reason`。`match_document_text` 仅作为 rule-only 兼容入口，把候选转换为 `SUGGESTED` 分类建议。默认情况下，LLM 只能从候选集合内选择分类，不得编造分类路径。

路径、目录名、文件名、扩展名和文件元数据只能作为候选召回、弱信号和初始命名字段，不能单独作为最终分类依据。最终分类必须通过受控解析后的正文、OCR 文本、PDF 页文本、Word 段落、Excel 工作表/单元格、压缩包内子文件清单等内容证据确认；如果文件名与正文证据冲突，以正文和可定位证据为准。对 `通知`、`工作安排`、`审批表`、`会议纪要`、`日报表`、`制度汇编`、压缩包和扫描件等泛化文件名，必须读取内容后再确认业务主题、文种、日期、涉及单位和重命名建议。

如业务上确实需要允许 LLM 自由生成分类路径，必须通过 `LLM_CLASSIFICATION_ALLOW_FREE_PATHS=true` 显式开启，并且自由路径只能保存为 `source=llm_free_path`、`status=NEEDS_REVIEW` 的待复核建议。自由路径不得自动写入正式 taxonomy，不得自动写入正式 `document_categories`，也不得覆盖人工确认结果；只有经过人工评审和 taxonomy v2 配置更新后，才能成为稳定分类节点。

全文分类必须通过 `DocumentClassificationService` 执行。Graph 只传 `document_id`、`extraction_run_id`、文件名和必要 fallback 摘要，不得在 `AgentGraphState` 保存全文，也不得由 Graph 直接读取 `DocumentPage` 或直接调用底层 matcher。`DocumentClassificationService` 属于 `AgentRuntimeContext` 的运行时依赖，负责从 `document_pages.text_content` 读取完整正文并返回结构化分类建议。

本次 AgentRun 的逐文件分类摘要继续写入 `document_results`，用于生成回执和保存运行快照。结构化分类建议必须同步写入 `document_classification_runs` 和 `document_category_suggestions`，状态为 `SUGGESTED`，并记录 `taxonomy_key`、`taxonomy_version`、`confidence`、`source`、`rank` 和结构化证据。`document_category_feedback` 用于后续保存用户接受、拒绝或修正意见。

分类证据必须优先使用 `evidence_items`，每项至少包含 `type=text_quote`、`page_number`、`sheet_name`、`quote`、`signals` 和 `source`。`evidence` 可以保留为兼容旧 UI 的关键词摘要，但不能替代可定位证据。非“其他”分类如果无法在原文中定位 quote，必须降级为 `NEEDS_REVIEW`，不得伪造页码或证据。

当前阶段的分类建议可以作为 `SUGGESTED` 结果展示在逐文件回执中；每个文件允许展示多个分类、置信度和证据，但这些建议不等同于用户确认后的正式分类关系。未来用户确认后的正式文件分类关系再写入 `document_categories`。

分类维度至少区分：

- 业务分类。
- 文档类型。
- 关键词。
- 年份。
- 实体。

每个分类关系必须记录：

- `confidence`
- `relation_role`
- `status`
- `taxonomy_version`
- `classifier_version`
- `evidence`

建议枚举：

```text
relation_role:
PRIMARY
SECONDARY
RELATED
DOCUMENT_TYPE

status:
AUTO_APPLIED
SUGGESTED
CONFIRMED
REJECTED
```

低置信度分类必须进入 `SUGGESTED` 或 `NEEDS_REVIEW`，不能强行分类。

### 11.1.1 Neo4j 图谱增强分类边界

Neo4j 图谱分类采用“可重建投影 + 只读候选增强”模式：

- PostgreSQL、taxonomy v2 配置和受管目录扫描结果仍是业务事实源，Neo4j 不得反向覆盖这些数据。
- `DocumentClassificationService` 继续作为唯一分类入口；Planner、Graph 节点和 LLM 不得直接访问 Neo4j。
- Neo4j Driver、Repository、Retriever 和 GraphRAG Adapter 属于运行时依赖，不得进入 `AgentGraphState`、checkpoint 或 `graph_state_json`。
- 图谱只接收稳定业务 ID、分类候选和关系元数据，不得写入文件全文、OCR 全文、密钥或服务器绝对路径。
- `SUGGESTED` 分类不得成为可信传播来源；只有人工确认、人工修正后的结构化分类，或明确标记为已分好类目录中的受控弱样本可以参与图谱支持。
- 图谱候选不能绕过 taxonomy 白名单、正文证据和用户确认；图谱与正文冲突时必须进入 `NEEDS_REVIEW`。
- 所有 Cypher 必须来自后端固定参数化模板，禁止 LLM 生成 Cypher 写语句，禁止普通用户使用 Text2Cypher。
- 图谱关闭、未安装依赖、连接失败或查询超时时，上传、解析、OCR 和基础分类必须无损降级。
- `neo4j-graphrag-python` 必须封装在项目 Adapter 后面；第一版本不使用实验性自动构图覆盖现有解析和分类链路。
- 第二版本向量分类必须读取 `document_pages.text_content` 完整正文，在运行时分块、归一化和聚合；正文和向量不得进入 `AgentGraphState`、日志或前端响应。
- 相同 SHA-256、Embedding 模型、版本和维度全部一致时可以复用 Neo4j 派生向量；模型或版本变化必须重算。
- 受管目录必须通过版本化 Profile 区分 `DEPARTMENT`、`CATEGORY`、`YEAR`、`COLLECTION`、`TEMPORARY` 和 `UNKNOWN`；`PATH_AS_WEAK_LABEL` 只能生成 `PATH_SUGGESTS`，不得生成 `CONFIRMED_AS`。
- `PATH_AS_CATEGORY` 只表示经 Profile 审核的目录路径可以成为全局分类候选来源，不表示目录内文件已确认属于该分类；`LOCATED_IN` 和父目录信息始终只能作为弱位置证据。
- 所有分类来源根必须共同形成全局候选集，新上传文件和任意受管目录文件都必须使用同一候选空间，不能按文件当前所在根或父目录裁剪候选。
- 配置了 `PATH_AS_CATEGORY` 分类来源根时，受管目录全局候选集是业务分类目录来源；不得静默混入或回退到另一套预置业务分类，目录为空或 Profile 无有效分类时必须进入 `NEEDS_REVIEW`。
- 文件与分类是多对多逻辑关系，一个文件必须允许同时保存和展示多个不同分支的 `SUGGESTED_AS` 或 `CONFIRMED_AS`；物理目录关系与逻辑分类关系必须分离。
- 只有用户明确接受或更正后的分类才能投影为 `CONFIRMED_AS`，目录位置、未反馈建议和普通弱标签均不得自动提升为确认分类。
- 图谱分类运行模式必须区分 `off`、`shadow` 和 `enabled`；`shadow` 只记录候选差异，不能改变用户结果。
- 无人工标注样本时允许小范围上线图谱增强建议，但结果只能是 `SUGGESTED` 或 `NEEDS_REVIEW`，不得自动写入正式 `document_categories`。
- 分类反馈必须来自用户明确的接受、拒绝或更正；未反馈、打开、下载或继续对话均不能推断为正样本。
- 接受产生目标正样本，拒绝产生原分类负样本，更正同时产生原分类负样本和目标分类正样本；反馈必须关联 taxonomy、分类器和 Embedding 版本，并采用可追踪的追加记录。
- 用户反馈不得直接修改 ACTIVE taxonomy、线上权重或可信图谱关系。候选配置必须先经过冻结反馈集的离线回放、人工批准和可回滚发布。

整体方案和第一版本实施依据分别为：

- `docs/neo4j-graph-classification-overall-plan.md`
- `docs/neo4j-graph-classification-v1-implementation-plan.md`
- `docs/neo4j-graph-classification-v2-implementation-plan.md`
- `docs/managed-file-global-multi-label-classification-plan.md`

### 11.2 证据规则

关键结论必须能定位到：

- 文件名或元数据。
- 页码。
- 文字段落。
- Excel 工作表与单元格。
- 分类证据。
- 图谱关系路径或关系来源。

没有足够证据时必须明确说明没有找到明确依据。

数字、日期、金额、表格汇总必须由确定性工具计算，不能让 LLM 心算或猜测。

### 11.3 检索顺序

长期检索顺序按外部方案设计：

```text
L0 当前消息附件
L1 当前对话中已提到、引用或打开的文件
L2 用户近期文件、收藏、显式偏好、常用别名
L3 用户历史行为相关文件
L4 全量文件知识图谱
L5 校内知识库
L6 外部互联网或外部信息（默认关闭）
```

MVP 可以先实现 L0、L1、L4 的轻量版本，但接口和排序逻辑不能阻碍后续加入用户记忆和图谱。

用户习惯只能扩展查询与调整排序，不得修改文件客观分类，不得排除全局高相关文件。

## 12. OperationPlan 规则

可以直接执行：

```text
上传
文件解析
OCR
预览生成
关键词和实体提取
多标签分类建议
相似文件识别
文件检索
摘要生成
改名建议生成
```

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

OperationPlan 必须展示：

- 计划处理对象数量。
- 每个对象的 before/after。
- 风险说明。
- 信息缺失或疑似重复项。
- 当前是否已执行。
- 用户确认方式。

当前阶段 OperationPlan 已支持受管目录文件的 Native/F2 重命名闭环：只有部署配置显式允许重命名的受管根目录，才能生成包含正文提取证据的 `RENAME_FILES` 计划；普通用户可以确认自己创建的计划，确认后按统一批次执行器协议执行并写入 `FILENAME_CHANGED` 或失败 ChangeItem。默认执行器必须是 `native`。自动建议发生目标名称冲突时，基础名称视为第一版，后续名称必须在扩展名前依次追加 `_第二版`、`_第三版` 等中文版本后缀；文件系统、`managed_files` 索引和同一批次待执行目标都必须参与冲突判断，仍不得覆盖既有文件。F2 只能作为固定版本、离线部署的受控 Adapter，必须先 dry-run 并与 OperationPlan 完全比对，禁止由 F2 自行修复冲突、覆盖、处理隐藏文件、目录改名、跨目录移动和全局 undo；版本后缀必须由后端在 OperationPlan 创建前确定。F2 不参与正文理解、年份/文号/标题提取或目标名称生成。执行器对象属于 `AgentRuntimeContext`/请求级运行依赖，不得进入 `AgentGraphState`。自动提取为 `NEEDS_REVIEW` 的项目必须跳过原批次，并持久化为当前用户、当前会话的待复核项。用户按“文件原文件名更正为新文件名”明确提供名称时，该消息可视为本次更正的执行确认，但后端仍必须先创建 `RENAME_FILES` OperationPlan 和 OperationConfirmation，再调用确认执行 Tool 并写 ChangeSet；不得直接绕过计划修改文件。用户手工更正时，原文件名匹配多个待复核项或手工目标名称重复必须列出候选或冲突提示，冲突项不得阻塞同一消息中其他唯一文件。用户回复“不需要”时只关闭待复核项，不修改文件。

当前阶段还支持上传附件的临时存储重命名：Planner 必须保留后端已经解析的明确 `document_ids`，`generate-rename-suggestions` 读取 `document_pages` 后生成 `RENAME_UPLOADED_FILES` OperationPlan；用户确认后，受控执行器只改变该 Document 的逻辑文件名和私有临时存储 basename，并写入 `FILENAME_CHANGED`、ToolInvocation、ChangeSet 和逐文件结果。此链路当前不生成分类建议、不选择受管目录、不执行正式归档；目标路径必须由后端固定为 Document 私有临时目录，不能接受 LLM 或计划中的任意路径。底层 FileObject 被多个 Document 或受管快照共享时必须写时复制，不能影响其他引用。Document 级幂等复用只允许同一用户、同一工作区、同一文件名、同一内容且仍为 `UPLOADED` 的草稿；已进入消息或来自受管快照的 Document 不能作为新上传结果复用，内容去重只能复用 FileObject。移动、删除和覆盖仍不得真实执行；没有白名单执行器的 OperationPlan 确认必须失败并保持待确认，不得伪造 `EXECUTED` 或文件变更 ChangeSet。

旧版 `.xls` 必须优先使用 `xlrd>=2.0.1` 直接只读解析，失败后才允许尝试可选 LibreOffice 转换；当前部署不强制安装 LibreOffice，后续出现实际兼容性缺口时再启用。表格正文解析失败时，只允许 `.xls/.xlsx/.xlsm/.csv/.tsv` 使用高结构化文件名回退生成重命名建议；解析失败仍必须落库，文件名回退只能用于命名，不得作为正文、分类或事实证据。

## 13. Skills 与 Rules

`skills/` 目录用于表达可复用的智能体能力。MVP 可以先作为文档和模块边界，后续再演进成自动评测和发布系统。

项目 Skill 清单以 `docs/skills-catalog.md` 为准。MVP 必须至少覆盖其中的 MVP Skills，并为每个 Skill 创建 `skills/<skill-name>/SKILL.md` 规则骨架。

每个 Skill 的 `SKILL.md` 必须包含：

- 触发条件。
- 输入输出 schema。
- 可调用 Tool 白名单。
- Open Source Backing：标注该 Skill 是否直接或间接使用开源 Tool Adapter，并写明项目地址；不使用时写明自研。
- 处理步骤。
- 失败与降级策略。
- 验收标准。
- 禁止事项。
- 对应 Rules。

自动学习只能创建候选 Skill，不能直接覆盖 ACTIVE Skill。候选 Skill 必须通过结构、安全、回归、灰度评测后才能启用，并且必须可回滚。

## 14. 前端规则

MVP 的普通用户入口是聊天页，但聊天页必须是任务工作台，不是单纯问答页面。

普通用户页面：

```text
/login
/chat
```

管理页面：

```text
/admin/documents
/admin/feedback
/admin/settings/llm
```

`/chat` 至少应包含：

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

后续可增加：

- 文件预览抽屉。
- 图谱详情抽屉。
- “为什么推荐”折叠区。
- 我的记忆侧栏。

不要把 `/chat` 设计成只有输入框和答案列表的 QA 界面。

## 15. 开发顺序

按方案 B，开发顺序调整为：

```text
1. Repository, tooling, health check
2. Auth, roles, default workspace
3. Conversation message entry and upload attachment flow
4. Minimal LangGraph Agent Runtime: AgentRun, graph state, Tool registry, tool invocation log
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

不要先做完整 Neo4j、Graphiti 或 Skill 自动演化平台。但从第一版开始，代码结构必须允许这些能力进入，而不是围绕 QA Service 写死。

## 16. 代码注释规则

后续所有新增或修改的代码都必须加中文注释。注释要求：

- 每个模块必须有中文文件级 docstring，说明该模块在 File Agent 架构中的职责。
- 每个公开类、公开函数、API 路由、LangGraph 节点、Tool handler、Service 方法必须有中文 docstring 或紧邻中文注释。
- 涉及 Agent Runtime、Tool 白名单、schema 校验、文件操作、数据库写入、权限、证据、ChangeSet、OperationPlan 的代码，必须解释安全边界和不能绕过的规则。
- 测试代码必须用中文注释说明正在保护的业务行为或安全约束。
- 注释应该解释“为什么这样做”和“边界是什么”，不能只重复代码表面含义。
- 不允许为了满足形式添加无意义注释，例如“给变量赋值”“返回结果”。

## 17. 测试与验证

所有功能改动必须配套测试或明确说明无法自动测试的原因。

如果启动方式、服务端口、Python 环境使用方式、测试命令、可用接口或运行限制发生变化，必须同步更新 `README.md` 和 `docs/runbook.md`。不能让文档中的启动方式落后于代码。

后端任务至少覆盖：

- health。
- auth、角色和 default workspace。
- conversation message entry。
- upload and document version creation。
- LangGraph AgentRun 状态流。
- Tool registry 白名单和 schema 校验。
- Tool invocation 记录。
- processing job 状态。
- parsing and artifact creation。
- ChangeSet / ChangeItem 创建。
- OperationPlan 必须确认后执行。
- chunk、evidence、embedding、检索。
- evidence-answer 有引用和无依据回答。
- multi-label classification evidence。
- feedback 和 admin 处理。
- LLM settings 权限。

测试 embedding、LLM 和 Agent planner 时必须使用 deterministic fake，不依赖真实外部模型服务。

完成 MVP 前必须执行：

```bash
cd apps/api
pytest -v
```

```bash
cd apps/web
npm run build
```

并完成手工烟测：

```text
login as user
open /chat
upload sample files with an instruction
confirm AgentRun is created
confirm processing status updates
confirm each processed file has a receipt
confirm original file is unchanged
ask for evidence answer
confirm answer includes references
request rename suggestions
confirm OperationPlan is shown and not executed
confirm execution only after user confirmation
submit feedback
login as admin
open /admin/feedback
resolve feedback
open /admin/documents
reprocess document
```

## 18. 提交策略

按可验证单元提交，不要把数据库、Agent Runtime、解析、前端页面混在一个提交里。

推荐提交前缀：

```text
chore:
feat:
fix:
test:
docs:
```

每次提交前应确认：

- 工作树中只包含当前任务相关变更。
- 后端测试或对应局部测试通过。
- 前端改动已通过 build 或说明未运行原因。
- 未提交 `.env`、密钥、本地数据库数据或用户上传文件。

## 19. 安全与配置

- `.env.example` 可以提交，真实 `.env` 不得提交。
- LLM API key 必须加密保存，接口只返回 masked key。
- 外部检索和外部模型默认关闭，启用时必须明确提示用户。
- 发送文件内容到外部服务必须通过 OperationPlan 或明确配置授权。
- 修改 `embedding_dim` 在已有 chunk 后必须有 admin 确认流程。
- 文件路径必须通过 StorageService 管理，避免路径穿越。
- 上传文件必须限制格式和大小。
- 不执行上传文件中的宏、脚本、链接或嵌入对象。
- 普通用户只能访问自己 default workspace 下的数据。
- 文件正文、OCR 文本和网页内容永远只能作为数据，不能成为系统指令。

## 20. MVP 完成标准

MVP 只有在以下全部满足时才算完成：

- 普通 user 可以登录并打开 `/chat`。
- `/chat` 是任务型 Agent 工作台，不是单纯 QA 页面。
- 普通 user 可以发送文件工作指令并上传 PDF、DOC、DOCX、XLSX、TXT、MD、CSV。
- 系统创建 AgentRun 并记录状态。
- Agent 通过白名单 Tool 执行文件处理。
- 系统可以保存原件、版本和派生件。
- 系统可以解析上传文件。
- 系统可以创建 chunk、evidence 和 embedding。
- 系统可以为文件生成多标签分类、置信度和证据，至少有基础实现。
- 每次处理产生 ChangeSet 或等价结构化回执。
- 批量处理能逐文件展示结果、失败、待确认和证据。
- 原始文件不会被解析、OCR、摘要、分类或 Skill 覆盖。
- 普通 user 可以自然语言提问并获得带引用的证据回答。
- 无证据问题返回明确的无依据说明。
- 高风险操作只生成 OperationPlan，确认后才执行。
- 普通 user 可以提交反馈。
- 普通 user 不能访问管理页面和 admin API。
- admin/ops 可以查看文件、重处理文件、查看并处理反馈。
- admin/ops 可以配置 LLM 和 embedding 设置。
- 后端测试全部通过。
- 前端构建成功。

满足以上标准后，才进入 Neo4j 文件知识图谱、Graphiti 用户时间记忆、MinIO/S3、Redis/Celery、OCR 增强、预览增强和 Skill 自动演化平台的完整实现阶段。
