# 全分类新文件测试的数据重置与隔离方案

> 日期：2026-08-27
> 状态：方案讨论，尚未执行
> 当前授权边界：仅输出方案；未授权清空数据库、删除文件、修改配置、执行迁移或修改代码
> 关联方案：`docs/2026-08-26-confidence-gated-auto-classification-initial-placement-plan.md`

## 1. 已确定方案

当前目标是用一批覆盖全部有效分类的新文件，验证首次分类、主分类自动落位、复核率和前端分类树展示是否准确。

当前已经确定采用：

```text
一次受控的文件域干净重置，保留用户信息
+ 暂停外部目录自动导入
+ 完整 taxonomy 全分类测试
+ 后续补充 test_dataset_id/execution_id 隔离
```

强制范围：

1. **只清除文件域及其依赖任务数据。**清除旧文件事实、工作副本、分类结果、检索投影、上传内容、文件处理任务和引用旧文件的会话任务数据。
2. **保留用户身份数据。**保留用户 ID、用户名、邮箱、密码哈希、显示名称、角色、账号时间字段、用户默认工作区和必要的非文件系统配置。
3. **现有全量重置脚本不能原样执行。**它会清空全部业务表，包括用户；后续需要新增文件域专用受控重置模式或独立命令。
4. **测试期间暂停外部目录自动导入。**否则外部文件会重新进入共享工作区，破坏干净基线。
5. **初次测试开放完整 taxonomy。**数据清理范围不能改变 taxonomy 候选空间。
6. **后续使用测试数据集隔离。**每轮测试使用独立 `test_dataset_id + execution_id`，避免再次清库。

不推荐只增加 `created_at >= 某时间` 之类的临时查询条件作为正式方案。它只能隐藏旧结果，不能阻止旧数据影响重复检测、图谱、已确认分类支持、工作副本复用和分类树统计。

本轮只讨论和固化方案。任何真实重置都属于破坏性操作，必须在后续得到用户再次明确授权并展示最终目标范围后才能执行。

## 2. 当前实现现状

### 2.1 已有受控开发重置命令

项目已有：

```text
apps/api/app/scripts/reset_development_shared_workspace.py
```

它要求显式参数：

```text
--confirm-reset-shared-workspace
```

当前命令会：

- 清空 SQLAlchemy `Base.metadata` 注册的全部业务表。
- 保留 `alembic_version`。
- 重新创建唯一的 `SYSTEM_SHARED` 工作区。
- 清空共享工作副本目录。
- 清空共享回收站。
- 清空 `FILE_STORAGE_ROOT` 下的 `uploads`、`quarantine` 和 `temp`。
- 清空配置的内部上传原件保护目录 `MANAGED_ROOT_ARCHIVE_WRITE_PATH`。
- 拒绝空路径、文件系统根、项目根、重复目标，以及与外部受管原始资料目录重叠的路径。
- 保留外部 `MANAGED_ROOT_*` 学校原始资料目录。

### 2.2 现有命令比“清空文件数据库”范围更大

因为它清空全部业务表，所以还会删除：

- 用户和登录数据。
- 会话、消息和附件上下文。
- AgentRun、ToolInvocation、ChangeSet 和 OperationPlan。
- 文件、版本、页面、摘要、Chunk、Evidence 和检索投影。
- 分类运行、建议、反馈、正式分类和组织决策。
- 上传查重、工作副本、回收站和异步任务记录。
- 其他已经注册到 `Base.metadata` 的业务配置或运行数据。

因此，不能把该命令描述成“只清文件表”，也不能直接用于本次已经确定的“保留用户信息”方案。后续实现必须新增文件域专用重置边界，禁止通过调用现有全量 `TRUNCATE ... CASCADE` 后再尝试恢复用户。

### 2.3 外部受管目录会重新导入

当前环境中：

```text
MANAGED_ROOT_WATCH_ENABLED=true
MANAGED_ROOT_RECONCILE_ON_STARTUP=true
```

现有重置命令不会删除外部受管原始资料。若重置后直接启动 watcher、scheduler 或 reconcile，外部文件可能重新进入数据库和共享工作副本，使“只测试本轮新上传文件”的环境再次被旧资料填充。

因此，干净上传分类测试期间必须二选一：

- 暂时停用外部受管目录 watcher、启动对账和扫描任务；或者
- 使用可靠的测试数据集隔离，让查询和评测只作用于当前 `test_dataset_id`。

