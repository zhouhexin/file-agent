# 受管原始目录、工作副本目录与回收站目录实施方案

- 状态：第一阶段已实现，后续继续补充独立分析队列和摘要检索索引
- 编写日期：2026-07-20
- 修订日期：2026-07-21
- 适用范围：受管原始目录中的文件导入、同步，以及工作副本的增删改查
- 架构边界：继续使用 Agent Runtime、白名单 Tool、OperationPlan、ChangeSet 和 StorageService

## 1. 目标

本方案将受管文件生命周期固定为三层：

```text
受管原始目录
→ 工作副本目录
→ 回收站目录
```

方案目标：

1. 受管原始目录对 Agent、Planner、用户接口和普通 Tool 始终只读；只有系统归档服务可以追加新的原始文件。
2. 服务启动时自动提交受管原始目录扫描和工作副本补齐任务，并由异步worker为尚无主导入工作副本的原始文件自动创建工作副本，不由用户发起或确认。
3. Agent 的读取、搜索、分类、重命名、移动、复制、修改和删除默认针对工作副本。
4. 删除工作副本时先进入回收站目录，避免误删后无法恢复。
5. 修改工作副本时保存旧的工作副本版本，避免覆盖后无法恢复。
6. 原始文件在外部发生变化时只更新同步状态，不自动覆盖工作副本。
7. 对话消息引用、工作副本生命周期和原始文件生命周期相互解耦。
8. 所有高风险操作继续经过 OperationPlan 确认，并生成 ChangeSet 和逐文件回执。
9. 每个上传附件在通过安全扫描和重复文件检查后都必须进入归档决策流程；没有重复候选时自动归档，存在重复候选时必须通过对话逐文件告知用户并等待是否继续上传的确认。
10. 用户确认继续上传后，附件才允许异步归档为原始文件并异步导入工作副本；用户选择使用已有文件或取消上传时，不得创建新的原始文件和工作副本。
11. 实时事件和定时全量同步共同保证受管原始目录覆盖所有已确认归档且校验成功的上传附件，以及部署时已经存在的全部原始文件。
12. 上传归档、原始文件导入、启动同步、定时同步和大批量扫描必须由持久化异步任务执行，API和AgentRun请求线程只创建任务并立即返回。
13. 异步归档和异步导入必须与聊天、查询、下载和其他正常程序能力隔离资源，不能阻塞服务启动或占满API进程。

本方案不增加额外的第四层受管文件存储。上传暂存只用于接收、安全扫描和失败重试，不是长期原始文件存储；OCR、预览、抽取文本等派生件仍遵守项目既有存储规则，但不属于本方案定义的受管文件三层生命周期。

## 2. 统一名词

后续设计文档、代码注释、数据库字段说明、API、Tool、前端和日志统一使用以下名词。

| 统一名词 | 英文代码名 | 定义 |
|---|---|---|
| 受管原始目录 | `ManagedRoot` | 保存所有原始文件的受保护目录；对 Agent 和用户接口只读，仅系统归档服务可以追加新文件 |
| 原始文件 | `ManagedFile` | 受管原始目录中的文件 |
| 工作副本目录 | `WorkingCopyRoot` | File Agent 可以进行增删改查的目录 |
| 工作副本 | `WorkingCopy` | 从原始文件复制出来、供 Agent 操作的文件 |
| 工作副本版本 | `DocumentVersion` | 工作副本发生内容修改时产生的文件内容版本 |
| 工作副本路径记录 | `WorkingCopyPathRecord` | 工作副本每次重命名或移动的数据库审计记录 |
| 回收站目录 | `TrashRoot` | 保存被删除或被替换的工作副本内容 |
| 回收站条目 | `TrashEntry` | 一次可恢复的删除或版本替换记录 |
| 归档 | `Archive` | 系统把无重复候选或经用户确认继续上传的安全附件追加为原始文件 |
| 导入 | `Import` | 系统把尚无工作副本的原始文件复制为工作副本 |
| 同步 | `Reconcile` | 系统检查原始文件、上传归档任务和工作副本覆盖关系并补齐缺失项 |
| 重复文件检查 | `DuplicateCheck` | 系统根据内容哈希和受控相似度在允许范围内查找重复或相似文件 |
| 重复候选 | `DuplicateFileCandidate` | 重复文件检查生成的候选，仅表示可能重复，不等于系统已经合并文件 |
| 重复上传确认 | `DuplicateUploadReview` | 系统通过对话等待用户决定继续上传、使用已有文件或取消上传的确认记录 |

名词使用约束：

- `ManagedRoot` 在产品文案中统一称为“受管原始目录”。
- `ManagedFile` 在产品文案中统一称为“原始文件”。
- 可被 Agent 修改的文件统一称为“工作副本”。
- 重命名和移动产生的历史统一称为“工作副本路径记录”。
- 删除和替换产生的可恢复记录统一称为“回收站条目”。
- “原件库”不是独立第四层；用户口头所称“原件库”在本文统一使用“受管原始目录”。
- “归档”只表示通过重复文件检查或经用户确认的上传附件进入受管原始目录；“导入”只表示从原始文件创建工作副本；“同步”表示检查并补齐归档、索引和工作副本覆盖关系。
- “重复候选”不得简称为“重复文件事实”；相似度结果只用于请求用户确认，不能自动删除、覆盖、合并或关联其他用户文件。

## 3. 核心不变量

实现过程中必须始终满足以下不变量：

1. 受管原始目录必须对 API、Agent Runtime、Planner、普通 Tool 和工作副本 worker 以只读方式挂载。
2. 系统归档 worker 可以通过独立写入挂载点追加上传原始文件，但不得覆盖、重命名、移动或删除已经归档的原始文件。
3. Agent、Planner 和面向 Agent 的普通 Tool 不得生成修改原始文件的动作；仅系统生命周期 Tool `upload-archive` 可以通过归档服务追加新原始文件。
4. 每个工作副本的 `managed_file_id` 必须非空并指向对应原始文件，包括由上传附件归档、用户复制或进入回收站目录的工作副本。
5. 服务启动同步和周期同步必须自动为尚未建立主导入工作副本的原始文件创建工作副本，不生成 OperationPlan，也不等待用户确认。
6. 每个通过安全扫描的上传附件必须先进入重复文件检查队列；无重复候选或用户确认继续上传后才能进入归档队列。归档完成前只能处于暂存或等待确认状态，不能创建无对应原始文件的工作副本。
7. 工作副本是 Agent 文件操作的默认对象。
8. 工作副本进入回收站目录后，原始文件保持不变。
9. 工作副本进入回收站目录后，对话引用的 Document、工作副本版本、解析结果和证据继续保留。
10. 重命名和移动只改变工作副本路径，不产生新的工作副本版本，但必须写入详细的工作副本路径记录。
11. 当前工作副本路径以最后一条状态为 `COMPLETED` 的工作副本路径记录更新时间为准。
12. 修改和覆盖必须产生新的工作副本版本。
13. 普通删除必须生成回收站条目，不能直接永久删除文件内容。
14. MVP 不自动永久删除回收站条目。
15. 原始文件发生变化时不得静默覆盖工作副本。
16. “受管原始目录最全”表示原始文件集合覆盖全部允许归档的有效上传和部署文件；“允许归档”仅指未发现重复候选或用户已明确选择继续上传，不表示工作副本的后续修改会反向覆盖原始文件。
17. 文件系统写入必须经过 StorageService 或等价受控服务，不能由 LLM、Planner 或普通 Graph 节点直接执行。
18. 高风险操作必须由已确认的 OperationPlan 驱动；无重复候选的系统自动归档和系统自动导入是幂等的保护性复制，不属于高风险用户文件操作。存在重复候选时必须完成重复上传确认，但重复上传确认不等同于OperationPlan。
19. 每个操作对象必须产生独立的执行状态和逐文件回执。
20. 重复文件检查、归档、导入、扫描和同步只能在持久化异步worker中执行；API、AgentGraph节点、上传请求和服务启动钩子不得同步复制大文件、遍历目录或等待任务完成。
21. 重复上传确认必须按上传附件版本逐文件保存，批次中的一个待确认文件不得阻塞其他无重复候选文件进入异步归档。
22. 跨用户重复候选只能返回脱敏提示和匹配类型，不得返回其他用户的身份、文件名、目录、正文、分类、对话或业务对象ID。
23. 用户确认继续上传后必须创建独立可追溯的原始文件和工作副本关系，不得因为SHA-256相同而借用其他用户无权访问的Document或WorkingCopy。
24. 定时同步不得绕过`WAITING_DUPLICATE_CONFIRMATION`状态自动归档或导入待确认上传。

## 4. 物理目录结构

推荐目录结构：

```text
# 第一层：受管原始目录，对 Agent 只读，仅系统归档服务可追加。
/managed-original/
└── school-files/
    ├── 奖学金/
    │   └── 申请表.docx
    └── 活动材料/
        └── 活动总结.pdf

# 第二层：工作副本目录，可读写。
/managed-working/
└── <workspace_id>/
    └── school-files/
        ├── 奖学金/
        │   └── 国家励志奖学金申请表.docx
        └── 活动材料/
            └── 2026年活动总结.pdf

# 第三层：回收站目录，只能通过受控服务访问。
/managed-trash/
└── <workspace_id>/
    └── <trash_entry_id>/
        ├── content/
        │   └── 申请表.docx
        └── manifest.json
```

目录规则：

- 受管原始目录可以位于本机、NAS 或受控挂载点；API、Agent 和普通 worker 只能只读访问，系统归档 worker 使用独立身份和写入挂载点执行仅追加写入。
- 已归档原始文件采用不可变路径；同名冲突时系统生成稳定唯一归档路径，不允许覆盖既有原始文件。
- 工作副本目录由 StorageService 管理，并按 `workspace_id + root_key` 隔离。
- 回收站目录由 StorageService 管理，不允许用户直接拼接绝对路径访问。
- 工作副本目录和回收站目录应位于同一文件系统，以便优先使用原子移动。
- 临时文件必须位于目标目录同一文件系统内，避免跨文件系统移动破坏原子性。
- 隐藏文件、符号链接、路径穿越和越界路径必须由 PathPolicy 拒绝或隔离处理。

