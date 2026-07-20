# File Agent Database Schema

本文定义 File Agent MVP 的 PostgreSQL + pgvector 数据库结构。项目定位以 `agent.md` 为准：MVP 从第一版开始使用 LangGraph Agent Runtime，并把 AgentRun、Tool 调用、ChangeSet 和 OperationPlan 作为核心表。

## 1. 设计原则

MVP 使用系统内置 `default workspace`，普通用户不手动创建项目。

权限边界：

```text
user:
- 在 /chat 发送文件工作指令
- 上传文件
- 查看自己的 AgentRun、Tool 调用摘要、ChangeSet、OperationPlan
- 确认自己的 OperationPlan
- 查看引用
- 提交反馈

admin / ops:
- 查看文件处理状态
- 触发重新解析/重新索引
- 处理反馈
- 配置模型
```

通用规则：

- 主键统一使用 UUID。
- 时间字段统一使用 `timestamptz`。
- JSON 字段使用 `jsonb`。
- 向量字段默认使用 `vector(1536)`；如果 embedding 模型维度不是 1536，需要在首次迁移前调整。
- 数据库 MVP 阶段不做行级安全策略，访问控制由后端服务层实现。
- `qa_answers` 是 evidence-answer Skill 的结果表，不代表系统只有 QA 能力。

## 2. PostgreSQL Extensions

```sql
create extension if not exists vector;
create extension if not exists pgcrypto;
```

## 3. Enum Values

建议在应用层使用枚举，并在数据库层使用 check constraint。

```text
User Role:
user
ops
admin

Conversation Status:
active
archived
deleted

Message Role:
user
assistant
system
tool

AgentRun Status:
RECEIVED
PLANNING
WAITING_FOR_CONFIRMATION
RUNNING_TOOL
WAITING_FOR_ASYNC_JOB
SUMMARIZING
COMPLETED
FAILED
NEEDS_REVIEW

Tool Invocation Status:
PENDING
RUNNING
COMPLETED
FAILED
SKIPPED

Document Status:
UPLOADED
USED_IN_MESSAGE
RECEIVED
QUARANTINED
SCANNING
ROUTED
PROCESSING
READY
NEEDS_REVIEW
FAILED

Parse Status:
PENDING
RUNNING
COMPLETED
FAILED

Artifact Type:
EXTRACTED_TEXT
PREVIEW_PDF
THUMBNAIL
OCR_PDF
CONTENT_JSON
EXPORT

Category Relation Role:
PRIMARY
SECONDARY
RELATED
DOCUMENT_TYPE

Category Status:
AUTO_APPLIED
SUGGESTED
CONFIRMED
REJECTED

Job Type:
DOCUMENT_INGEST
BATCH_INGEST
EMBEDDING_REBUILD
CLASSIFICATION
AUDIT_FIX
OPERATION_EXECUTION

Job Status:
PENDING
RUNNING
COMPLETED
FAILED
NEEDS_REVIEW

OperationPlan Status:
PLANNED
WAITING_CONFIRMATION
EXECUTING
EXECUTED
CANCELLED
FAILED

ChangeSet Status:
PENDING
COMPLETED
FAILED
NEEDS_REVIEW

Feedback Target Type:
ANSWER
REFERENCE
CHUNK
DOCUMENT
CHANGESET
OPERATION_PLAN
WIKI_PAGE

Feedback Type:
WRONG_ANSWER
MISSING_SOURCE
BAD_SOURCE
OUTDATED
NEEDS_MORE_DETAIL
WRONG_CLASSIFICATION
BAD_OPERATION_PLAN
OTHER

Feedback Status:
OPEN
RESOLVED
REJECTED
```

## 4. Tables

### 4.1 users

```sql
create table users (
  id uuid primary key default gen_random_uuid(),
  username varchar(100) not null unique,
  email varchar(255) null unique,
  password_hash varchar(255) not null,
  display_name varchar(100) not null default '',
  role varchar(20) not null default 'user',
  default_workspace_id uuid null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint users_role_check check (role in ('user', 'ops', 'admin'))
);
```

### 4.2 workspaces