为排除查重和历史分类支持的影响，首次基线更推荐同时暂停外部导入。

### 2.4 当前上传 `batch_id` 不足以做长期隔离

现有上传批次 ID 由会话仓储生成，保存在 `messages.attachments_json` 中，主要用于解释“刚上传的文件”。它没有稳定传播到：

- `working_copies`
- `document_classification_runs`
- `document_organization_decisions`
- `document_search_profiles`
- 主分类树聚合查询

所以它适合单条消息附件上下文，不适合贯穿完整分类测试、结果对比和前端文件树。

## 3. 四种方案对比

| 方案 | 数据是否删除 | 分类过程是否真正隔离 | 开发量 | 适用场景 | 推荐度 |
|---|---:|---:|---:|---|---:|
| A. 现有全量开发重置 | 是，且删除用户 | 高 | 低，已有脚本 | 允许全部业务数据丢弃 | 本次禁止 |
| B. 文件域受控重置并保留用户 | 是，仅文件域和依赖任务 | 高 | 中，需要新边界 | 当前干净基线 | 本次采用 |
| C. 临时时间/用户/ID 查询条件 | 否 | 低 | 很低 | 临时查看新文件结果 | 仅临时辅助 |
| D. 版本化测试数据集隔离 | 否 | 中到高 | 中 | 多轮、可复现、可对比测试 | 后续采用 |

### 3.1 方案 A：现有全量开发重置，本次不采用

优点：

- PostgreSQL 文件事实、分类结果、索引、工作副本和上传存储全部从零开始。
- 不会因为旧工作副本触发重复上传复用。
- 不会把旧分类建议或正式分类混入本轮统计。
- 最适合验证首次上传、首次分类和主分类首次落位完整链路。

代价：

- 会清除所有业务表，不只文件数据。
- 测试账号、会话和管理设置需要重新建立。
- 外部受管目录若重新扫描，仍会重新产生数据。
- 现有脚本不负责清理外部 Neo4j 投影；若图谱参与分类，需要单独处理。

### 3.2 方案 B：文件域受控重置并保留用户，本次采用

该方案新增显式文件域清理命令或作用域，例如：

```text
reset_classification_test_data
或
reset_development_shared_workspace --scope file-domain --preserve-users
```

这里只定义后续接口语义，不代表现在已经存在或可以执行。

核心规则：

- 不调用 `Base.metadata` 全表清空。
- 不使用可能级联清除 `users`、保留工作区或系统设置的 `TRUNCATE ... CASCADE`。
- 使用版本化的显式表分类清单和受控 `DELETE` 顺序。
- 清除所有文件事实和引用这些事实的任务数据，避免悬空外键和旧附件上下文。
- 保留用户账号、密码哈希、角色和默认工作区关系。
- 清理前后校验用户表逐行指纹和数量完全一致。

优点：

- 得到不受旧文件影响的分类基线。
- 用户可以继续用原账号登录，无需重新注册。
- 现有角色和必要配置不会丢失。

代价：

- 需要新增代码和测试，不能直接使用现有全量脚本。
- 必须显式处理会话附件、Agent 审计、文件反馈和异步任务等依赖关系。
- 后续新增数据库表时必须更新 KEEP/CLEAR 分类清单，否则重置应拒绝执行。

### 3.3 方案 C：临时查询条件

可以临时使用：

```text
Document.created_at >= test_started_at
Document.user_id = test_user_id
DocumentVersion.source_type = 'UPLOAD'
WorkingCopy.status = 'ACTIVE'
working_copy_id/document_id IN 本轮上传后记录的稳定 ID 集合
```

其中最可靠的是保存本轮明确的 `document_id`、`document_version_id` 和 `working_copy_id` 清单，再使用 ID 集合过滤。单独按时间和用户过滤不可靠，因为：

- `WorkingCopy` 是共享对象，不直接表达上传测试用户。
- 重复上传可能复用较早创建的工作副本，导致 `created_at` 不在本轮时间范围。
- 异步任务完成时间与上传时间不同。
- 时间边界可能包含并发上传或后台扫描文件。
- 旧数据仍可影响查重、图谱支持和正式分类统计。

因此，该方案可以用于“只看本轮结果”，但不能证明分类运行处在干净环境中。

### 3.4 方案 D：版本化测试数据集隔离

为可重复测试新增稳定测试数据集边界：