## 5. 配置设计

建议新增：

```env
# 受管原始目录。
MANAGED_ROOT_SCHOOL_FILES=/managed-original/school-files
MANAGED_ROOT_SCHOOL_FILES_CLASSIFICATION_MODE=NONE

# 系统归档 worker 使用的独立仅追加写入入口；不得提供给 API、Agent 或普通 Tool。
MANAGED_ROOT_ARCHIVE_WRITE_PATH=/managed-original-archive-write/school-files
MANAGED_ROOT_ARCHIVE_ENABLED=true

# 所有工作副本目录的统一根路径。
WORKING_COPY_STORAGE_ROOT=/managed-working

# 所有回收站目录的统一根路径。
TRASH_STORAGE_ROOT=/managed-trash

# 受管原始目录实时监听与定时同步。
MANAGED_ROOT_WATCH_ENABLED=true
MANAGED_ROOT_RECONCILE_INTERVAL_SECONDS=300
MANAGED_ROOT_RECONCILE_ON_STARTUP=true

# 上传附件通过安全扫描后先异步查重；无重复候选或用户确认继续后才异步归档。
UPLOAD_ARCHIVE_ENABLED=true
UPLOAD_ARCHIVE_RETRY_INTERVAL_SECONDS=300

# 重复文件检查与对话确认。
UPLOAD_DUPLICATE_CHECK_ENABLED=true
UPLOAD_DUPLICATE_SIMILARITY_THRESHOLD=0.90
UPLOAD_DUPLICATE_MAX_CANDIDATES=5
UPLOAD_DUPLICATE_CONFIRMATION_TTL_HOURS=168

# 异步归档、导入和操作批次；API进程不得执行这些I/O任务。
FILESYSTEM_ASYNC_JOBS_ENABLED=true
FILESYSTEM_JOB_LEASE_SECONDS=120
ARCHIVE_WORKER_CONCURRENCY=2
IMPORT_WORKER_CONCURRENCY=2
WORKING_COPY_IMPORT_BATCH_SIZE=100
WORKING_COPY_OPERATION_BATCH_SIZE=20

# 回收站保留期。MVP 到期后只标记可清理，不自动永久删除。
TRASH_RETENTION_DAYS=30
TRASH_AUTO_PURGE_ENABLED=false
```

配置迁移规则：

- 废弃 `MANAGED_ROOT_<KEY>_ALLOW_RENAME`。
- 受管原始目录不再提供面向 Agent 或用户接口的写操作开关。
- `MANAGED_ROOT_ARCHIVE_WRITE_PATH` 只能配置给系统归档 worker，API、Agent Runtime、Planner 和普通 Tool 进程不得读取或挂载该写入口。
- `MANAGED_ROOT_RECONCILE_ON_STARTUP` 必须保持 `true`，确保每次服务启动都会扫描、索引并自动补齐工作副本。
- `MANAGED_ROOT_RECONCILE_ON_STARTUP=true`只表示启动时创建异步同步任务，不能在启动钩子内等待扫描、归档或导入完成。
- `UPLOAD_DUPLICATE_SIMILARITY_THRESHOLD`只能由admin/ops通过受控配置调整，修改后必须记录配置审计和评测版本。
- `UPLOAD_DUPLICATE_CHECK_ENABLED`在生产环境必须保持`true`；关闭时服务必须拒绝启动上传归档worker，不能静默跳过查重直接归档。
- `ARCHIVE_WORKER_CONCURRENCY`和`IMPORT_WORKER_CONCURRENCY`必须设置独立上限，不能复用API进程的请求并发数。
- 工作副本允许的操作由应用层白名单和用户角色共同控制。
- `TRASH_AUTO_PURGE_ENABLED` 在 MVP 中必须保持 `false`。
- 如果启动方式、挂载路径或 worker 进程发生变化，必须同步更新 `.env.example`、`README.md` 和 `docs/runbook.md`。

## 6. 数据模型

### 6.1 受管原始目录

保留现有 `managed_roots`，语义固定为受管原始目录：

```text
managed_roots
- id
- root_key
- display_name
- container_path
- classification_mode
- enabled
- read_only
- archive_write_enabled
- last_reconciled_at
- created_by
- created_at
- updated_at
```

约束：

- `read_only` 固定为 `true`，表示 Agent、用户接口和普通 Tool 的访问边界。
- `archive_write_enabled` 只允许系统归档服务追加上传原始文件，不赋予覆盖、改名、移动或删除已有原始文件的权限。
- `allowed_operations_json` 只允许 `scan`、`list`、`search` 和 `read` 等只读能力。
- 受管原始目录配置仍以部署环境为事实源。

### 6.2 原始文件

保留现有 `managed_files`，语义固定为原始文件索引：

```text
managed_files
- id
- root_id
- relative_path
- relative_path_hash
- category_path
- filename
- extension
- size_bytes
- modified_at
- fingerprint
- content_sha256
- file_identity
- source_type
- source_upload_version_id
- archived_at
- status
- last_seen_scan_run_id
- created_at
- updated_at
```

建议状态：

```text
ACTIVE
MISSING
```

约束：

- Agent 操作不得修改 `managed_files.relative_path`。
- 只有系统归档、受管原始目录扫描和同步服务可以创建或更新原始文件索引。
- `source_type` 至少支持 `DEPLOYED_FILE` 和 `UPLOAD_ARCHIVE`；上传归档形成的原始文件必须保存 `source_upload_version_id`。
- `source_upload_version_id` 对非空值添加唯一约束，保证同一个上传附件版本只归档一次。
- 已归档文件的内容和路径不可变；每个新的上传附件版本都创建独立原始文件记录和稳定归档路径。相同文件名或相同 SHA-256 只能标记为疑似重复，不能合并原始文件关系或覆盖既有文件。
- `file_identity` 可以保存本地文件系统的 device 与 inode，仅用于辅助识别重命名和移动。
- `content_sha256` 是内容版本的可靠标识；`fingerprint` 只用于快速判断是否需要重新计算 SHA-256。

### 6.3 工作副本目录

新增 `working_copy_roots`：

```text
working_copy_roots
- id
- workspace_id
- managed_root_id
- root_key
- relative_storage_path
- status
- last_imported_at
- last_reconciled_at
- created_at
- updated_at
```

建议状态：

```text
INITIALIZING
READY
FAILED
```

唯一约束：

```text
(workspace_id, managed_root_id)
```

### 6.4 工作副本

新增 `working_copies`：

```text
working_copies
- id
- working_copy_root_id
- workspace_id
- managed_file_id
- document_id
- current_version_id
- relative_path
- relative_path_hash
- filename
- extension
- size_bytes
- content_sha256
- imported_source_sha256
- is_primary_import
- status
- sync_status
- last_operation_plan_id
- created_at
- updated_at
```

字段规则：

- `managed_file_id` 不允许为空；从上传附件创建工作副本前，必须先把附件归档为原始文件，用户复制工作副本时必须继承对应原始文件。
- 对历史无原始文件工作副本，迁移任务必须先归档其初始上传附件版本并回填 `managed_file_id`，迁移完成后再添加非空约束。
- `document_id` 表示稳定业务文档。
- `current_version_id` 指向当前工作副本版本。
- `imported_source_sha256` 表示创建工作副本时使用的原始文件内容版本。
- `content_sha256` 表示当前工作副本内容版本。
- 自动导入创建的工作副本设置 `is_primary_import = true`；用户后续复制产生的工作副本设置为 `false`。
- 同一个 `working_copy_root_id + managed_file_id` 只能有一个主导入工作副本；即使该工作副本进入回收站目录，也必须保留该映射，周期同步不得自动重新创建。

建议状态：

```text
IMPORTING
ACTIVE
TRASHED
CONFLICT
FAILED
```

建议同步状态：

```text
SYNCED
ORIGINAL_CHANGED
ORIGINAL_MISSING
```

活动工作副本唯一约束：

```text
(workspace_id, working_copy_root_id, relative_path_hash)
WHERE status = 'ACTIVE'
```

主导入工作副本唯一约束：

```text
(working_copy_root_id, managed_file_id)
WHERE is_primary_import = true
```

### 6.5 工作副本版本

新增或正式启用 `document_versions`，语义统一为工作副本版本和上传附件版本：

```text
document_versions
- id
- document_id
- version_number
- parent_version_id
- working_copy_id
- storage_tier
- storage_path
- filename
- content_type
- size_bytes
- sha256
- source_type
- source_managed_file_id
- operation_plan_id
- created_by
- created_at
```

建议 `storage_tier`：

```text
WORKING_COPY
TRASH
UPLOAD
```

建议 `source_type`：

```text
IMPORT
UPDATE
RESTORE
UPLOAD
```

约束：

- 重命名或移动不产生新的工作副本版本。
- 修改或覆盖必须产生新的工作副本版本。
- 已进入对话的工作副本版本不能因为工作副本进入回收站目录而删除。
- 工作副本版本内容只能通过 StorageService 定位。

### 6.6 工作副本路径记录

新增 `working_copy_path_records`，逐文件保存每次重命名和移动的详细数据库记录：

```text
working_copy_path_records
- id
- working_copy_id
- sequence_number
- operation_type
- before_relative_path
- after_relative_path
- before_filename
- after_filename
- document_version_id
- content_sha256
- operation_plan_id
- operation_confirmation_id
- agent_run_id
- tool_invocation_id
- changeset_id
- change_item_id
- status
- error_code
- error_message
- executed_by
- created_at
- updated_at
```

建议 `operation_type`：

```text
RENAME
MOVE
```

建议状态：

```text
PLANNED
RUNNING
COMPLETED
FAILED
CANCELLED
STALE
```

约束与排序规则：