```sql
create table workspaces (
  id uuid primary key default gen_random_uuid(),
  name varchar(200) not null,
  description text not null default '',
  owner_id uuid not null references users(id) on delete cascade,
  is_default boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table users
  add constraint users_default_workspace_fk
  foreign key (default_workspace_id) references workspaces(id)
  on delete set null;
```

Implementation note:

```text
当前 SQLite 兼容 migration 先添加 default_workspace_id 字段，不单独 ALTER 添加外键约束；
PostgreSQL 生产 migration 可以在后续收紧该约束。
```

Indexes:

```sql
create index workspaces_owner_id_idx on workspaces(owner_id);
create unique index workspaces_owner_default_uidx on workspaces(owner_id) where is_default = true;
```

### 4.3 workspace_members

```sql
create table workspace_members (
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  role varchar(20) not null default 'user',
  created_at timestamptz not null default now(),
  primary key (workspace_id, user_id),
  constraint workspace_members_role_check check (role in ('user', 'ops', 'admin'))
);
```

### 4.4 conversations

```sql
create table conversations (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  title varchar(200) not null default '新会话',
  status varchar(20) not null default 'active',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint conversations_status_check check (status in ('active', 'archived', 'deleted'))
);
```

Indexes:

```sql
create index conversations_workspace_user_idx on conversations(workspace_id, user_id);
create index conversations_updated_at_idx on conversations(updated_at desc);
```

### 4.5 messages

```sql
create table messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  role varchar(20) not null,
  content text not null,
  agent_run_id uuid null,
  created_at timestamptz not null default now(),
  constraint messages_role_check check (role in ('user', 'assistant', 'system', 'tool'))
);
```

`agent_run_id` 的 foreign key 在 `agent_runs` 创建后补充。

### 4.6 agent_runs

```sql
create table agent_runs (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null references conversations(id) on delete cascade,
  message_id uuid not null references messages(id) on delete cascade,
  user_id uuid not null,
  intent varchar(100) null,
  status varchar(40) not null default 'RECEIVED',
  selected_skills_json jsonb not null default '[]'::jsonb,
  plan_json jsonb not null default '{}'::jsonb,
  graph_state_json jsonb not null default '{}'::jsonb,
  final_response text null,
  error_message text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint agent_runs_status_check check (status in ('RECEIVED', 'PLANNING', 'WAITING_FOR_CONFIRMATION', 'RUNNING_TOOL', 'WAITING_FOR_ASYNC_JOB', 'SUMMARIZING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW'))
);
```

Indexes:

```sql
create index agent_runs_conversation_idx on agent_runs(conversation_id, created_at desc);
create index agent_runs_message_idx on agent_runs(message_id);
```

### 4.7 tool_invocations

```sql
create table tool_invocations (
  id uuid primary key default gen_random_uuid(),
  agent_run_id uuid not null references agent_runs(id) on delete cascade,
  tool_name varchar(100) not null,
  status varchar(30) not null,
  input_json jsonb not null default '{}'::jsonb,
  output_json jsonb not null default '{}'::jsonb,
  changeset_id uuid null,
  operation_plan_id uuid null,
  created_at timestamptz not null default now(),
  finished_at timestamptz null,
  constraint tool_invocations_status_check check (status in ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED'))
);
```

Indexes:

```sql
create index tool_invocations_agent_run_idx on tool_invocations(agent_run_id, created_at);
create index tool_invocations_tool_name_idx on tool_invocations(tool_name);
```

### 4.8 documents

Current implementation note:

```text
当前已落地最小上传闭环：documents + file_objects。
更完整的 document_versions、processing_jobs、artifacts 会在后续解析链路中继续补齐。
```