```text
classification_test_datasets
classification_test_dataset_items
classification_test_executions
classification_test_results
```

它不改变普通用户文件事实，只给测试和评测建立显式范围。

## 4. 推荐执行路径

### 4.0 仅复测首次落位策略时的最小重置

如果 taxonomy、正文解析、摘要和候选分类算法均未变化，只调整
`AutoPlacementPolicy` 如何从既有候选决定首次物理目录，则不执行本方案后续的文件域全量重置。
此时使用：

```powershell
python -m app.scripts.reset_managed_root_working_copies `
  --root-key test_library `
  --confirm-reset-working-copies `
  --confirm-writers-stopped
```

该命令按唯一 `managed_root + working_copy_root` 清除工作副本实体文件、工作副本 Document/Version、
分类建议副本、正式主分类、组织决策、路径记录、搜索投影及直接关联 ChangeItem/ChangeSet，并把既有
`MATERIALIZE_WORKING_COPY` 幂等任务重新置为 `PENDING`。它必须保留 `managed_files`、
`managed_file_revisions`、源分析 Document/Version、页面、摘要、Chunk、源侧分类运行和候选建议。

该最小重置只验证“既有候选 -> 主分类 -> 首次落位”，不能用于验证 taxonomy、解析、摘要或候选分类
算法发生变化后的效果。执行前仍必须停止 API、scheduler、watcher 和全部 worker；目标工作副本路径
必须位于 `WORKING_COPY_STORAGE_ROOT` 内且不能与外部受管原目录重叠。

### 4.1 首轮：干净基线测试

在保留用户身份数据的前提下，首轮采用：

```text
冻结本轮分类配置
-> 停止所有写入进程
-> 预检数据库与精确目录目标
-> 输出 KEEP/CLEAR/REBUILD 表清单和保留用户指纹
-> 用户再次确认文件域清空范围
-> 执行文件域受控重置
-> 验证数据库和存储处于空基线
-> 暂停外部受管目录自动导入
-> 使用原有账号登录并创建全新测试会话
-> 上传覆盖全部分类的新文件
-> 等待解析、分类、落位和索引完成
-> 生成逐分类评测报告
```

### 4.2 后续：测试数据集隔离

首轮基线完成后，增加 `test_dataset_id`。之后每轮测试：

```text
创建测试数据集
-> 固定 taxonomy/classifier/policy 版本
-> 上传或关联测试文件
-> 保存人工标准答案
-> 创建一次测试执行
-> 运行全部分类
-> 文件树、表格和评测 API 只查询当前数据集
-> 保存结果并关闭数据集
```

这样可以比较不同分类器版本，而不必反复清空数据库。

## 5. 文件域受控重置的安全方案

### 5.1 重置前必须确认

执行前必须输出并确认：

- 当前数据库连接目标的脱敏标识，确认不是生产库。
- `KEEP`、`CLEAR` 和 `REBUILD` 三类表的完整清单及主要对象计数。
- 待保留用户数量，以及基于 `id + username + email + password_hash + role + default_workspace_id` 生成的不可逆校验指纹。
- 共享工作副本、回收站、上传暂存、隔离暂存、临时目录和内部原件保护目录的解析后绝对路径。
- 外部受管目录保护清单。
- 是否需要保存数据库备份或测试报告。
- API、scheduler、watcher、文件 worker、分析 worker 和图谱同步进程是否已停止。

若存在未分类的新业务表，或任何目标为空、指向根目录、项目根、用户目录或与外部受管资料重叠，必须停止，不能手动绕过。

### 5.2 需要停止的进程

必须停止：

```text
FastAPI API
scheduler
managed-root watcher/reconcile
filesystem worker
file lifecycle/materialization worker
document analysis worker
structured extraction worker
graph projection/sync worker
```

否则重置期间可能发生并发写入、文件重新生成或外部目录重新扫描。

### 5.3 清空范围

后续实现必须维护显式表分类清单。当前方案定义如下。

#### 5.3.1 `KEEP`：必须原样保留

| 数据 | 处理规则 |
|---|---|
| `users` | 所有行和身份字段原样保留，包括 ID、用户名、邮箱、密码哈希、显示名称、角色和时间字段 |
| `workspaces` | 保留用户默认工作区和唯一 `SYSTEM_SHARED` 工作区，保证 `users.default_workspace_id` 不失效 |
| `managed_roots` | 保留外部目录授权配置、root_key、显示名称和安全策略；清空其文件索引后暂停扫描 |
| `alembic_version` | 原样保留 |
| 非文件系统设置 | 保留模型 Provider、角色策略等非文件事实；密钥仍按既有安全规则保存 |
| taxonomy/rules/skills | 保留项目配置文件，全部有效分类仍参与测试 |

`users` 和 `workspaces` 在数据库事务中不得执行 DELETE、TRUNCATE 或依赖 CASCADE 删除。重置前后必须比较数量和逐行指纹；任一用户字段变化都视为重置失败。

#### 5.3.2 `CLEAR`：清除文件事实和文件任务上下文

按当前 ORM 模型，至少清除：

```text
文件与版本：
documents
file_objects
document_versions
document_artifacts
document_insights