- 每个重命名或移动计划项都必须有一条工作副本路径记录，成功和失败都不能只写日志。
- `before_relative_path` 和 `after_relative_path` 必须保存完整相对路径，不能只保存 basename。
- `before_filename` 和 `after_filename` 必须保存文件名，便于前端直接展示重命名历史。
- `document_version_id` 和 `content_sha256` 必须指向操作时的当前工作副本版本，证明路径变化没有改变文件内容。
- `operation_plan_id`、`operation_confirmation_id`、`agent_run_id`、`tool_invocation_id`、`changeset_id` 和 `change_item_id` 必须形成完整审计链。
- `sequence_number` 在同一个 `working_copy_id` 内单调递增，并添加唯一约束 `(working_copy_id, sequence_number)`。
- `updated_at` 使用 `timestamptz`，由后端数据库事务的统一操作时间写入，不能采用 LLM 提供的时间。
- 当前工作副本路径以状态为 `COMPLETED`、`updated_at` 最新的工作副本路径记录为准；相同更新时间时以更大的 `sequence_number` 为准。
- 推荐查询顺序为 `updated_at DESC, sequence_number DESC`，并只使用 `COMPLETED` 记录计算当前路径。
- 状态进入 `COMPLETED`、`FAILED`、`CANCELLED` 或 `STALE` 后，工作副本路径记录不可修改；纠错必须新增工作副本路径记录，不能覆盖历史。
- `working_copies.relative_path`、`filename` 和 `updated_at` 是当前路径的查询缓存，必须与最后一条成功工作副本路径记录在同一数据库事务中更新。
- 成功操作时，`working_copies.updated_at` 与工作副本路径记录的 `updated_at` 必须使用同一个后端操作时间。
- 如果 `working_copies.relative_path` 与最后一条成功工作副本路径记录不一致，必须停止后续写操作并标记 `NEEDS_RECONCILIATION`。

### 6.7 回收站条目

新增 `trash_entries`：

```text
trash_entries
- id
- workspace_id
- working_copy_id
- document_version_id
- entry_type
- original_relative_path
- trash_relative_path
- status
- operation_plan_id
- deleted_by
- deleted_at
- retention_until
- restored_at
- purged_at
- created_at
- updated_at
```

建议 `entry_type`：

```text
DELETED
REPLACED_VERSION
```

建议状态：

```text
ACTIVE
RESTORED
ELIGIBLE_FOR_PURGE
PURGED
```

语义：

- `DELETED` 表示用户删除了工作副本。
- `REPLACED_VERSION` 表示修改工作副本前保存的旧内容。
- `ELIGIBLE_FOR_PURGE` 只表示可以创建永久删除计划，不表示已经删除。

### 6.8 文件系统事件

为实时监听和定时同步新增 `managed_file_events`：

```text
managed_file_events
- id
- root_id
- event_type
- source_relative_path
- target_relative_path
- observed_size
- observed_mtime
- origin
- deduplication_key
- status
- observed_at
- processed_at
- error_message
```

建议事件类型：

```text
CREATED
MODIFIED
MOVED
DELETED
```

建议来源：

```text
EXTERNAL
AGENT
SYSTEM_ARCHIVE
RECONCILIATION
```

监听回调只负责记录事件或创建任务，不得直接执行数据库批量更新、解析、分类或文件写操作。

### 6.9 上传归档记录

新增 `upload_archive_records`，用于驱动即时归档、定时补偿和受管原始目录完整性检查：

```text
upload_archive_records
- id
- upload_document_version_id
- managed_root_id
- managed_file_id
- archive_relative_path
- content_sha256
- status
- attempt_count
- next_retry_at
- last_error_code
- last_error_message
- filesystem_job_id
- changeset_id
- created_at
- updated_at
- archived_at
```

建议状态：

```text
DUPLICATE_CHECK_PENDING
DUPLICATE_CHECKING
WAITING_DUPLICATE_CONFIRMATION
PENDING
ARCHIVING
RETRY_WAIT
ARCHIVED
FAILED
QUARANTINED
CANCELLED
EXISTING_FILE_SELECTED
```

约束：

- `upload_document_version_id` 必须唯一，同一个上传附件版本只能有一条归档状态记录。
- 安全扫描成功后创建`DUPLICATE_CHECK_PENDING`记录并进入异步重复文件检查；安全扫描未通过时使用`QUARANTINED`，不得进入重复文件检查或归档worker。
- 没有重复候选时状态自动推进为`PENDING`；存在重复候选时必须进入`WAITING_DUPLICATE_CONFIRMATION`。
- 用户选择继续上传时推进为`PENDING`；选择取消上传时推进为`CANCELLED`；选择使用当前用户有权访问的已有文件时推进为`EXISTING_FILE_SELECTED`。
- `ARCHIVED` 状态必须同时具有非空 `managed_file_id`、`archive_relative_path`、`content_sha256` 和 `archived_at`。
- `ARCHIVED` 是归档成功事实，后续不得因为上传暂存清理而删除或回退。
- 周期补偿只重试`DUPLICATE_CHECK_PENDING`、`PENDING`、到期的`RETRY_WAIT`和可重试的`FAILED`记录；不得处理`WAITING_DUPLICATE_CONFIRMATION`、`CANCELLED`或`EXISTING_FILE_SELECTED`。
- 完整性检查必须同时比对 `upload_archive_records`、`managed_files.source_upload_version_id` 和受管原始目录实际文件，不能只相信单张数据库表。

### 6.10 重复上传确认

新增`upload_duplicate_reviews`：

```text
upload_duplicate_reviews
- id
- upload_document_version_id
- conversation_id
- workspace_id
- user_id
- status
- decision
- selected_existing_working_copy_id
- notification_message_id
- confirmation_message_id
- duplicate_check_job_id
- expires_at
- decided_at
- created_at
- updated_at
```

建议状态：

```text
CHECKING
WAITING_CONFIRMATION
RESOLVED
EXPIRED
FAILED
```

建议决策：

```text
CONTINUE_UPLOAD
USE_EXISTING_FILE
CANCEL_UPLOAD
```

约束：

- `upload_document_version_id`必须唯一，一个上传附件版本只能有一条有效重复上传确认。
- `USE_EXISTING_FILE`只允许选择当前用户在当前workspace中有权读取的活动工作副本。
- 跨用户重复候选不得进入`selected_existing_working_copy_id`，只能允许`CONTINUE_UPLOAD`或`CANCEL_UPLOAD`。
- 对话中的“继续上传”“使用已有文件”“取消上传”必须由后端绑定到确定的`upload_duplicate_review_id`和附件版本，LLM不得猜测确认对象。
- 确认决策必须幂等；同一确认重复提交不得重复创建归档任务或重复清理暂存文件。
- 到期未确认只把记录标记为`EXPIRED`并停止归档，暂存文件按独立保留策略清理；不得把超时视为用户已经取消或已经确认。

新增`upload_duplicate_candidates`：

```text
upload_duplicate_candidates
- id
- duplicate_review_id
- candidate_managed_file_id
- candidate_working_copy_id
- match_type
- match_scope
- similarity_score
- match_evidence_json
- user_visible_summary_json
- rank
- created_at
```

建议匹配类型：

```text
EXACT_SHA256
NEAR_DUPLICATE
```

建议匹配范围：

```text
SAME_WORKSPACE
SAME_USER
CROSS_USER
```

脱敏规则：

- `SAME_WORKSPACE`和`SAME_USER`候选只能在权限校验通过后展示用户本来就有权查看的文件名、工作副本路径和修改时间。
- `CROSS_USER`候选的`user_visible_summary_json`只能包含“检测到相同内容”或“检测到高度相似内容”、匹配类型和相似度区间，不能包含任何目标文件标识或内容摘要。
- 相似度只是确认提示，不是删除、合并、覆盖、拒绝上传或建立跨用户关系的依据。

### 6.11 持久化异步任务

现有`filesystem_jobs`需要支持不同I/O任务队列和可恢复租约，至少补充或确认以下字段：

```text
filesystem_jobs
- queue_name
- deduplication_key
- priority
- status
- progress_current
- progress_total
- attempt_count
- max_attempts
- available_at
- lease_owner
- lease_expires_at
- heartbeat_at
- payload_json
- result_json
- error_message
- created_at
- started_at
- finished_at
```

建议`queue_name`：

```text
DUPLICATE_CHECK
ARCHIVE
IMPORT
RECONCILE
FILE_OPERATION
```

API事务必须使用数据库事务内任务创建或Outbox等价机制，保证业务状态和任务记录不会出现一边提交、一边丢失。worker通过租约、心跳和幂等键领取任务；租约过期后任务可以安全重试。

## 7. 原始文件自动导入

新增文件系统任务类型：

```text
IMPORT_WORKING_COPIES
```

完整流程：

```text
服务启动完成基础设施检查
→ 创建 RECONCILE_UPLOAD_ARCHIVES 任务，补齐待归档上传
→ 为每个启用的受管原始目录创建 RECONCILE_MANAGED_ROOT 任务
→ 扫描并索引全部原始文件
→ 为每个尚无主导入工作副本的原始文件创建 IMPORT_WORKING_COPIES 任务
→ 独立 import worker 在隐藏临时文件上解析、生成双摘要、分类和规范名称
→ 原子提交最终名称和最终分类目录
→ 创建 WorkingCopy、初始 DocumentVersion 和初始路径记录
→ 写入 ChangeSet 和逐文件 ChangeItem
→ 更新启动同步状态和监控指标
```

自动导入规则：

- 已存在于受管原始目录的原始文件导入，以及已经通过重复上传确认的上传原始文件导入，属于系统生命周期任务，不由用户、Planner或Agent发起，不生成OperationPlan，也不再次等待用户确认。
- 服务启动钩子只负责在数据库和任务系统就绪后幂等创建同步任务，不得阻塞 API 进程完成大批量文件复制。
- API请求、AgentRun和同步任务本身只创建`IMPORT_WORKING_COPIES`任务；文件复制、哈希校验和数据库逐文件提交只能由独立import worker执行。
- 导入任务创建后立即返回`filesystem_job_id`和`PENDING`状态，用户可以继续聊天、查询其他文件和执行不依赖该工作副本的任务。
- 自动导入范围是每个启用的 `ManagedRoot -> WorkingCopyRoot` 映射；不得把同一个原始文件导入到未授权 workspace。
- 实时监听发现新增原始文件时立即自动导入；定时同步负责补齐服务停机、监听丢失或任务失败期间遗漏的主导入工作副本。
- 主导入工作副本进入回收站目录后仍视为已经导入，自动同步不得重新创建；用户需要时通过恢复流程恢复。
- 自动导入虽无需确认，仍必须写 `filesystem_jobs`、ToolInvocation 或等价系统调用记录、ChangeSet 和逐文件 ChangeItem。