```sql
create table documents (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id),
  workspace_id uuid null references workspaces(id),
  original_filename varchar(255) not null,
  content_type varchar(120) not null default 'application/octet-stream',
  size_bytes bigint not null default 0,
  sha256 varchar(64) not null,
  status varchar(40) not null default 'UPLOADED',
  ingest_status varchar(40) not null default 'UPLOADED',
  locked_at timestamptz null,
  locked_message_id uuid null,
  locked_conversation_id uuid null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

Indexes:

```sql
create index documents_workspace_idx on documents(workspace_id);
create index documents_owner_idx on documents(user_id);
create index documents_sha256_idx on documents(sha256);
```

### 4.8.1 file_objects

```sql
create table file_objects (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id),
  storage_backend varchar(40) not null default 'local',
  storage_path varchar(500) not null,
  size_bytes bigint not null,
  sha256 varchar(64) not null,
  created_at timestamptz not null default now()
);
```

### 4.8.2 document_insights

```sql
create table document_insights (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null unique references documents(id),
  keywords_json jsonb not null default '[]',
  labels_json jsonb not null default '[]',
  summary text not null default '',
  extracted_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

### 4.8.3 document_extraction_runs 当前实现

当前代码已实现最小文件解析持久化表，用于 `extract-document-text` Tool。后续接入 `document_versions` 后，可再演进到 4.11 的版本化页面结构。

```sql
create table document_extraction_runs (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id),
  status varchar(40) not null default 'RUNNING',
  extractor varchar(80) not null default '',
  error_message text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

### 4.8.4 document_pages 当前实现

```sql
create table document_pages (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id),
  extraction_run_id uuid not null references document_extraction_runs(id),
  page_number integer null,
  sheet_name varchar(255) null,
  text_content text not null default '',
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
```

### 4.9 document_versions

当前实现先以不可变 `Document.id + sha256` 作为源文件版本，并使用下表保存可跨解析运行复用的旧版 Office 派生件。后续正式引入 `document_versions` 时，再把 `document_artifacts.document_id` 平滑扩展为版本关联。

```sql
create table document_artifacts (
  id varchar(36) primary key,
  document_id varchar(36) not null references documents(id),
  artifact_type varchar(50) not null,
  storage_backend varchar(40) not null default 'local',
  storage_path text not null,
  content_type varchar(120) not null,
  size_bytes bigint not null,
  sha256 varchar(64) not null,
  source_sha256 varchar(64) not null,
  converter_name varchar(80) not null,
  converter_version varchar(120) not null default '',
  converter_config_hash varchar(64) not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (document_id, artifact_type, source_sha256, converter_config_hash)
);
```

`CONVERTED_DOCX` 记录只保存相对 `FILE_STORAGE_ROOT` 的路径。相同源哈希和转换指纹可以跨 Document 复用同一物理文件，但每个 Document 必须保留独立记录；最后一个引用删除后才能删除物理派生件。

```sql
create table document_versions (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  version_no integer not null,
  storage_key varchar(1000) not null,
  sha256 varchar(64) not null,
  parse_status varchar(30) not null default 'PENDING',
  parser_version varchar(50) not null default 'v1',
  created_at timestamptz not null default now(),
  constraint document_versions_parse_status_check check (parse_status in ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')),
  constraint document_versions_unique_version unique (document_id, version_no)
);
```

### 4.10 artifacts

```sql
create table artifacts (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  document_version_id uuid not null references document_versions(id) on delete cascade,
  artifact_type varchar(50) not null,
  storage_key varchar(1000) not null,
  mime_type varchar(200) not null default 'application/octet-stream',
  size_bytes bigint not null default 0,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint artifacts_type_check check (artifact_type in ('EXTRACTED_TEXT', 'PREVIEW_PDF', 'THUMBNAIL', 'OCR_PDF', 'CONTENT_JSON', 'EXPORT'))
);
```

### 4.11 document_pages

```sql
create table document_pages (
  id uuid primary key default gen_random_uuid(),
  document_version_id uuid not null references document_versions(id) on delete cascade,
  page_no integer not null,
  text text not null default '',
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint document_pages_unique_page unique (document_version_id, page_no)
);
```

### 4.12 document_chunks

```sql
create table document_chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  document_version_id uuid not null references document_versions(id) on delete cascade,
  page_id uuid null references document_pages(id) on delete set null,
  chunk_index integer not null,
  text text not null,
  token_count integer not null default 0,
  embedding vector(1536) null,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint document_chunks_unique_chunk unique (document_version_id, chunk_index)
);
```

Indexes:

```sql
create index document_chunks_document_idx on document_chunks(document_id);
create index document_chunks_version_idx on document_chunks(document_version_id);
create index document_chunks_text_search_idx on document_chunks using gin(to_tsvector('simple', text));
create index document_chunks_embedding_idx on document_chunks using ivfflat (embedding vector_cosine_ops) with (lists = 100);
```

### 4.13 evidence_spans

```sql
create table evidence_spans (
  id uuid primary key default gen_random_uuid(),
  chunk_id uuid not null references document_chunks(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  page_no integer null,
  sheet_name varchar(200) null,
  cell_range varchar(100) null,
  start_char integer null,
  end_char integer null,
  quote text not null default '',
  created_at timestamptz not null default now()
);
```

### 4.14 categories

```sql
create table categories (
  id uuid primary key default gen_random_uuid(),
  code varchar(100) not null,
  name varchar(200) not null,
  parent_id uuid null references categories(id) on delete set null,
  category_type varchar(50) not null default 'business',
  taxonomy_version varchar(50) not null default 'v1',
  created_at timestamptz not null default now(),
  constraint categories_code_version_unique unique (code, taxonomy_version)
);
```

### 4.15 document_classification_runs

```sql
create table document_classification_runs (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  agent_run_id uuid not null references agent_runs(id) on delete cascade,
  taxonomy_key varchar(100) not null default 'school_file_classification',
  taxonomy_version varchar(50) not null default '2026-06',
  classifier_version varchar(100) not null default 'taxonomy-rule-v1',
  source varchar(30) not null default 'rule',
  status varchar(30) not null default 'COMPLETED',
  error_message text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

### 4.16 document_category_suggestions

```sql
create table document_category_suggestions (
  id uuid primary key default gen_random_uuid(),
  classification_run_id uuid not null references document_classification_runs(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  category_name varchar(500) not null,
  category_path_json jsonb not null default '[]'::jsonb,
  taxonomy_key varchar(100) not null default 'school_file_classification',
  taxonomy_version varchar(50) not null default '2026-06',
  confidence double precision not null default 0,
  status varchar(30) not null default 'SUGGESTED',
  evidence_json jsonb not null default '[]'::jsonb,
  source varchar(30) not null default 'rule',
  rank integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

### 4.17 document_category_feedback

```sql
create table document_category_feedback (
  id uuid primary key default gen_random_uuid(),
  suggestion_id uuid not null references document_category_suggestions(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  action varchar(30) not null,
  comment text not null default '',
  created_at timestamptz not null default now()
);
```

`document_classification_runs` 和 `document_category_suggestions` 保存 AgentRun 生成的可追踪建议，不等同于用户确认后的正式分类。`document_category_feedback` 预留用户接受、拒绝和修正记录。正式分类关系仍写入 `document_categories`。

### 4.18 document_categories

```sql
create table document_categories (
  id uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  category_id uuid not null references categories(id) on delete cascade,
  relation_role varchar(30) not null,
  confidence double precision not null default 0,
  status varchar(30) not null default 'SUGGESTED',
  evidence_span_id uuid null references evidence_spans(id) on delete set null,
  classifier_version varchar(50) not null default 'v1',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint document_categories_role_check check (relation_role in ('PRIMARY', 'SECONDARY', 'RELATED', 'DOCUMENT_TYPE')),
  constraint document_categories_status_check check (status in ('AUTO_APPLIED', 'SUGGESTED', 'CONFIRMED', 'REJECTED'))
);
```

### 4.19 qa_answers

```sql
create table qa_answers (
  id uuid primary key default gen_random_uuid(),
  message_id uuid not null references messages(id) on delete cascade,
  agent_run_id uuid null references agent_runs(id) on delete set null,
  question text not null,
  answer text not null,
  model varchar(100) not null default '',
  retrieval_trace_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
```

### 4.20 answer_references

```sql
create table answer_references (
  id uuid primary key default gen_random_uuid(),
  answer_id uuid not null references qa_answers(id) on delete cascade,
  chunk_id uuid not null references document_chunks(id) on delete cascade,
  document_id uuid not null references documents(id) on delete cascade,
  evidence_span_id uuid null references evidence_spans(id) on delete set null,
  score double precision not null default 0,
  created_at timestamptz not null default now()
);
```

### 4.21 operation_plans

```sql
create table operation_plans (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  conversation_id uuid not null references conversations(id) on delete cascade,
  agent_run_id uuid null references agent_runs(id) on delete set null,
  user_id uuid not null references users(id) on delete cascade,
  operation_type varchar(80) not null,
  status varchar(40) not null default 'PLANNED',
  risk_level varchar(20) not null default 'medium',
  reason text not null default '',
  plan_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  confirmed_at timestamptz null,
  executed_at timestamptz null,
  constraint operation_plans_status_check check (status in ('PLANNED', 'WAITING_CONFIRMATION', 'EXECUTING', 'EXECUTED', 'CANCELLED', 'FAILED'))
);
```

### 4.22 operation_confirmations

```sql
create table operation_confirmations (
  id uuid primary key default gen_random_uuid(),
  operation_plan_id uuid not null references operation_plans(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  confirmation_text varchar(200) not null default '',
  created_at timestamptz not null default now()
);
```

### 4.23 change_sets

```sql
create table change_sets (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  conversation_id uuid null references conversations(id) on delete set null,
  agent_run_id uuid null references agent_runs(id) on delete set null,
  operation_plan_id uuid null references operation_plans(id) on delete set null,
  user_id uuid not null references users(id) on delete cascade,
  operation_type varchar(80) not null,
  status varchar(30) not null default 'PENDING',
  summary_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  completed_at timestamptz null,
  constraint change_sets_status_check check (status in ('PENDING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW'))
);

alter table agent_runs
  add constraint agent_runs_changeset_fk
  foreign key (changeset_id) references change_sets(id)
  on delete set null;

alter table agent_runs
  add constraint agent_runs_operation_plan_fk
  foreign key (operation_plan_id) references operation_plans(id)
  on delete set null;

alter table tool_invocations
  add constraint tool_invocations_changeset_fk
  foreign key (changeset_id) references change_sets(id)
  on delete set null;

alter table tool_invocations
  add constraint tool_invocations_operation_plan_fk
  foreign key (operation_plan_id) references operation_plans(id)
  on delete set null;
```

### 4.24 change_items

```sql
create table change_items (
  id uuid primary key default gen_random_uuid(),
  changeset_id uuid not null references change_sets(id) on delete cascade,
  target_type varchar(50) not null,
  target_id uuid null,
  target_document_id uuid null references documents(id) on delete set null,
  change_type varchar(80) not null,
  before_value_json jsonb not null default '{}'::jsonb,
  after_value_json jsonb not null default '{}'::jsonb,
  source varchar(100) not null default '',
  confidence double precision not null default 0,
  evidence_json jsonb not null default '{}'::jsonb,
  execution_status varchar(30) not null default 'COMPLETED',
  created_at timestamptz not null default now()
);
```

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

### 4.25 feedback

```sql
create table feedback (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  target_type varchar(30) not null,
  target_id uuid not null,
  feedback_type varchar(50) not null,
  comment text not null default '',
  status varchar(20) not null default 'OPEN',
  resolution text not null default '',
  resolved_by uuid null references users(id) on delete set null,
  created_at timestamptz not null default now(),
  resolved_at timestamptz null,
  constraint feedback_target_type_check check (target_type in ('ANSWER', 'REFERENCE', 'CHUNK', 'DOCUMENT', 'CHANGESET', 'OPERATION_PLAN', 'WIKI_PAGE')),
  constraint feedback_type_check check (feedback_type in ('WRONG_ANSWER', 'MISSING_SOURCE', 'BAD_SOURCE', 'OUTDATED', 'NEEDS_MORE_DETAIL', 'WRONG_CLASSIFICATION', 'BAD_OPERATION_PLAN', 'OTHER')),
  constraint feedback_status_check check (status in ('OPEN', 'RESOLVED', 'REJECTED'))
);
```

### 4.26 processing_jobs

```sql
create table processing_jobs (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  conversation_id uuid null references conversations(id) on delete set null,
  agent_run_id uuid null references agent_runs(id) on delete set null,
  user_id uuid not null references users(id) on delete cascade,
  job_type varchar(50) not null,
  status varchar(30) not null default 'PENDING',
  total_items integer not null default 0,
  processed_items integer not null default 0,
  error_message text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz null,
  constraint processing_jobs_type_check check (job_type in ('DOCUMENT_INGEST', 'BATCH_INGEST', 'EMBEDDING_REBUILD', 'CLASSIFICATION', 'AUDIT_FIX', 'OPERATION_EXECUTION')),
  constraint processing_jobs_status_check check (status in ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'NEEDS_REVIEW'))
);
```

### 4.27 processing_events

```sql
create table processing_events (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references processing_jobs(id) on delete cascade,
  level varchar(20) not null default 'info',
  message text not null,
  payload_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  constraint processing_events_level_check check (level in ('debug', 'info', 'warn', 'error'))
);
```

### 4.28 llm_settings

```sql
create table llm_settings (
  id uuid primary key default gen_random_uuid(),
  workspace_id uuid not null references workspaces(id) on delete cascade,
  provider varchar(50) not null default 'openai_compatible',
  api_url varchar(1000) not null default '',
  api_key_encrypted text not null default '',
  chat_model varchar(100) not null default '',
  embedding_model varchar(100) not null default '',
  embedding_dim integer not null default 1536,
  external_file_content_enabled boolean not null default false,
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint llm_settings_embedding_dim_check check (embedding_dim > 0)
);
```

### 4.29 user_preferences

```sql
create table user_preferences (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  preference_type varchar(50) not null,
  key varchar(200) not null,
  value_json jsonb not null default '{}'::jsonb,
  source varchar(50) not null default 'explicit',
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
```

This table is for explicit MVP preferences only. It is not Graphiti memory.

## 5. Updated At Trigger

建议给包含 `updated_at` 的表统一加触发器。

```sql
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;
```

Apply the trigger to:

```text
users
workspaces
conversations
agent_runs
documents
document_classification_runs
document_category_suggestions
document_categories
operation_plans
processing_jobs
llm_settings
user_preferences
```

## 6. Access Rules

规则：

```text
user:
- can read own conversations
- can create own conversations
- can send messages to own conversations
- can upload documents to own conversations
- can read own AgentRuns and Tool invocation summaries
- can read documents in own default workspace
- can read own ChangeSets
- can create and confirm own OperationPlans
- can ask evidence-backed questions
- can create feedback
- cannot resolve feedback
- cannot call admin endpoints
- cannot update llm_settings
- cannot bypass OperationPlan confirmation

admin / ops:
- can read admin document list
- can reprocess documents
- can read job and processing events
- can read and resolve feedback
- can update llm_settings
```

## 7. Migration Order

建议第一版迁移顺序：

```text
1. extensions
2. users
3. workspaces
4. users.default_workspace_id foreign key
5. workspace_members
6. conversations
7. messages
8. agent_runs
9. messages.agent_run_id foreign key
10. tool_invocations
11. documents
12. document_versions
13. artifacts
14. document_extraction_runs
15. document_pages
16. document_chunks
17. evidence_spans
18. categories
19. document_classification_runs
20. document_category_suggestions
21. document_category_feedback
22. document_categories
23. qa_answers
24. answer_references
25. operation_plans
26. operation_confirmations
27. change_sets
28. change_items
29. agent_runs/tool_invocations references to changeset and operation_plan
30. feedback
31. processing_jobs
32. processing_events
33. llm_settings
34. user_preferences
35. indexes
36. updated_at triggers
```

## 8. Deferred Tables

以下表不进入 MVP：

```text
wiki_pages
graph_nodes
graph_edges
skill_versions
skill_candidates
skill_evaluations
skill_release_history
graphiti_memory_events
```

以下能力进入后续阶段：

```text
完整 Neo4j 文件事实图谱
Graphiti 用户时间记忆
自动 Skill 演化、评测、灰度、回滚
Wiki 页面生成和治理
复杂权限体系
```

注意：`change_sets`、`change_items`、`operation_plans`、`categories`、`document_categories` 不再是 deferred tables，它们属于 MVP。