解析与结构化结果：
document_extraction_runs
document_pages
document_summaries
document_classification_summaries
document_elements
structured_extraction_runs
structured_extraction_fields

索引与证据：
document_index_runs
document_chunks
evidence_spans
qa_answers
answer_references
document_search_profiles

分类事实：
document_classification_runs
document_category_suggestions
document_category_feedback
document_categories
document_category_confirmation_sources
classification_clarifications
classification_graph_outbox
graph_projection_runs
document_organization_decisions

受管文件索引与工作副本：
managed_files
managed_file_revisions
managed_file_analysis_runs
managed_file_search_profiles
managed_file_text_chunks
managed_file_table_structures
managed_file_events
managed_file_snapshots
working_copy_roots
working_copies
working_copy_path_records
trash_entries
relevant_file_sets
relevant_file_set_items

上传、查重和异步任务：
upload_archive_records
upload_duplicate_reviews
upload_duplicate_candidates
filesystem_jobs
filesystem_job_events
filesystem_scan_runs

文件操作、建议和审计：
file_rename_review_items
file_rename_batches
file_rename_batch_items
operation_plans
operation_confirmations
change_sets
change_items
```

#### 5.3.3 会话与 Agent 数据处理

为避免保留引用已删除文件的附件 JSON、引用、计划和运行快照，本次文件域重置必须同时清除：

```text
conversations
messages
agent_runs
tool_invocations
file_search_clarifications
planner_shadow_comparisons
capability_suggestions
```

这表示保留用户账号，但不保留旧聊天和文件任务历史。用户重置后使用原账号登录并创建新会话，不需要重新注册或重设密码。

如果未来必须保留纯文本非文件会话，需要另行实现逐消息附件、引用和 AgentRun 依赖清洗；本次干净分类基线不采用这种高复杂度做法。

#### 5.3.4 `REBUILD/RESET`：保留配置，重置运行状态

- 保留 `managed_roots` 行，但将 `last_reconciled_at` 等扫描游标重置为空，并在测试期间禁用 watcher/reconcile。
- 保留唯一 `SYSTEM_SHARED` 和用户默认工作区；删除文件事实后验证工作区不存在悬空文件引用。
- 新测试开始后按正常业务流程重建工作副本根、分类运行、组织决策、Chunk、Evidence 和搜索投影。
- 不从数据库备份恢复任何旧文件派生数据。

#### 5.3.5 文件系统清理范围

清空：

```text
共享工作副本目录
共享回收站
上传暂存
隔离暂存
临时处理目录
内部上传原件保护目录
```

明确保留：

```text
外部 MANAGED_ROOT_* 原始资料目录
项目代码与 docs
taxonomy JSON 配置
规则和 Skill 文件
模型缓存
诊断日志，除非另有明确清理要求
```

#### 5.3.6 实现安全约束

- 使用显式 `DELETE` 依赖顺序或经过验证的定向删除，禁止 `TRUNCATE Base.metadata ... CASCADE`。
- 启动时把当前数据库所有表与 KEEP/CLEAR/REBUILD manifest 对比；出现未分类表必须拒绝执行。
- 数据库可连接、表清单验证、用户指纹快照和目录安全校验全部通过后，才能触碰文件系统。
- 数据库删除和目录清理必须沿用现有受控脚本的失败回滚与精确路径保护思想。
- 不允许先清空全库再从临时文件恢复用户，因为这会破坏密码、角色、外键和审计可信度。

### 5.4 Neo4j 和其他派生存储

现有重置脚本没有清理 Neo4j。首轮分类基线必须明确选择一种模式：

1. 规则基线：设置图谱分类为 `off`，不让旧图谱影响本轮结果。
2. 图谱 shadow：允许记录增强差异，但用户可见分类仍使用基础结果，并在报告中单独标识。
3. 图谱增强正式测试：先对测试图投影做受控清理和重建，再运行本轮数据；不能复用来源不明的旧投影。

当前环境是 `GRAPH_CLASSIFICATION_MODE=shadow`。如果维持 shadow，必须把基础分类指标和图谱 shadow 指标分开报告。

### 5.5 重置后验证

至少验证：

- `alembic_version` 仍存在且等于预期 head。
- `users` 行数、用户 ID 集合和逐行身份指纹与重置前完全一致。
- 原用户仍能使用原密码登录，角色和默认工作区不变。
- 用户默认工作区和唯一 `SYSTEM_SHARED` 工作区仍存在，ID 不变。
- 文件域表没有旧文件、工作副本、分类运行、检索投影、会话附件和异步任务。
- 旧会话、消息、AgentRun 和文件操作计划不再出现，避免引用已删除文件。
- 所有目标目录存在且内容为空。
- 外部受管原始目录内容和哈希未改变。
- taxonomy 配置仍能加载，全部有效分类数量与重置前一致。
- watcher/reconcile 未在测试开始前重新导入外部文件。
- 原测试账号可以直接登录并创建全新会话，无需重新注册。

## 6. 测试数据集模型

### 6.1 `classification_test_datasets`

```text
id UUID
name varchar
description text
status DRAFT | ACTIVE | COMPLETED | ARCHIVED
taxonomy_key varchar
taxonomy_version varchar
classifier_version varchar
calibration_version varchar
policy_version varchar
configuration_snapshot_json jsonb
created_by UUID
created_at timestamptz
started_at timestamptz nullable
completed_at timestamptz nullable
```

`configuration_snapshot_json` 只能保存非敏感开关、阈值版本和 Provider 名称，不保存 API key、正文或绝对路径。

### 6.2 `classification_test_dataset_items`

```text
id UUID
dataset_id UUID
source_item_key varchar
upload_document_id UUID nullable
document_version_id UUID nullable
working_copy_id UUID nullable
content_sha256 varchar
expected_primary_category_id varchar
expected_secondary_category_ids_json jsonb
expected_evidence_json jsonb
filename_group NATURAL | WEAKENED
sample_group STANDARD | AMBIGUOUS | NEGATIVE | OTHER
status PENDING | UPLOADED | READY | FAILED
created_at timestamptz
```

约束：

- `dataset_id + source_item_key` 唯一。
- 同一个工作副本可以关联多个测试数据集，不能把 `test_dataset_id` 直接写成 `WorkingCopy` 的唯一归属字段。
- 标准答案只能供评测读取，不能进入分类 Prompt、taxonomy signals 或候选召回输入。
- 原始分类目录可以用于生成 `expected_primary_category_id`，但上传时不能把该目录路径传给分类器。

### 6.3 `classification_test_executions`

```text
id UUID
dataset_id UUID
status PLANNED | RUNNING | COMPLETED | PARTIAL | FAILED
taxonomy_version varchar
classifier_version varchar
calibration_version varchar
policy_version varchar
graph_mode off | shadow | enabled
configuration_snapshot_json jsonb
started_at timestamptz
completed_at timestamptz nullable
```

一次数据集可以运行多个 execution，用于比较不同规则、阈值和分类器版本。

### 6.4 `classification_test_results`

```text
id UUID
execution_id UUID
dataset_item_id UUID
classification_run_id UUID nullable
organization_decision_id UUID nullable
predicted_primary_category_id varchar nullable
predicted_category_ids_json jsonb
decision AUTO_ORGANIZED | NEEDS_REVIEW | FAILED
top1_correct boolean nullable
top3_contains_expected boolean nullable
placement_correct boolean nullable
review_expected boolean nullable
metrics_json jsonb
created_at timestamptz
```

结果必须绑定确切 `classification_run_id`，不能简单读取“当前最新分类”，否则重跑后无法复现历史报告。

## 7. 查询隔离规则

### 7.1 工作副本查询

测试文件树和表格应以显式关联过滤：

```sql
SELECT wc.*
FROM working_copies wc
JOIN classification_test_dataset_items item
  ON item.working_copy_id = wc.id