单个原始文件的导入步骤：

1. 根据 `managed_file_id` 查询原始文件。
2. 校验原始文件状态为 `ACTIVE`。
3. 校验目标 WorkingCopyRoot 与 workspace 的系统配置关系。
4. 通过 PathPolicy 解析原始文件路径。
5. 读取复制前的大小、修改时间和 SHA-256。
6. 在工作副本目录的隐藏内部路径创建同文件系统临时文件；该文件不是用户可见WorkingCopy。
7. 复制原始文件内容到临时文件。
8. 校验临时文件 SHA-256。
9. 再次检查原始文件的大小和修改时间。
10. 如果原始文件在复制期间发生变化，删除临时文件并重试或标记失败。
11. 对临时文件执行受控解析，生成普通文档摘要和分类主题摘要。
12. 根据分类主题摘要生成主分类建议，并根据正文命名字段生成规范文件名。
13. 后端根据固定taxonomy和命名策略生成最终路径；LLM不得返回物理路径。
14. 使用原子移动把临时文件直接提交到最终名称和最终分类目录。
15. 创建 Document、DocumentVersion、WorkingCopy和`INITIAL_IMPORT`路径记录。
16. 写入`WORKING_COPY_IMPORTED`、双摘要和分类建议ChangeItem。
17. 返回工作副本稳定ID和最终文件信息；普通用户界面不展示原文件名、内部状态、Skill或Tool。

批量规则：

- 单个原始文件失败不得回滚已经成功的工作副本。
- 每个原始文件使用独立 savepoint 或等价隔离边界。
- 同一原始文件、workspace 和内容哈希的重复自动导入任务必须幂等。
- 批量导入必须分页处理并记录进度。
- 目标路径冲突时不得覆盖既有工作副本。低置信度或冲突降级必须使用基于`managed_file_id`的稳定内部路径，例如`待整理/<managed_file_id>/<filename>`；仍无法提交时标记`CONFLICT`并由周期同步重试或进入人工处理。

## 8. 受管原始目录同步

同步采用三个触发通道：

```text
服务启动同步
+
实时监听
+
定时全量同步
```

新增任务类型：

```text
RECONCILE_UPLOAD_ARCHIVES
RECONCILE_MANAGED_ROOT
```

服务启动时必须先幂等创建 `RECONCILE_UPLOAD_ARCHIVES`，再为每个启用的受管原始目录创建 `RECONCILE_MANAGED_ROOT`。实时监听建议使用独立 `watcher` 进程，通过跨平台文件事件库监听受管原始目录。定时全量同步由 scheduler 按相同顺序向 `filesystem_jobs` 写入任务，filesystem worker 继续负责实际处理。

服务启动、watcher和scheduler都只能创建异步任务，不得等待扫描、重复文件检查、归档或导入完成。API健康检查只反映服务和任务队列是否可用，不要求受管原始目录已经完成全量同步。

同步映射：

| 原始文件变化 | 原始文件处理 | 工作副本处理 |
|---|---|---|
| 新增 | 创建 ManagedFile | 自动创建工作副本 |
| 内容修改 | 更新 SHA-256 和状态 | 标记 `ORIGINAL_CHANGED` |
| 重命名或移动 | 更新 ManagedFile 路径 | 工作副本路径保持不变 |
| 删除 | 标记 `MISSING` | 标记 `ORIGINAL_MISSING`，工作副本继续可用 |
| 重新出现 | 恢复为 `ACTIVE` | 重新计算同步状态 |

同步规则：

- 原始文件内容修改后不得自动覆盖工作副本。
- 原始文件重命名后不得自动重命名工作副本。
- 原始文件删除后不得删除工作副本。
- 对每个状态为 `ACTIVE` 的原始文件，同步任务必须检查目标 WorkingCopyRoot 是否已经存在对应主导入工作副本；从未建立时自动补建，已经进入回收站目录或其他终态时不得自动重建。
- 对每个安全扫描成功但尚无`ManagedFile`的上传附件版本，同步任务必须先检查重复上传确认状态：无重复候选或已确认继续上传时重新创建归档任务；仍在`WAITING_DUPLICATE_CONFIRMATION`时只保持等待并重新发送必要通知，不得绕过确认。
- “受管原始目录最全”的完整性检查以成功上传附件版本和部署文件扫描结果为输入，分别输出已覆盖、待归档、归档失败、已索引、待导入和导入失败数量。
- 实时监听事件必须去重、合并并延迟处理，避免在大文件尚未写完时导入。
- 服务启动、监听器启动、事件队列溢出和周期到期时必须执行全量同步。
- 每个受管原始目录只能存在一个 `PENDING` 或 `RUNNING` 的全量同步任务。

用户确认使用新的原始文件更新工作副本时：

```text
生成 OperationPlan
→ 保存当前工作副本为 REPLACED_VERSION 回收站条目
→ 复制新的原始文件
→ 创建新的 DocumentVersion
→ 更新 WorkingCopy.current_version_id
→ 更新 WorkingCopy.sync_status = SYNCED
→ 写入 ChangeSet 和逐文件回执
```

## 9. 工作副本操作

### 9.1 查询、读取、搜索和分类

以下能力默认只针对工作副本：

```text
工作副本列表
工作副本搜索
工作副本正文读取
工作副本分类
工作副本证据回答
工作副本关系查询
```

原始文件只用于：

```text
检查原始文件是否存在
检查原始文件是否发生变化
查看工作副本与原始文件的关系
创建或刷新工作副本
```

### 9.2 重命名

```text
用户请求重命名
→ 生成重命名建议
→ 创建 OperationPlan
→ 用户确认
→ 锁定 WorkingCopy
→ 校验工作副本路径、当前版本和 SHA-256
→ 重命名工作副本
→ 在同一数据库事务中更新 working_copies 当前路径缓存
→ 写入 COMPLETED 工作副本路径记录
→ 写入 FILENAME_CHANGED ChangeItem
→ 返回逐文件回执
```

重命名规则：

- 原始文件不变化。
- 工作副本版本不增加。
- 每次重命名必须记录完整的变更前后相对路径、变更前后文件名、当前工作副本版本、内容 SHA-256、执行人、更新时间和完整审计链 ID。
- 当前工作副本路径以最后一条 `COMPLETED` 工作副本路径记录的 `updated_at` 为准；更新时间相同时以更大的 `sequence_number` 为准。
- 重命名失败也必须写入 `FAILED` 工作副本路径记录和失败 ChangeItem，不能只写运行日志。
- 目标路径冲突时当前工作副本失败，批次其他工作副本继续执行。

### 9.3 移动

```text
用户请求移动
→ 创建 OperationPlan
→ 用户确认
→ 校验目标目录和冲突
→ 移动工作副本
→ 在同一数据库事务中更新 working_copies 当前路径缓存
→ 写入 COMPLETED 工作副本路径记录
→ 写入 FILE_MOVED ChangeItem
→ 返回逐文件回执
```

移动规则：

- 原始文件不变化。
- 工作副本版本不增加。
- 每次移动必须记录完整的变更前后相对路径、变更前后文件名、当前工作副本版本、内容 SHA-256、执行人、更新时间和完整审计链 ID。
- 当前工作副本路径以最后一条 `COMPLETED` 工作副本路径记录的 `updated_at` 为准；更新时间相同时以更大的 `sequence_number` 为准。
- 移动失败也必须写入 `FAILED` 工作副本路径记录和失败 ChangeItem，不能只写运行日志。
- 不允许移动到工作副本目录之外。

### 9.4 复制

```text
用户请求复制
→ 创建 OperationPlan
→ 用户确认
→ 创建新的 WorkingCopy
→ 复制当前工作副本内容
→ 创建新的 Document 和初始 DocumentVersion
→ 写入 FILE_COPIED
→ 返回逐文件回执
```

复制创建的工作副本必须保留同一个 `managed_file_id` 以追溯原始文件，并设置 `is_primary_import = false`，不得取代系统自动创建的主导入工作副本映射。

### 9.5 修改或覆盖

```text
用户请求修改
→ Tool 在临时区域生成修改结果
→ 创建 OperationPlan，展示修改前后信息
→ 用户确认
→ 把旧工作副本内容移入回收站目录
→ 创建 REPLACED_VERSION 回收站条目
→ 原子提交新的工作副本内容
→ 创建新的 DocumentVersion
→ 更新 WorkingCopy.current_version_id
→ 写入 WORKING_COPY_VERSION_CREATED
→ 触发重新解析、分类、embedding 和图投影
→ 返回逐文件回执
```

修改规则：

- 原始文件不变化。
- 旧工作副本内容必须可恢复。
- 修改结果校验失败时不得替换当前工作副本。

### 9.6 删除

```text
用户请求删除
→ 创建 OperationPlan
→ 用户确认
→ 工作副本移入回收站目录
→ WorkingCopy.status = TRASHED
→ 创建 DELETED 回收站条目
→ 写入 FILE_TRASHED
→ 返回逐文件回执
```

删除规则：

- 原始文件不变化。
- 不允许通过普通删除直接调用永久删除。
- 对话引用的 Document、工作副本版本、解析结果和证据继续保留。
- 工作副本原路径在删除后可以被新的工作副本使用。

### 9.7 恢复

```text
用户请求恢复
→ 检查原工作副本路径是否被占用
→ 创建恢复 OperationPlan
→ 有冲突时提供新的恢复路径
→ 用户确认
→ 从回收站目录恢复工作副本
→ WorkingCopy.status = ACTIVE
→ 回收站条目状态改为 RESTORED
→ 写入 FILE_RESTORED
→ 返回逐文件回执
```