WHERE item.dataset_id = :test_dataset_id
  AND wc.status = 'ACTIVE';
```

实际实现继续使用 SQLAlchemy/Pydantic 服务边界，不把 SQL 交给 LLM 或前端。

### 7.2 分类结果查询

评测不得按 `document_id` 猜测最新运行，必须从：

```text
test execution
-> test result
-> classification_run_id
-> category suggestions
-> organization_decision_id
```

读取本次确切结果。

### 7.3 主分类树统计

主分类树统计必须先限定 `test_dataset_id`，再按生效主分类聚合：

- 每个 `working_copy_id` 只计数一次。
- 次级分类不造成跨节点重复。
- `NEEDS_REVIEW` 进入虚拟“待确认主分类”节点。
- `SKIPPED` 进入“保持中性位置”节点。
- 节点数量和文件表格必须使用同一数据集过滤条件。

### 7.4 权限边界

- `test_dataset_id` 只是查询范围，不授予文件访问权限。
- 后端仍需同时校验用户角色、工作区和文件可读性。
- 测试数据集管理建议只开放给 `admin/ops` 或显式测试角色。
- 普通用户默认全局搜索和对话附件范围不得被测试数据集隐式改变。

## 8. API 与前端测试入口

建议提供等价能力：

```text
POST /api/admin/classification-test-datasets
GET  /api/admin/classification-test-datasets
POST /api/admin/classification-test-datasets/{dataset_id}/items
POST /api/admin/classification-test-datasets/{dataset_id}/executions
GET  /api/admin/classification-test-datasets/{dataset_id}/files
GET  /api/admin/classification-test-datasets/{dataset_id}/category-tree
GET  /api/admin/classification-test-executions/{execution_id}/results
GET  /api/admin/classification-test-executions/{execution_id}/metrics
```

前端测试页面应提供：

- 当前测试数据集选择器。
- taxonomy 全分类覆盖进度，例如 `已上传样本分类数 / 全部有效分类数`。
- 主分类树和当前数据集文件表格。
- 预期分类、实际分类、Top1/Top3、自动落位或复核状态。
- 每个分类的样本数、正确数、错误数、复核数和混淆对象。
- 自然文件名组与弱化文件名组对比。
- taxonomy、分类器、校准器、策略和图谱模式版本。

普通 `/chat` 和普通文件树不必暴露测试数据集内部对象。

## 9. 全分类测试流程

### 9.1 准备标准答案

为 taxonomy 中每个有效分类建立 manifest：

```text
source_item_key
expected_primary_category_id
expected_secondary_category_ids
filename_group
sample_group
notes
```

建议每个分类至少 5 个不同文件；首轮链路检查至少 1 个。重要或容易混淆的分类准备 10～20 个。

### 9.2 上传要求

- 从统一中性入口上传，不能让原分类目录路径进入分类器。
- 保留一组自然文件名，验证真实体验。
- 另准备弱化文件名组，验证正文分类能力。
- 同一内容哈希不要无意中同时标成不同主分类；需要测试歧义时应明确标记 `AMBIGUOUS`。
- 每批上传保存稳定文档和工作副本 ID 到数据集 item。

### 9.3 等待完成

每个 item 必须等待以下状态终结：

```text
内部原件保护完成
工作副本 ACTIVE 或明确 FAILED
正文抽取完成或明确失败
分类运行完成或明确失败
组织决策完成
搜索投影 READY 或明确失败
```

不能在异步任务仍运行时统计最终分类效果。

### 9.4 评测指标

全局和逐分类至少报告：

```text
样本数
Top1 accuracy
Top3 recall
auto-placement precision
auto-placement coverage
review rate
parse failure rate
evidence missing rate
placement correctness
confusion matrix
```

“用户未纠正”不能当成正确。正确性必须与独立 manifest 对比。

## 10. 为什么推荐混合方案

只清库的问题：

- 每轮都要重建账号、会话和全部处理数据。
- 无法方便比较两个分类器版本。
- 破坏性高，操作成本高。

只加查询条件的问题：

- 旧数据仍可能影响分类过程。
- 时间和用户条件无法正确覆盖共享工作副本复用。
- 很难准确绑定一次分类运行。

混合方案的优势：

- 首轮文件域受控重置在保留用户身份的同时提供可信干净基线。
- 后续 `test_dataset_id + execution_id` 提供稳定、可复现、可比较的测试范围。
- 全部分类始终开放，测试数据集只限定文件集合，不缩小 taxonomy 候选空间。
- 普通用户文件事实、测试标准答案和评测结果保持边界清晰。

## 11. 实施阶段

### 阶段 0：只执行现状审计

- 输出业务表计数和存储目标清单。
- 确认当前环境不是生产。
- 生成 KEEP/CLEAR/REBUILD manifest，确认用户、工作区和非文件设置属于 KEEP。
- 生成用户身份逐行指纹，作为重置后强制比对基线。
- 确认外部目录、Neo4j 和 watcher 策略。

本阶段不删除任何内容。

### 阶段 1：受控干净重置

- 实现文件域专用受控重置模式及其安全测试，禁止调用现有全量清表逻辑。
- 停止写入进程。
- 可选备份。
- 用户再次确认精确范围。
- 执行文件域受控重置。
- 完成重置后验证用户身份指纹、工作区、文件表和存储目录。
- 关闭外部自动导入，使用原有账号登录并创建新测试会话。

### 阶段 2：完成当前全分类基线

- 上传覆盖全部分类的文件。
- 使用独立 manifest 作为标准答案。
- 完成首次分类、落位和前端分类树检查。
- 导出逐分类报告。

### 阶段 3：实现测试数据集隔离

- 新增四张测试数据集表和迁移。
- 上传/关联流程保存 dataset item。
- 分类执行绑定 execution 和 result。
- 文件树、表格、分类结果和指标支持 `test_dataset_id`。
- 增加 admin/ops 测试页面。

### 阶段 4：后续重复测试

- 不再默认清空文件域或全库。
- 每次创建新 dataset/execution。
- 对相同文件重跑不同版本并比较结果。
- 旧数据集只读归档，按显式开发清理策略统一处理。

## 12. 测试要求

### 12.1 重置脚本测试

- 无显式确认参数时拒绝执行。
- `users` 和 `workspaces` 不在 KEEP 清单或意外进入删除依赖时拒绝执行。
- 出现未分类的新业务表时拒绝执行。
- 数据库不可连接时不清文件目录。
- 路径为空、根目录、项目根或与外部目录重叠时拒绝执行。
- 保留所有用户、密码哈希、角色、默认工作区、`alembic_version` 和外部受管目录。
- 清空后原账号可以登录，唯一共享工作区和用户默认工作区 ID 不变。
- 文件表、旧会话、文件任务和文件存储清空，不存在指向已删除文件的附件或审计记录。
- watcher 未停止时前置检查应警告或阻止执行。

### 12.2 数据集隔离测试

- 两个数据集包含不同文件时，树、表格和指标不串数据。
- 同一工作副本可关联两个数据集，但每个数据集内只计数一次。
- 同一数据集的两个 execution 可以绑定不同分类运行并分别复现。
- 数据集过滤不改变完整 taxonomy 候选空间。
- `test_dataset_id` 不绕过文件访问权限。
- 标准答案不会进入分类服务、Prompt 或证据。
- `NEEDS_REVIEW` 文件在数据集树中可见、可读取并只计数一次。

### 12.3 端到端验收

- 重置前后用户数量、ID、用户名、邮箱、密码哈希、角色和默认工作区指纹完全一致。
- 原有用户使用原密码登录成功，不需要重新注册。
- 重置后旧工作副本、分类建议、检索投影和上传内容不再出现。
- 外部受管目录没有被删除或修改。
- watcher/reconcile 不会在测试过程中重新导入旧文件。
- taxonomy 全部有效分类仍可加载并参与候选召回。
- 新测试文件只出现在当前数据集的分类树和表格中。
- 每个结果可追溯到确切文件、标准答案、分类运行和组织决策。

## 13. 已确定的最终执行边界

针对当前“保留用户信息并使用全分类的新文件确认分类效果”的目标，执行边界确定为：

```text
文件域受控干净重置，保留 users/workspaces 和必要非文件设置
+ 清除旧会话与文件任务上下文
+ 清除文件、分类、索引、工作副本和文件存储
+ 暂停外部目录自动导入
+ 使用原账号创建新会话
+ 完整 taxonomy 全分类测试
+ 后续补充 test_dataset_id/execution_id 隔离
```

现有全量业务表重置命令本次禁止原样执行。后续必须先实现并验证文件域专用范围，确认用户身份指纹不会变化，再请求实际清空授权。

在用户后续明确授权前，不执行任何删除、清空、配置修改或代码修改。