### 9.8 永久删除

MVP 不自动永久删除。

只有同时满足以下条件才允许创建永久删除 OperationPlan：

1. 回收站条目已经超过保留期。
2. 用户明确提出永久删除。
3. 当前用户有权操作对应 workspace。
4. 工作副本版本没有被消息、证据、ChangeSet 或其他业务对象引用。
5. OperationPlan 明确说明操作不可恢复。

执行后：

- 回收站条目状态更新为 `PURGED`。
- 写入 `FILE_DELETED` ChangeItem。
- 原始文件继续保持不变。

## 10. 上传附件查重确认与异步归档

上传附件继续遵守项目现有安全扫描、重复文件确认和原件保护规则。上传暂存不是长期文件层；附件通过安全扫描后先执行异步重复文件检查，无重复候选或用户确认继续上传后，才由系统归档服务异步追加到受管原始目录，再从对应原始文件异步创建工作副本。

流程：

```text
用户上传附件
→ 上传暂存
→ 安全扫描和 MIME 校验
→ 创建上传附件版本并进入 DUPLICATE_CHECK_PENDING
→ 创建 CHECK_UPLOAD_DUPLICATES 异步任务
→ duplicate-check worker计算SHA-256并生成受控相似度候选
→ 无重复候选：状态进入 PENDING
→ 有重复候选：状态进入 WAITING_DUPLICATE_CONFIRMATION
→ 对话逐文件提示用户选择继续上传、使用已有文件或取消上传
→ 用户确认继续上传：状态进入 PENDING
→ archive worker领取 ARCHIVE_UPLOAD_TO_MANAGED_ROOT 任务
→ 使用同文件系统临时文件写入并校验 SHA-256
→ 原子提交为不可变原始文件
→ 创建或关联 ManagedFile
→ 写入 ORIGINAL_FILE_ARCHIVED ChangeItem
→ 异步创建 IMPORT_WORKING_COPIES 任务
→ import worker创建 WorkingCopy 和初始 DocumentVersion
→ 状态进入 WORKING_COPY_READY
```

如果用户选择使用已有文件：

```text
校验候选工作副本仍属于当前用户和当前workspace
→ 把当前消息附件引用切换为已有Document或WorkingCopy
→ UploadArchiveRecord进入EXISTING_FILE_SELECTED
→ 清理新的上传暂存按保留策略执行
→ 不创建新的ManagedFile、WorkingCopy或导入任务
```

如果用户选择取消上传：

```text
UploadArchiveRecord进入CANCELLED
→ 当前附件不进入消息正式附件集合
→ 暂存文件按保留策略异步清理
→ 不创建新的ManagedFile、WorkingCopy或导入任务
```

重复文件检查规则：

- 精确重复使用完整文件SHA-256判定，不能仅使用文件名、大小或快速fingerprint。
- 相似文件使用本地受控文本指纹、Embedding或等价确定性服务生成候选，默认不得把文件正文发送到外部模型。
- 重复文件检查失败时不得误报“没有重复文件”；任务进入可重试失败状态，达到重试上限后提示用户检查失败并允许用户明确决定是否继续上传。
- 同一批上传必须逐文件处理。无重复候选文件可以继续异步归档，待确认文件单独等待，不能让一个待确认项阻塞整个批次。
- 系统不能自动选择`USE_EXISTING_FILE`、`CONTINUE_UPLOAD`或`CANCEL_UPLOAD`。
- 用户确认时必须重新校验候选文件状态和访问权限；候选已删除、进入回收站或权限变化时，不能继续引用旧候选。
- 用户选择继续上传后，即使内容与已有文件完全相同，也必须创建独立上传版本、独立原始文件记录和当前workspace下的工作副本关系。
- 可选的底层内容寻址去重不得改变上述逻辑隔离和审计关系，也不得向用户暴露跨用户内容复用。

上传归档路径规则：

```text
uploads/<yyyy>/<mm>/<upload_version_id>/<sanitized_filename>
```

该路径只是受管原始目录中的稳定归档路径，不代表业务分类。当前阶段尚未实现分类目录落位时，不得根据文件名猜测分类路径。

从上传附件创建的工作副本必须满足：

```text
working_copies.managed_file_id = managed_files.id
managed_files.source_type = UPLOAD_ARCHIVE
managed_files.source_upload_version_id = upload_document_version.id
document_versions.source_type = IMPORT
document_versions.source_managed_file_id = managed_files.id
```

归档和补偿规则：

- 上传暂存成功后立即创建重复文件检查任务，不等待用户发送消息；无重复候选时自动创建归档任务，存在重复候选时等待重复上传确认。重复上传确认不是OperationPlan，但必须有确定的确认记录和审计链。
- 同一个 `source_upload_version_id` 的归档任务必须幂等；重试不得产生多个对应原始文件。
- 重复文件检查、归档和导入必须使用独立异步任务串联，前一阶段只负责提交下一阶段任务，不允许在同一API请求或worker调用栈中同步执行完整链路。
- 归档成功必须以原始文件内容落盘、SHA-256 校验成功和 ManagedFile 事务提交全部完成为准。
- 周期同步必须扫描所有安全校验成功且已经允许归档、但尚未归档的上传附件版本并自动重试，保证受管原始目录覆盖全部已确认有效上传；等待重复上传确认的附件不进入覆盖率分母。
- 归档失败时保留上传暂存和结构化失败状态，不能创建 `managed_file_id = null` 的工作副本。
- 工作副本导入失败不影响已经归档的原始文件；周期同步继续补建工作副本。
- 工作副本后续重命名、移动、修改或删除不得反向同步到原始文件。
- 用户在归档或自动导入尚未完成时发送消息，消息继续引用稳定的上传附件版本；需要工作副本的 Agent 动作进入等待状态或返回处理中回执，不能创建无原始文件工作副本绕过流程。
- 上传、重复文件检查、归档或导入进行期间，聊天接口必须正常响应；只有依赖该文件工作副本的具体Tool步骤进入`WAITING_FOR_ASYNC_JOB`，不能让整个服务或其他AgentRun等待。

上传附件删除规则：

- `DUPLICATE_CHECK_PENDING`、`DUPLICATE_CHECKING`、`WAITING_DUPLICATE_CONFIRMATION`、`PENDING`、`ARCHIVING`和`RETRY_WAIT`状态的上传暂存不得清理。
- `CANCELLED`、`EXISTING_FILE_SELECTED`以及按策略过期且未继续上传的暂存文件，必须由独立异步清理任务按保留期删除；清理任务不得删除已归档原始文件或工作副本。
- 原始文件归档成功且 SHA-256 一致后，上传暂存的物理文件可以按保留策略清理；清理暂存不得删除 Document、上传附件版本、ManagedFile 或工作副本记录。
- 已进入消息的上传附件版本不能删除。
- 已创建工作副本后，用户删除的是工作副本，不是消息引用的上传附件版本。
- 工作副本进入回收站目录不得受到 `Document already used in a message` 限制。

## 11. Tool 设计

新增白名单 Tool：

| Tool | 职责 | 有副作用 | 需要确认 |
|---|---|---:|---:|
| `upload-duplicate-check` | 系统异步检查上传附件的精确重复和相似候选；不向Planner开放 | yes | no |
| `upload-duplicate-decision-record` | 根据用户明确对话记录继续上传、使用已有文件或取消上传；只能处理后端已解析的待确认记录 | yes | no |
| `upload-archive` | 系统归档服务把已通过安全扫描且已允许归档的上传附件追加为原始文件；不向 Planner 开放 | yes | no |
| `working-copy-import` | 系统为缺少工作副本的原始文件自动创建工作副本；不向 Planner 开放 | yes | no |
| `managed-root-reconcile` | 检查上传归档、原始文件索引和工作副本覆盖关系并补齐缺失项 | yes | no |
| `working-copy-list` | 查询工作副本 | no | no |
| `working-copy-search` | 搜索工作副本 | no | no |
| `working-copy-read` | 读取工作副本当前版本 | yes | no |
| `working-copy-lineage-read` | 查询原始文件、工作副本和工作副本版本关系 | no | no |
| `working-copy-update-plan` | 生成修改或覆盖计划 | yes | no |
| `working-copy-restore-plan` | 生成恢复计划 | yes | no |
| `trash-entry-list` | 查询回收站条目 | no | no |
| `confirmed-file-action` | 执行确认后的重命名、移动、复制、修改、删除或恢复 | yes | yes |

现有 Tool 迁移：

| 现有 Tool | 迁移方式 |
|---|---|
| `managed-file-list` | 保留兼容入口，内部转发到 `working-copy-list` |
| `managed-file-search` | 保留兼容入口，内部转发到 `working-copy-search` |
| `managed-file-read-document` | 保留兼容入口，默认读取工作副本 |
| `generate-rename-suggestions` | 输入对象改为 `working_copy_id` |
| `managed-root-scan` | 继续扫描受管原始目录 |
| `confirmed-file-action` | 扩展为工作副本统一执行入口 |

兼容 Tool 必须标记 deprecated，新 Planner 只能生成新的工作副本 Tool。

`upload-duplicate-check`、`upload-archive`、`working-copy-import`和`managed-root-reconcile`是系统生命周期Tool，只能由上传处理器、服务启动同步、watcher、scheduler或filesystem worker调用。Planner、LLM和普通用户不能选择这些Tool，也不能提供其文件路径参数。

`upload-duplicate-decision-record`只记录用户已经通过对话明确表达的决策。调用前必须由后端上下文服务把“第一个文件”“继续上传”等表达解析为确定的`upload_duplicate_review_id`和`upload_document_version_id`；存在多个可能对象时必须请求用户澄清，不能让LLM选择候选ID。

所有会触发重复文件检查、归档、导入、同步或批量文件I/O的Tool都只能返回：

```text
filesystem_job_id
job_status
progress_total
queued_at
```

Tool handler不得在请求线程内等待文件系统任务完成。

## 12. OperationPlan

每个工作副本计划项统一使用：

```json
{
  "working_copy_id": "uuid",
  "managed_file_id": "uuid",
  "operation": "TRASH",
  "before": {
    "relative_path": "奖学金/申请表.docx",
    "sha256": "sha256-value",
    "document_version_id": "uuid"
  },
  "after": {
    "relative_path": null,
    "trash_entry_id": "uuid"
  },
  "protection": {
    "managed_original_unchanged": true,
    "recoverable": true,
    "retention_until": "2026-08-20T00:00:00Z"
  },
  "execution_status": "PENDING"
}
```

确认卡必须展示：

1. 操作对象是工作副本。
2. 对应原始文件及其来源；上传归档来源显示为上传附件归档。
3. 受管原始目录不会变化。
4. 是否产生新的工作副本版本。
5. 是否生成回收站条目。
6. 是否可以恢复以及恢复截止时间。
7. 路径冲突、内容变化、失败和跳过事项。
8. 当前是否已经执行。

OperationPlan 必须保存当前工作副本版本、SHA-256 和路径。确认执行时任何一项发生变化，都必须返回计划过期并要求重新生成。

重命名或移动计划创建时，必须为每个计划项创建状态为 `PLANNED` 的工作副本路径记录。用户确认后依次推进为 `RUNNING` 和 `COMPLETED` 或 `FAILED`；计划取消或因路径、版本、SHA-256 变化而过期时，分别推进为 `CANCELLED` 或 `STALE`。只有 `COMPLETED` 记录可以改变当前工作副本路径，未确认和失败记录不能影响当前路径。

## 13. ChangeSet

新增或明确以下 `change_type`：

```text
ORIGINAL_FILE_ARCHIVED
UPLOAD_DUPLICATE_REVIEW_CREATED
UPLOAD_DUPLICATE_DECISION_RECORDED
UPLOAD_ARCHIVE_FAILED
WORKING_COPY_IMPORTED
WORKING_COPY_IMPORT_FAILED
FILE_COPIED
FILENAME_CHANGED
FILE_MOVED
WORKING_COPY_VERSION_CREATED
FILE_TRASHED
FILE_RESTORED
FILE_DELETED
ORIGINAL_FILE_CHANGED
ORIGINAL_FILE_MISSING
FILE_OPERATION_FAILED
```

语义规则：

- `ORIGINAL_FILE_ARCHIVED` 表示系统已把上传附件安全、不可变地追加为原始文件。
- `UPLOAD_DUPLICATE_REVIEW_CREATED`表示系统发现精确重复或相似候选并创建了待用户确认记录，不表示候选已经被认定为同一业务文件。
- `UPLOAD_DUPLICATE_DECISION_RECORDED`表示用户已明确选择继续上传、使用已有文件或取消上传，必须保存决策、确认消息和逐文件对象。
- `WORKING_COPY_IMPORTED` 表示系统已从原始文件自动创建工作副本，不表示用户执行了高风险操作。
- `UPLOAD_ARCHIVE_FAILED` 和 `WORKING_COPY_IMPORT_FAILED` 必须保存结构化失败原因，并由周期同步重试。
- `ORIGINAL_FILE_CHANGED` 和 `ORIGINAL_FILE_MISSING` 只表示同步检测结果，不表示 Agent 修改了原始文件。
- `FILE_TRASHED` 表示工作副本进入回收站目录。
- `FILE_DELETED` 只用于永久删除回收站条目。
- `WORKING_COPY_VERSION_CREATED` 表示工作副本内容产生新版本。
- `FILENAME_CHANGED` 和 `FILE_MOVED` ChangeItem 必须关联对应的工作副本路径记录；ChangeItem 表达业务变更和逐文件回执，工作副本路径记录保存不可变路径历史，两者不能互相替代。
- 每个工作副本必须单独写 ChangeItem。
- 用户取消上传或选择已有文件时，即使没有创建工作副本，也必须按上传附件版本写独立ChangeItem和回执。
- 单项失败不得阻止批次内其他工作副本执行。

逐文件回执至少包含：

```text
工作副本 ID
工作副本名称
工作副本操作前路径
工作副本操作后路径
对应原始文件
当前工作副本版本
工作副本路径记录 ID
路径记录更新时间
执行状态
回收站条目
原始文件是否变化
失败代码和可恢复建议
```

## 14. API

新增：

```text
POST /api/admin/managed-roots/{root_id}/reconcile
GET  /api/uploads/{upload_version_id}/duplicate-review
POST /api/uploads/{upload_version_id}/duplicate-review/decision
GET  /api/working-copies
GET  /api/working-copies/{working_copy_id}
GET  /api/working-copies/{working_copy_id}/download
GET  /api/working-copies/{working_copy_id}/lineage
GET  /api/working-copies/{working_copy_id}/versions
GET  /api/working-copies/{working_copy_id}/path-records
GET  /api/trash-entries
POST /api/trash-entries/{trash_entry_id}/restore-plan
GET  /api/managed-roots/{root_id}/reconcile-status
GET  /api/uploads/{upload_version_id}/archive-status
```

重复上传确认请求：

```json
{
  "duplicate_review_id": "uuid",
  "decision": "CONTINUE_UPLOAD",
  "selected_existing_working_copy_id": null
}
```

创建重复文件检查、归档、导入和同步任务的API统一返回HTTP `202 Accepted`，响应中包含`filesystem_job_id`和状态查询地址。查询类API不能因为后台归档或导入正在运行而整体不可用。

继续复用：

```text
POST /api/operations/plans
POST /api/operations/plans/{plan_id}/confirm
GET  /api/changesets/{changeset_id}
GET  /api/filesystem-jobs/{job_id}
GET  /api/jobs/{job_id}/events
```

权限：

- `user` 可以查看和操作自己 workspace 下的工作副本与回收站条目。
- `ops` 和 `admin` 可以手动触发幂等同步并查看归档或导入失败任务，但不能选择跳过某个有效原始文件的自动导入。
- 任何角色都不能通过普通 API 修改原始文件。
- 普通 user 不能查看或操作其他 workspace 的工作副本。
- 永久删除必须显式确认；批量永久删除建议限制为 `ops` 和 `admin`。

## 15. Agent Runtime

Planner 输出中的文件操作对象必须从 `managed_file_id` 迁移为 `working_copy_id`。

推荐状态流：

```text
用户消息
→ chat-intake
→ 解析工作副本范围
→ planning
→ 生成 Tool 计划或 OperationPlan
→ tool-dispatch
→ filesystem job（批量任务）
→ evidence/change
→ 逐文件回执
```

AgentGraphState 只保存：

```text
working_copy_ids
document_version_ids
operation_plan_id
filesystem_job_id
changeset_id
逐文件轻量结果
```

AgentGraphState 不得保存：

```text
工作副本正文
工作副本绝对路径
数据库 Session
StorageService
watcher
worker
文件句柄
```

工作副本目录解析必须由后端服务生成确定的 `working_copy_ids`，Planner 和 LLM 不得自行猜测路径。

上传归档、服务启动同步、定时同步和原始文件自动导入不进入 AgentGraph，也不接受 Planner 计划。它们由应用生命周期和任务系统驱动，但仍必须经过系统生命周期 Tool、schema 校验、StorageService、权限隔离、ToolInvocation、ChangeSet 和结构化日志边界。

重复文件检查完成并发现候选后，由后端通知服务在原上传会话中创建可审计的Agent消息和重复上传确认卡：

```text
检测到“文件A.docx”已有相同或高度相似文件。
请选择：
1. 继续上传并保留为独立文件
2. 使用已有文件（仅在当前用户有权访问时显示）
3. 取消本次上传
```

跨用户命中时消息只能写“系统检测到相同内容”或“系统检测到高度相似内容”，不得显示其他用户文件信息，也不得提供“使用已有文件”选项。

用户可以点击确认卡，也可以继续在聊天中自然语言回复。对话入口必须先由`ConversationAttachmentContextService`或等价服务读取当前会话的待确认记录，再把确定的候选集合交给Planner；AgentGraphState只保存待处理的`duplicate_review_ids`和用户选择，不保存跨用户候选标识。

归档或导入任务已经排队后，AgentRun可以进入`WAITING_FOR_ASYNC_JOB`并立即返回处理中回执。任务完成事件由job event、SSE/WebSocket或前端轮询更新卡片，不得占用原HTTP连接等待文件复制完成。

## 16. 并发、幂等和一致性

执行工作副本操作前必须：

1. 锁定 `working_copies` 数据库行。
2. 校验 OperationPlan 中的当前版本、SHA-256 和路径。
3. 校验工作副本路径仍然存在。
4. 校验目标路径没有冲突。
5. 校验 OperationPlan 尚未执行。
6. 校验当前用户和 workspace 所属关系。
7. 校验工作副本状态允许当前操作。
8. 校验 `working_copies.relative_path` 与最后一条成功工作副本路径记录一致。

计划项执行状态：

```text
PREPARED
FILESYSTEM_APPLIED
DATABASE_APPLIED
COMPLETED
FAILED
NEEDS_RECONCILIATION
```

推荐写入顺序：

```text
创建临时文件
→ 写入并 fsync
→ 校验 SHA-256
→ 原子移动
→ 更新数据库
→ 写入 ChangeItem
→ 标记 COMPLETED
```

文件系统和数据库不能组成真正的原子事务，因此：

- 每个计划项必须有稳定幂等键。
- 上传归档使用 `upload_document_version_id` 作为幂等键；原始文件自动导入使用 `working_copy_root_id + managed_file_id` 作为主导入幂等键。
- 多 API 实例同时启动时，必须通过数据库唯一约束、任务去重键或分布式锁保证同一轮启动同步只创建一组有效任务。
- 同一 OperationPlan 重复确认不得重复执行已经完成的计划项。
- worker 重启后必须根据执行状态恢复任务。
- 重命名和移动成功时，`working_copies` 当前路径缓存、工作副本路径记录和 ChangeItem 必须在同一数据库事务中提交，并使用同一个后端操作时间。
- 文件系统已经改名或移动、但数据库事务失败时，不得创建新的 DocumentVersion；计划项必须进入 `NEEDS_RECONCILIATION`，由校准任务根据文件系统实际路径补偿或回滚。
- `NEEDS_RECONCILIATION` 计划项必须由工作副本目录校准任务修复或进入人工处理。
- Agent 自己产生的文件系统事件必须带 `origin=AGENT`，避免 watcher 重复生成审计记录。

异步任务隔离规则：

- API、Agent和管理页面进程不运行归档、导入或全量扫描循环；生产部署使用独立duplicate-check、archive、import和reconcile worker，MVP数据库队列也必须保持进程边界。
- 不使用FastAPI `BackgroundTasks`执行大文件复制、目录遍历或批量哈希；这些任务必须写入可恢复的`filesystem_jobs`。
- 不同`queue_name`使用独立并发上限，归档和导入I/O高峰不得耗尽聊天请求线程、数据库连接池或LLM调用并发。
- worker每处理一个文件都更新心跳和进度，长时间无心跳的任务由租约机制重新排队。
- worker按固定批次提交，每个文件使用独立savepoint；单文件失败不得回滚整个批次。
- 同一受管原始目录的全量扫描和同步使用去重键或互斥租约，同一上传附件版本的查重和归档使用唯一幂等键。
- 队列积压时实施背压：上传可以继续进入暂存和排队，但系统必须显示预计状态和积压数量，不能转而在API请求中同步执行。
- 用户发起的普通聊天、文件查询和下载使用与后台I/O任务分离的资源配额；后台任务只能使用受限CPU、内存、磁盘吞吐和数据库连接数。
- 服务关闭时worker停止领取新任务，正在执行的单项完成安全点提交后退出；未完成任务依靠租约恢复。

## 17. 前端

聊天页统一展示工作副本，不再把原始文件直接作为可操作对象。

工作副本卡至少展示：

```text
工作副本名称
当前路径
对应原始文件
同步状态
当前工作副本版本
分类和证据
是否存在回收站条目
可执行操作
```

删除确认卡必须明确：

```text
将删除：工作副本
不会删除：原始文件
删除后位置：回收站目录
是否可恢复：是
恢复截止时间：具体时间
```

新增前端能力：

- 工作副本逐文件明细。
- 重复上传确认卡，逐文件展示匹配类型、允许公开的已有文件信息和三个明确决策。
- 跨用户重复候选只展示脱敏提示，不展示其他用户文件详情或“使用已有文件”操作。
- 异步重复文件检查、归档和导入进度卡；用户关闭页面或继续聊天后任务仍可恢复展示。
- 上传附件的扫描、归档和工作副本就绪状态。
- 归档或自动导入失败时的重试状态和管理员可诊断错误码。
- 原始文件变化提示。
- 工作副本版本列表。
- 回收站条目列表。
- 恢复确认卡。
- “查看原始文件关系”折叠区。
- 批量任务逐项选择和执行状态。

## 18. 日志与监控

日志字段统一增加：

```text
managed_root_id
managed_file_id
source_upload_version_id
upload_archive_record_id
upload_duplicate_review_id
upload_duplicate_candidate_id
working_copy_root_id
working_copy_id
working_copy_path_record_id
document_version_id
trash_entry_id
operation_plan_id
changeset_id
filesystem_job_id
queue_name
lease_owner
```

监控指标：

```text
受管原始目录数量
原始文件 ACTIVE/MISSING 数量
工作副本 ACTIVE/TRASHED/CONFLICT 数量
安全扫描成功但待归档上传数量
等待重复文件检查数量
等待重复上传确认数量
精确重复候选数量
相似文件候选数量
跨用户脱敏候选数量
上传归档失败数量
上传归档覆盖率
待导入原始文件数量
自动导入失败数量
原始文件主导入工作副本覆盖率
原始文件变化数量
回收站条目数量和占用空间
导入成功率
恢复成功率
工作副本操作失败率
NEEDS_RECONCILIATION 数量
各异步队列等待任务数量
各异步队列运行任务数量
异步任务平均等待时间和P95等待时间
异步任务平均执行时间和P95执行时间
worker租约过期和重试数量
```

完整性指标口径：

- 上传归档覆盖率 = 已进入`ARCHIVED`的允许归档上传附件版本数 / 全部无重复候选或已确认继续上传的安全扫描成功上传附件版本数，目标为100%。
- `WAITING_DUPLICATE_CONFIRMATION`、`CANCELLED`和`EXISTING_FILE_SELECTED`不进入上传归档覆盖率分母，但必须分别统计并展示。
- 原始文件主导入工作副本覆盖率 = 已存在主导入工作副本映射的 `ACTIVE` 原始文件数 / 全部 `ACTIVE` 原始文件数，目标为 100%。
- `QUARANTINED` 上传不进入上传归档覆盖率分母，但必须单独统计和展示。
- 任一覆盖率低于 100% 时，系统同步状态必须显示 `DEGRADED` 或 `SYNCING`，并展示待处理数量，不能静默报告健康。

安全约束：

- 日志不得写入文件正文、OCR 全文或工作副本绝对路径。
- 日志中的路径只能使用 `root_key + relative_path`。
- API 和 Tool 输出不得泄漏受管原始目录、工作副本目录或回收站目录的宿主机绝对路径。

## 19. 迁移方案

### 阶段一：固定受管原始目录的保护边界

- 所有 `ManagedRoot.read_only` 改为 `true`，明确其表示 Agent 和用户接口只读。
- 为系统归档 worker 配置独立仅追加写入身份和挂载点；API、Agent 和普通 Tool 不能获得该写权限。
- 停止 `ConfirmedRenameService` 修改原始文件和 `ManagedFile.relative_path`。
- 废弃 `MANAGED_ROOT_<KEY>_ALLOW_RENAME`。
- 现有尚未执行的原始文件重命名计划标记为 `STALE`。
- 原始文件扫描、查询和预览保持可用。

### 阶段二：建立工作副本数据模型

- 创建 `working_copy_roots`。
- 创建 `working_copies`。
- 创建 `working_copy_path_records`，并建立 `(working_copy_id, sequence_number)` 唯一约束以及当前路径查询索引。
- 创建 `upload_archive_records`，并为 `upload_document_version_id` 添加唯一约束和待重试状态索引。
- 创建或正式启用 `document_versions`。
- 创建 `trash_entries`。
- 创建 `managed_file_events`。
- 为所有新表补充迁移、索引和约束测试。

### 阶段三：实现上传归档和自动导入

- 创建`upload_duplicate_reviews`和`upload_duplicate_candidates`，增加唯一约束、权限索引和待确认状态索引。
- 扩展`filesystem_jobs`的`queue_name`、租约、心跳、优先级、幂等键和重试字段。
- 实现安全扫描后的`CHECK_UPLOAD_DUPLICATES`异步任务，以及精确SHA-256和本地相似度候选生成。
- 实现对话重复上传确认卡、自然语言决策解析和后端确定性确认接口。
- 实现上传附件安全扫描成功后的 `ARCHIVE_UPLOAD_TO_MANAGED_ROOT` 任务。
- 实现系统归档服务的仅追加写入、哈希校验、原子提交、幂等和失败重试。
- 为历史上传附件及 `managed_file_id` 为空的工作副本执行一次性归档回填。
- 实现工作副本 StorageService。
- 实现 `IMPORT_WORKING_COPIES` 任务。
- 实现逐文件复制、哈希校验、幂等和回执。
- 服务启动时只异步创建扫描和缺失工作副本任务，不生成OperationPlan、不等待用户确认，也不阻塞API启动完成。
- 回填完成后为全部工作副本的 `managed_file_id` 添加非空约束。

### 阶段四：切换读取链路

- 文件列表、搜索、读取、分类和证据回答切换到工作副本。
- 原始文件只用于同步和关系查询。
- 已有 ManagedFileSnapshot 按 `managed_file_id + source_sha256` 关联到初始工作副本版本，避免重复解析。
- 保留旧 API 和 Tool 兼容入口，但新 Planner 只生成工作副本 Tool。

### 阶段五：切换重命名和移动链路

- 重命名建议输入切换为 `working_copy_id`。
- `confirmed-file-action` 只修改工作副本。
- 重命名不创建新的 DocumentVersion，必须同时写工作副本路径记录和 `FILENAME_CHANGED` ChangeItem。
- 移动不创建新的 DocumentVersion，必须同时写工作副本路径记录和 `FILE_MOVED` ChangeItem。
- `working_copies` 当前路径缓存和成功工作副本路径记录必须使用同一个后端操作时间，并在同一数据库事务中更新。
- 当前路径必须按最后一条 `COMPLETED` 工作副本路径记录的 `updated_at` 计算；更新时间相同时使用 `sequence_number` 决定顺序。
- 重命名或移动失败时必须保留 `FAILED` 工作副本路径记录和失败 ChangeItem。
- 原始文件和原始文件索引不再被 Agent 文件操作修改。

### 阶段六：实现回收站目录

- 实现工作副本删除。
- 实现回收站条目查询。
- 实现工作副本恢复。
- 实现恢复路径冲突处理。
- 到期回收站条目只标记 `ELIGIBLE_FOR_PURGE`，不自动永久删除。

### 阶段七：实现修改和工作副本版本

- 修改前保存旧工作副本内容。
- 创建新的 DocumentVersion。
- 更新工作副本当前版本。
- 重新执行解析、分类、embedding 和 Neo4j 投影。
- 保留旧对话引用的工作副本版本。

### 阶段八：实现实时监听和定时同步

- 服务启动时自动提交一次全量同步任务，但不在启动线程内执行或等待完成。
- 实时监听受管原始目录。
- 每 5 分钟执行一次全量同步。
- 每 5 分钟检查安全扫描成功但尚未归档的上传附件并自动重试。
- 等待重复上传确认的附件只重发必要通知，不得被定时同步自动归档。
- 新增原始文件自动创建工作副本，不等待用户确认。
- 原始文件变化只更新同步状态。
- 用户确认后才使用新的原始文件刷新工作副本。
- 增加事件去重、队列溢出恢复和 worker 健康检查。

## 20. 测试要求

后端至少覆盖：

- 服务启动会为每个启用的受管原始目录幂等创建全量同步任务。
- 服务启动、上传请求和AgentRun不会同步执行目录扫描、重复文件检查、归档或导入。
- 创建重复文件检查、归档、导入和同步任务的API立即返回`202 Accepted`和`filesystem_job_id`。
- 后台归档和导入积压时，聊天、查询、下载和其他AgentRun仍能正常响应。
- 服务启动同步会为每个尚无主导入工作副本的原始文件自动创建工作副本，不生成 OperationPlan。
- 同一原始文件、workspace 和内容哈希的重复自动导入任务只产生一个活动工作副本。
- 上传附件通过安全扫描后先异步执行重复文件检查。
- 没有重复候选时自动异步归档为原始文件，并异步创建有关联`managed_file_id`的工作副本。
- 同一用户或同一workspace存在精确重复或相似候选时，通过对话逐文件提示并等待决策。
- 多个用户存在精确重复或相似候选时，只返回脱敏提示，不泄露其他用户的身份、文件名、路径、正文、分类或对话。
- 跨用户重复候选只保存在受限后台审计中，不投影为Neo4j文件关系，也不进入普通检索结果。
- 相似度低于阈值时不会创建重复上传确认；达到阈值也只能创建候选，不能由系统自动取消、复用或拒绝上传。
- 用户确认继续上传后才创建归档任务；确认过程不生成OperationPlan，但产生确定的重复上传确认审计记录。
- 用户选择使用已有文件时，只能选择当前用户有权访问的工作副本，不创建新的ManagedFile和WorkingCopy。
- 用户选择取消上传时，不创建新的ManagedFile和WorkingCopy，并异步清理暂存。
- 批次中一个待确认文件不会阻止其他无重复候选文件继续归档和导入。
- 定时同步不会绕过`WAITING_DUPLICATE_CONFIRMATION`自动归档。
- 同一个上传附件版本重复归档不会产生多个原始文件。
- 两个不同上传附件版本文件名或SHA-256相同时先分别创建重复上传确认；用户分别确认继续上传后，才分别创建可追溯且互不覆盖的原始文件记录。
- 归档失败保留上传暂存且不创建无对应原始文件的工作副本。
- 用户在归档或自动导入期间发送消息时，文件任务等待或返回处理中状态，不会绕过原始文件关联约束。
- 定时同步能补齐安全扫描成功且已经允许归档、但遗漏的上传归档任务；不得绕过重复上传确认。
- 定时同步能补齐已经存在原始文件但缺少的工作副本。
- 自动导入目标路径冲突时不会覆盖现有文件，并能使用稳定备用路径或进入可重试冲突状态。
- 主导入工作副本进入回收站目录后，启动同步和定时同步都不会自动重新创建；只能通过恢复流程恢复。
- API、Agent Runtime、Planner 和普通 Tool 无法读取系统归档 worker 的写入挂载点。
- 系统归档 worker 只能追加新原始文件，不能覆盖、重命名、移动或删除已有原始文件。
- `upload_archive_records`、ManagedFile 索引和实际归档文件不一致时进入校准或人工处理，不能误报归档成功。
- 工作副本后续修改不会反向覆盖上传归档形成的原始文件。
- 导入后原始文件 SHA-256 不变。
- 导入产生 WorkingCopy、DocumentVersion、ToolInvocation 和 ChangeSet。
- 重命名只改变工作副本路径，不创建新的 DocumentVersion。
- 移动只改变工作副本路径，不创建新的 DocumentVersion。
- 每次重命名和移动都产生包含完整变更前后路径、文件名、当前版本、SHA-256、执行人和审计链 ID 的工作副本路径记录。
- 重命名或移动失败时仍产生 `FAILED` 工作副本路径记录和失败 ChangeItem。
- 当前工作副本路径等于 `updated_at` 最新的 `COMPLETED` 工作副本路径记录；更新时间相同时使用更大的 `sequence_number`。
- `working_copies` 当前路径缓存与最后一条成功工作副本路径记录在同一事务和同一后端操作时间内更新。
- 已进入终态的工作副本路径记录不可修改，纠错只能新增记录。
- 当前路径缓存与工作副本路径记录不一致时，后续写操作被拒绝并进入校准流程。
- 删除后工作副本进入回收站目录。
- 删除后原始文件仍然存在且 SHA-256 不变。
- 删除后消息引用的 Document 和工作副本版本仍可读取。
- 工作副本删除不受 `Document already used in a message` 限制。
- 恢复后工作副本内容 SHA-256 不变。
- 修改产生新的工作副本版本。
- 修改失败时当前工作副本保持不变。
- 原始文件修改不会自动覆盖工作副本。
- 原始文件删除后工作副本仍可使用。
- 原始文件重命名后工作副本路径保持不变。
- 重复确认 OperationPlan 不会重复执行。
- OperationPlan 过期时拒绝执行。
- 目标路径冲突时不覆盖既有工作副本。
- 批次单项失败不影响其他工作副本。
- 路径穿越、符号链接和越界路径被拒绝。
- 普通 user 不能操作其他 workspace。
- 永久删除不能绕过确认。
- worker 中断后可以通过校准恢复一致性。
- worker租约过期后任务可以安全重试，重复确认和重复投递不会重复归档或导入。
- duplicate-check、archive、import和reconcile worker具有独立并发上限和资源配额。
- 实时监听漏事件后可以通过全量同步恢复一致性。

完成实现后必须执行：

```bash
cd apps/api
pytest -v
```

```bash
cd apps/web
npm run build
```

手工烟测：

```text
配置受保护的受管原始目录和系统归档 worker 独立写入入口
在受管原始目录放入测试原始文件
启动服务
确认启动同步自动索引原始文件并创建工作副本
上传一个附件但不发送消息
确认附件异步完成重复文件检查、归档和工作副本导入
再次上传相同文件
确认对话出现重复上传确认卡且归档任务尚未创建
选择继续上传，确认异步创建独立原始文件和工作副本
再次上传相同文件并选择使用已有文件，确认没有创建新的原始文件
使用另一个用户上传相同文件，确认只显示跨用户脱敏提示
在归档和导入运行期间继续聊天和查询其他文件，确认接口不被阻塞
确认没有生成导入 OperationPlan
停止实时监听并放入新的原始文件
确认定时同步自动补齐原始文件索引和工作副本
读取和搜索工作副本
重命名工作副本
确认原始文件名称不变
修改工作副本并查看工作副本版本
删除工作副本并查看回收站条目
确认原始文件仍然存在
恢复工作副本
让原始文件在外部发生变化
确认工作副本只显示 ORIGINAL_CHANGED
确认新的原始文件不会自动覆盖工作副本
```

## 21. 完成标准

三层文件模型只有在以下全部满足时才算完成：

- 受管原始目录对 Agent、用户接口和普通 Tool 始终只读，只有系统归档服务可以追加新原始文件。
- 所有安全扫描成功的上传附件都先异步执行重复文件检查；无重复候选或用户确认继续上传的附件才异步归档为原始文件。
- 重复或相似候选通过对话逐文件告知用户并等待继续上传、使用已有文件或取消上传的明确决策。
- 跨用户重复候选不会泄露其他用户文件信息，也不会建立未授权的Document或WorkingCopy关系。
- 重复文件检查、归档、导入、启动同步和定时同步均由持久化异步任务执行，不阻塞API启动、聊天或正常文件查询。
- 每个工作副本都有非空 `managed_file_id` 并能追溯到对应原始文件。
- 服务启动时自动同步所有启用的受管原始目录，并为尚无主导入工作副本的原始文件自动创建工作副本。
- 无重复候选的自动归档和原始文件自动导入不由用户控制，也不生成OperationPlan；存在重复候选的上传必须先完成重复上传确认。
- 受管原始目录覆盖全部允许归档的有效上传和部署原始文件，工作副本修改不会反向覆盖原始文件。
- Agent 的增删改查全部针对工作副本。
- 重命名、移动、修改和删除不会改变原始文件。
- 重命名和移动只改变工作副本路径，不产生新的工作副本版本。
- 每次重命名和移动都有不可变的详细工作副本路径记录，当前路径以最后一条成功记录的更新时间为准。
- 删除工作副本后可以通过回收站条目恢复。
- 修改工作副本后可以访问旧的工作副本版本。
- 原始文件变化不会静默覆盖工作副本。
- 原始文件删除后工作副本继续可用。
- 消息引用不会阻止工作副本进入回收站目录。
- 每次高风险操作都有 OperationPlan。
- 每个工作副本都有 ChangeItem 和逐文件回执。
- 定时同步和实时监听丢失事件后可以通过全量同步恢复一致性。
- 后端测试全部通过。
- 前端构建成功。

## 22. 明确不做

本阶段不做：

- Agent 修改原始文件。
- 工作副本自动覆盖原始文件。
- 原始文件变化后自动覆盖工作副本。
- 双向文件同步。
- 自动合并工作副本和原始文件内容。
- 自动永久删除回收站条目。
- 使用硬链接连接原始文件和工作副本。
- 绕过 OperationPlan 执行高风险操作。
- 使用文件系统扫描日志替代 ChangeSet。

未来如果需要把工作副本发布到受管原始目录，必须设计独立的高风险“发布”能力，不得复用普通重命名、移动、修改或删除流程。

## 23. 开源设计参考

- [Paperless-ngx Document Versions](https://github.com/paperless-ngx/paperless-ngx/blob/dev/docs/api.md#document-versions)：参考稳定 Document 与文件内容版本分离的设计。
- [Nextcloud files_versions](https://github.com/nextcloud/server/tree/master/apps/files_versions)：参考文件版本作为独立能力的设计。
- [Nextcloud files_trashbin](https://github.com/nextcloud/server/tree/master/apps/files_trashbin)：参考回收站条目、恢复和保留期设计。

本项目不直接引入这些项目作为运行依赖，只参考其文件版本和回收站边界。实际实现继续遵守 File Agent 的 Agent Runtime、Tool 白名单、OperationPlan、ChangeSet、StorageService、权限和审计规则。
