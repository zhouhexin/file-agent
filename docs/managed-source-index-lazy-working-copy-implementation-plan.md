# 受管原始文件预分析与工作副本按需物化实施方案

- 状态：开发完成，待部署数据库迁移与环境验证
- 编写日期：2026-08-13
- 适用分支：`workfile`
- 适用范围：受管原始目录初始化、源文件检索、正文问答、工作副本按需生成及后续文件操作
- 关联文档：`docs/managed-original-working-copy-trash-implementation-plan.md`、`docs/classification-topic-summary-implementation-plan.md`

## 1. 目标

旧实现会在受管原始目录扫描后，为全部原始文件创建 `IMPORT_WORKING_COPIES`，随后执行
`ANALYZE_DOCUMENT_VERSION`。当目录包含大量文件，特别是大量需要 LibreOffice 转换的 `.doc`、
`.xls` 文件时，全量复制和全量工作副本分析会长期占用 `IMPORT`、`ANALYSIS` 队列。

本方案将初始化和用户使用流程调整为：

```text
受管原始目录初始化
-> 快速扫描原始文件元数据
-> 后台只读分析原始文件
-> 持久化摘要、关键词、实体、正文分块、表格结构和检索索引
-> 不创建工作副本

用户发起文件请求
-> 同时检索工作副本索引和原始文件索引
-> 优先使用活动工作副本
-> 没有工作副本时，直接使用已验证的原始文件分析结果回复
-> 回复完成后异步物化本轮全部相关原始文件的工作副本
-> 后续增删改查只针对工作副本
```

目标包括：

1. 服务启动、scheduler 和目录扫描不得等待文件复制、LibreOffice 转换或全文分析完成。
2. 初始化期间后台逐步完成全部支持格式原始文件的正文检索准备。
3. 文件搜索必须覆盖活动工作副本和尚未物化的原始文件，支持正文关键词、摘要、实体和表格内容查询。
4. 对没有工作副本但已有源侧分析结果的文件，系统必须直接使用当前原始文件修订的完整正文或表格证据生成正确回答并返回可定位引用；回答成功后，把本轮最终检索结果中全部相关原始文件异步物化为工作副本，不能用“正在处理，稍后回答”替代本轮答案。
5. 工作副本物化后必须复用同一原始文件修订的分析结果，不能再次启动 LibreOffice 或重复全文解析。
6. 当全部当前原始文件修订都已有匹配的活动工作副本时，检索自动跳过原始文件分支。
7. 原始文件始终只读；重命名、移动、删除、恢复和内容修改仍只允许作用于工作副本。

## 2. 统一名词

本方案沿用三层文件生命周期名词，并补充以下名词：

| 统一名词 | 英文代码名 | 定义 |
|---|---|---|
| 原始文件 | `ManagedFile` | 受管原始目录扫描登记的只读文件 |
| 原始文件修订 | `ManagedFileRevision` | 由文件大小、修改时间、文件标识和最终 SHA-256 固化的一次原始文件内容状态 |
| 原始文件分析 | `ManagedFileAnalysisRun` | 对一个原始文件修订执行解析、摘要、关键词、实体、正文分块和表格结构抽取的运行记录 |
| 原始文件检索资料 | `ManagedFileSearchProfile` | 原始文件修订的文件级检索投影，包括标题、摘要、主题、关键词、实体和年份 |
| 原始文件正文分块 | `ManagedFileTextChunk` | 原始文件修订的页、段落、Sheet 或单元格范围级正文索引 |
| 原始文件表格结构 | `ManagedFileTableStructure` | Sheet、表头、行列数、类型和确定性统计信息 |
| 相关文件集合 | `RelevantFileSet` | 本轮检索、证据校验和重排完成后，被最终结果收录的全部相关文件稳定 ID 集合；不等于未经验证的初始候选集 |
| 工作副本物化 | `MaterializeWorkingCopy` | 把相关文件集合中尚无匹配活动工作副本的原始文件复制并登记为可操作工作副本 |
| 双范围检索 | `DualScopeSearch` | 对活动工作副本和未被当前工作副本覆盖的原始文件修订并行召回、合并和去重 |

“物化”只表示创建工作副本，不表示修改原始文件，也不需要 OperationPlan。用户后续对工作副本执行高风险操作时，仍必须生成并确认 OperationPlan。

## 3. 核心不变量

1. 原始文件分析只能通过受控只读路径解析器读取 `ManagedRoot + relative_path`，不得接受 LLM 生成的绝对路径。
2. API、Agent、Planner、普通 Tool 和分析 worker 都不得修改、移动、覆盖或删除原始文件。
3. 原始文件分析结果必须绑定 `ManagedFileRevision`，不能只绑定可变化的文件路径。
4. 摘要用于文件级召回和排序，完整正文分块用于防止摘要遗漏正文关键词；摘要不能替代最终证据。
5. 最终内容回答必须引用当前修订的页码、段落、Sheet 或单元格范围。
6. 工作副本只改名或移动时不生成新版本，也不重建摘要；工作副本内容变化时必须创建新 `DocumentVersion` 并执行 `ANALYZE_DOCUMENT_VERSION`。
7. 工作副本物化必须校验原始文件修订。复制前后文件大小或修改时间变化时，不得复用旧分析结果。
8. 同一原始文件修订在同一共享工作区最多存在一个主导入工作副本，物化任务必须幂等。
9. 检索结果必须按原始文件来源关系去重，不能把同一内容的原始文件和工作副本重复展示为两份结果。
10. 存在与当前原始文件修订一致的活动工作副本时，默认只检索和展示工作副本。
11. 原始文件已变化但工作副本仍对应旧修订时，两者不得静默合并；系统必须标记 `ORIGINAL_CHANGED`。
12. 初始化分析、LibreOffice 转换和工作副本物化都只能由持久化异步 worker 执行，scheduler 只负责入队。
13. 搜索或问答完成后，必须对 `RelevantFileSet` 中全部尚未物化的当前原始文件修订提交幂等物化任务，不能只物化生成答案时实际引用的一份文件，也不能只处理前端当前页展示的文件。
14. 未通过相关性校验、被硬条件排除或仅属于内部扩大召回的候选不得进入 `RelevantFileSet`，避免把误召回文件批量复制到工作副本目录。

## 4. 目标任务链

### 4.1 初始化任务链

```text
RECONCILE_MANAGED_ROOT
-> SCAN_MANAGED_ROOT
-> 保存或更新 ManagedFile
-> 创建 ManagedFileRevision(ANALYSIS_PENDING)
-> 创建 ANALYZE_MANAGED_FILE_REVISION，低优先级
-> 不创建 IMPORT_WORKING_COPIES
```

`SCAN_MANAGED_ROOT` 仍只做快速目录遍历和元数据登记。扫描批次提交后，独立 `SOURCE_ANALYSIS` worker
逐个分析原始文件；启动过程不等待这些任务完成。

### 4.2 原始文件分析任务链

新增任务：

```text
ANALYZE_MANAGED_FILE_REVISION
-> 锁定当前 ManagedFileRevision
-> 读取前校验 size + modified_at + file_identity
-> 通过只读解析 Adapter 提取正文或表格
-> .doc/.xls 按需调用 LibreOffice
-> 生成本地 Jieba + LexRank 摘要和分类主题摘要
-> 提取关键词、实体、年份和文档类型
-> 生成页、段落、Sheet、单元格范围正文分块
-> 生成表格结构和确定性统计
-> 建立 PostgreSQL FTS/GIN；按配置建立向量索引
-> 读取后再次校验原始文件状态和 SHA-256
-> 原子发布当前修订的检索资料
```

后台摘要 Provider 继续默认使用 CPU-only `Jieba + LexRank`。`LLM_ENABLED=true` 不能隐式开启源文件初始化分析的外部 LLM 调用。

### 4.3 用户查询任务链

```text
用户自然语言请求
-> LLM 生成结构化查询条件
-> 后端校验目录范围、年份、文件类型和查询词
-> HybridSearch 执行双范围检索
   -> 活动工作副本检索
   -> 当前未被工作副本覆盖的原始文件修订检索
-> 候选取并集、按 managed_file_id 和修订关系去重
-> 固化本轮 RelevantFileSet
-> 对需要内容回答的候选读取已持久化证据
-> LLM 基于验证后的证据回复
-> 对 RelevantFileSet 中全部未物化原始文件提交 MATERIALIZE_WORKING_COPY
```

普通文件列表仅展示未经相关性判断的候选时，不应把初始候选集全部物化。满足以下任一条件时，
后端先固化 `RelevantFileSet`，再异步物化集合中的全部相关原始文件：

- 文件搜索完成，文件被收录进最终“明确相关”或“可能相关”结果集合。
- 用户打开、预览、下载或选择一个或多个具体原始文件。
- 系统使用一个或多个原始文件证据生成内容回答。
- 用户要求对一个文件集合执行分类、重命名、移动、删除、恢复、复制或修改。

分页只影响前端展示，不影响相关文件集合。若一次查询确认 80 份相关文件但页面只展示前 20 份，
后台仍需对 80 份相关文件逐一创建幂等物化任务。为避免大结果集瞬间占满队列，任务按配置的批次
大小提交和限流执行，但不能静默截断。

### 4.4 工作副本物化任务链

新增任务：

```text
MATERIALIZE_WORKING_COPY
-> 锁定 ManagedFile + 当前 ManagedFileRevision
-> 校验当前修订和分析结果
-> 单次复制并校验 SHA-256
-> 创建 Document、DocumentVersion、WorkingCopy、FileObject
-> 写入初始 WorkingCopyPathRecord 和 ChangeSet
-> 复用源侧页面、摘要、关键词、实体、Chunk、表格结构和向量
-> 建立 DocumentSearchProfile
-> 标记物化完成
```

现有 `IMPORT_WORKING_COPIES` 在迁移期作为兼容任务读取，但新的受管目录扫描不得再批量创建该任务；上传附件归档链路可以继续在归档后立即物化，因为上传文件通常会被当前用户直接使用。

## 5. 数据模型

### 5.1 `managed_file_revisions`

建议字段：

```text
id
managed_file_id
revision_number
size_bytes
modified_at
file_identity
quick_fingerprint
content_sha256
status
analysis_status
is_current
created_at
updated_at
```

状态建议：

```text
DISCOVERED
ANALYSIS_PENDING
ANALYZING
READY
NEEDS_REVIEW
FAILED
STALE
```

唯一约束：同一个 `managed_file_id` 最多一条 `is_current=true`。

### 5.2 `managed_file_analysis_runs`

记录解析器、转换器和检索版本：

```text
id
managed_file_revision_id
status
parser_name
parser_version
converter_name
converter_version
summary_provider
summary_version
index_version
error_code
error_message
started_at
finished_at
```

LibreOffice 转换产物作为派生件关联到该分析运行，成功后可复用，失败或重处理时不能覆盖原始文件。

### 5.3 `managed_file_search_profiles`

文件级快速召回字段：

```text
managed_file_revision_id
title
summary
topic_summary_json
keywords_json
entities_json
years_json
document_type
sheet_names_json
search_text
search_vector
embedding
status
created_at
updated_at
```

`search_vector` 使用 PostgreSQL FTS/GIN；中文查询先由现有 Jieba 查询服务生成受控检索词。向量索引是补充召回通道，不能替代 FTS、文件名和正文关键词检索。

### 5.4 `managed_file_text_chunks`

正文级防漏召回与证据定位字段：

```text
id
managed_file_revision_id
chunk_index
page_number
sheet_name
cell_range
section_title
text_content
search_text
search_vector
embedding
token_count
created_at
```

表格按 Sheet、表头和有上限的行区间分块。不得把整个超大工作表拼成一个 Chunk。

### 5.5 `managed_file_table_structures`

保存由确定性代码计算的表格结构：

```text
managed_file_revision_id
sheet_name
row_count
column_count
headers_json
column_types_json
date_ranges_json
numeric_statistics_json
sample_values_json
created_at
```

行数、金额、最大值、最小值和日期范围必须由 `openpyxl` 或确定性 Adapter 计算，LLM 不得生成或修改这些数字。

### 5.6 工作副本来源关联

建议给 `document_versions` 增加：

```text
source_managed_file_revision_id
source_analysis_run_id
```

`working_copies.imported_source_sha256` 继续保留。通过修订 ID 和 SHA-256 双重证明工作副本复用了哪次源侧分析。

## 6. 双范围检索与防漏规则

### 6.1 检索范围

每次 workspace 全局文件搜索至少并行执行：

1. 活动工作副本文件名、路径、摘要、关键词、实体和正文 Chunk 检索。
2. 当前原始文件修订文件名、路径、摘要、关键词、实体、表格结构和正文 Chunk 检索。

原始文件分支仅查询满足以下条件的记录：

```text
不存在与当前 ManagedFileRevision 匹配的 ACTIVE WorkingCopy
或原始文件当前修订晚于工作副本导入修订
```

### 6.2 召回通道

结果必须取以下通道的并集，再做确定性过滤和混合重排：

- 完整文件名和模糊文件名。
- 受管目录相对路径。
- 年份、扩展名、时间和文档类型。
- 摘要与分类主题摘要。
- 关键词和实体。
- PostgreSQL FTS/GIN 正文分块。
- 表格 Sheet、表头和单元格文本。
- 启用时的 pgvector 与 Neo4j 补充召回。

不能因为某一通道没有命中就提前排除其他通道命中的文件。

### 6.3 去重和优先级

合并结果时使用以下顺序：

1. 当前活动工作副本且来源修订一致。
2. 当前原始文件修订。
3. 与原始文件已经分叉的旧工作副本版本，只在用户明确要求历史版本时展示。

同一 `managed_file_id + source_revision_id` 只展示一次。工作副本结果卡沿用现有样式；源文件结果卡复用同一视觉结构，但后端资源引用必须标明 `resource_type=MANAGED_SOURCE`，前端不能伪造 `document_id`。

### 6.4 检索完整性

后端按当前受管目录快照计算：

```text
total_active_files
covered_by_working_copy
covered_by_source_index
metadata_only
analysis_pending
analysis_failed
unsupported
```

只要每个活动原始文件都被“当前工作副本索引”或“当前原始文件修订索引”覆盖，正文检索完整性即可为 `COMPLETE`。否则返回 `PARTIAL` 或 `UNVERIFIABLE`，并明确未覆盖数量，不能声称已经找全。

### 6.5 全部物化后的自动优化

不依赖人工开关。查询前执行低成本覆盖判断：

```text
active_managed_file_count
== current_revision_materialized_and_indexed_count
```

相等时跳过原始文件检索分支，只查询工作副本。新增、变化或缺失工作副本出现后，原始文件分支自动重新启用。

## 7. 用户回复与按需物化

### 7.1 已有源侧分析结果

用户询问具体文件且原始文件修订状态为 `READY`：

1. 校验检索资料、正文 Chunk、表格结构和证据均属于当前原始文件修订。
2. 直接读取数据库中的摘要、Chunk、页码、Sheet 和单元格范围；必要时对原始文件执行轻量修订校验。
3. LLM 只能基于这些已验证证据生成与用户问题对应的正确回答，并返回文件名、页码、Sheet 或单元格引用。
4. 证据不足时明确说明缺少依据，不能根据文件名、摘要或历史修订猜测答案。
5. 本轮必须返回最终回答；不能用“正在处理，稍后回答”作为已经 `READY` 文件的响应。
6. 回复结果持久化后，对本轮 `RelevantFileSet` 中全部尚无匹配活动工作副本的原始文件提交
   `MATERIALIZE_WORKING_COPY`；物化过程不阻塞或改变本轮回答。

### 7.2 初始化分析尚未完成

完全避免等待在技术上不可保证，尤其是首次命中未分析的 `.doc`、`.xls` 或 OCR 文件。降级顺序为：

1. 唯一完整文件名命中时，提升同一 `ANALYZE_MANAGED_FILE_REVISION` 任务到最高只读分析优先级。
2. 文件名和元数据足以回答“是否存在、路径、类型、时间”等问题时立即回答，不等待正文分析。
3. 内容问答必须等待证据完成，不能让 LLM 根据文件名猜测。
4. 前端只更新原消息状态，不创建重复消息；任务完成后自动续跑原 AgentRun。

通过初始化低优先级预分析、查询命中提权和 LibreOffice 派生件复用，应把该降级压缩到少量尚未分析或分析失败的文件，而不是普通查询的默认行为。

### 7.3 物化失败

工作副本批量物化发生在回答之后，单个文件失败不能撤销已经有证据支持的回答，也不能阻止同一
相关文件集合中的其他文件继续物化。系统应：

- 保存 `MATERIALIZATION_FAILED` 状态和结构化日志。
- 告知用户文件内容已读取，但暂时不能执行修改类操作。
- 允许 ops/admin 显式重试。
- 后续修改类请求再次触发幂等物化，不得直接修改原始文件。

## 7.4 本分支实现记录（2026-08-13）

`workfile` 分支已按本方案完成以下实现：

1. 新增 `ManagedFileRevision`、`ManagedFileAnalysisRun`、源侧检索资料、正文分块、表格结构与
   `RelevantFileSet` 持久化模型，并提供 Alembic 修订 `20260813_0001`。
2. `SCAN_MANAGED_ROOT` 默认只创建低优先级 `SOURCE_ANALYSIS` 任务；不再为普通受管目录批量创建
   工作副本。扫描本身不计算完整 SHA-256 或解析正文。
3. `SOURCE_ANALYSIS` 在受控相对路径上完成解析、Jieba + LexRank 摘要、分类主题摘要、Chunk、表格
   结构和 PostgreSQL FTS 投影。分析完成后可直接作为源侧证据回答，首次读取不会因缺少工作副本而
   返回“正在处理”。
4. `hybrid-search` 已合并活动工作副本和未覆盖的当前源修订；检索完整性统计同样覆盖两种范围。
   当所有当前修订已有内容一致的活动工作副本时，源侧结果自然为空。
5. 搜索或源侧证据回答会固化本轮全部最终相关文件到 `RelevantFileSet`，并为其中每一个未物化
   源修订创建幂等 `MATERIALIZE_WORKING_COPY` 任务；分页不会截断该集合。
6. 物化任务复用源侧已持久化页面、元素、摘要与索引，避免同一 `.doc`/`.xls` 再次启动 LibreOffice。
   首次发布工作副本时，标准文件名也复用同一批已克隆的 `document_pages` 和现有命名建议服务；
   该分支禁止因命名解析指纹差异重新读取或解析文件。命名建议达到现有 `READY` 门槛时作为首次
   发布名称，证据不足则保留原名并待复核。此规则不作用于受管原件，也不改变活动工作副本后续
   改名仍须经过 OperationPlan 确认的边界。
   原始文件发生新修订时，已有工作副本会标记 `ORIGINAL_CHANGED`，不会被自动覆盖。
7. 周期性 `RECONCILE_MANAGED_ROOT` 必须为每轮终态扫描创建新的 `scan_generation`；历史
   `COMPLETED` / `FAILED` 扫描保持不可变审计记录，不得被重置。若同一受管根已有
   `PENDING` 或 `RUNNING` 扫描，则当前协调只复用该活动任务，保证单根最多一个活动扫描。
   因此一次配置或 taxonomy 错误不会永久阻断修复后的后续扫描，也不会产生并发重复遍历。

部署前必须执行数据库迁移，并启动 `SOURCE_ANALYSIS` 与 `MATERIALIZE,IMPORT` worker；具体命令见
`docs/runbook.md`。`MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY=1` 的默认部署含义是只启动一个
`SOURCE_ANALYSIS` worker，避免多个 LibreOffice 子进程并发抢占资源。

## 8. Agent、Tool 与安全边界

### 8.1 Tool 调整

建议保留对普通用户隐藏的生命周期能力：

| 能力 | 用途 | 是否进入 Adaptive Catalog |
|---|---|---:|
| `hybrid-search` | 内部执行双范围检索并返回统一候选 | 是 |
| `managed-source-evidence-read` | 读取后端已解析并授权的原始文件修订证据 | 是，成熟后启用 |
| `materialize-working-copy` | 为相关文件集合逐文件异步创建工作副本 | 否，由后端生命周期触发 |
| `confirmed-file-action` | 执行确认后的工作副本操作 | 否，只能由确认接口触发 |

LLM 只能引用 CatalogSnapshot 中已启用 Tool。`managed-source-evidence-read` 输入只接受稳定
`managed_file_id` 或 `managed_file_revision_id`，真实路径由后端解析。

### 8.2 文件操作请求

用户对只有原始文件的对象提出重命名、移动、删除、恢复、复制或修改时：

```text
解析并锁定原始文件修订
-> MATERIALIZE_WORKING_COPY
-> 物化完成后自动续跑原请求
-> 生成工作副本 OperationPlan
-> 用户确认
-> confirmed-file-action
```

任何情况下都不得为了减少等待而对原始文件执行操作。

## 9. 队列和资源隔离

建议队列：

```text
RECONCILE     目录配置和一致性任务
SCAN          快速元数据扫描
SOURCE_ANALYSIS 原始文件修订分析
MATERIALIZE   按需工作副本物化
ANALYSIS      工作副本内容发生变化后的版本分析
FILE_OPERATION 已确认的工作副本操作
```

优先级：

- 用户当前请求命中的 `SOURCE_ANALYSIS`：10。
- 上传附件或操作前的 `MATERIALIZE`：20。
- 初始化源文件分析：100。
- 历史失败重处理：120。

LibreOffice 任务必须限制并发，建议每个部署实例从 1 开始；普通 DOCX、XLSX、TXT 等解析可以使用更高并发。不得让大量 LibreOffice 子进程挤占 API、检索或工作副本操作资源。

## 10. 配置建议

```env
# 新受管目录只建立源侧索引，工作副本在文件进入相关文件集合后生成。
MANAGED_FILE_INITIALIZATION_MODE=source_index_first

# 原始文件后台预分析。
MANAGED_SOURCE_ANALYSIS_ENABLED=true
MANAGED_SOURCE_ANALYSIS_BACKGROUND_PRIORITY=100
MANAGED_SOURCE_ANALYSIS_ON_DEMAND_PRIORITY=10
MANAGED_SOURCE_ANALYSIS_BATCH_SIZE=20
MANAGED_SOURCE_LIBREOFFICE_CONCURRENCY=1

# 双范围检索和相关文件集合物化。
MANAGED_SOURCE_SEARCH_ENABLED=true
MATERIALIZE_RELEVANT_FILES_AFTER_RESPONSE=true
MATERIALIZE_WORKING_COPY_PRIORITY=20
MATERIALIZE_RELEVANT_FILES_BATCH_SIZE=50

# 后台摘要保持本地 CPU-only，不由 LLM_ENABLED 隐式改变。
DOCUMENT_SUMMARY_PROVIDER=extractive
CLASSIFICATION_SUMMARY_PROVIDER=extractive
```

迁移期可保留：

```env
MANAGED_FILE_INITIALIZATION_MODE=eager_working_copy
```

仅用于回滚到当前全量工作副本行为，不作为新部署默认值。

## 11. 日志和运维可观测性

至少新增以下结构化事件：

```text
managed_source.revision.discovered
managed_source.analysis.queued
managed_source.analysis.started
managed_source.analysis.converted
managed_source.analysis.completed
managed_source.analysis.failed
search.dual_scope.started
search.dual_scope.coverage
search.dual_scope.completed
working_copy.materialization.queued
working_copy.materialization.completed
working_copy.materialization.failed
working_copy.materialization.analysis_reused
```

运维页面应能查看：

- 原始文件总数及当前修订数。
- 源侧分析 READY、PENDING、FAILED、UNSUPPORTED 数量。
- LibreOffice 平均和 P95 转换耗时。
- 工作副本物化覆盖率。
- 查询时工作副本命中数、原始文件命中数和去重数。
- 因索引未完成导致的 `PARTIAL` 查询数量。

日志不得记录原始文件正文、本地绝对路径、API key 或大段摘要。

## 12. 数据迁移与兼容

1. 已存在的活动工作副本全部保留，不删除、不重新复制。
2. 通过现有 `WorkingCopy.managed_file_id` 和 `imported_source_sha256` 回填对应 `ManagedFileRevision`。
3. 已有工作副本解析、摘要和 Chunk 可以反向生成源侧检索资料，避免历史文件重新调用 LibreOffice。
4. 没有工作副本的原始文件创建当前修订并进入低优先级源侧分析。
5. 新代码上线后，`SCAN_MANAGED_ROOT` 停止为普通部署原始文件批量创建 `IMPORT_WORKING_COPIES`。
6. 已经排队但尚未运行的历史全量导入任务需要由一次迁移脚本标记为 `CANCELLED_BY_MIGRATION`；RUNNING 任务允许完成，不能中途删除目标文件。
7. 上传附件归档链路仍可立即物化，不受普通受管目录懒物化策略影响。

## 13. 分阶段开发顺序

### 阶段一：数据模型与修订识别

- 新增原始文件修订、分析运行、检索资料、正文分块和表格结构表。
- 扫描器生成或更新当前修订，不再只依赖可变路径。
- 增加 Alembic 迁移、模型测试和 PostgreSQL 索引。

### 阶段二：原始文件只读分析

- 抽取现有解析 Adapter，使其可以读取受控原始文件修订。
- 实现 `ANALYZE_MANAGED_FILE_REVISION` 和 `SOURCE_ANALYSIS` 队列。
- 接入本地摘要、关键词、实体、正文分块、表格结构和 LibreOffice 派生件缓存。
- 保证分析异常不影响目录扫描和 API。

### 阶段三：扫描策略切换

- 增加 `MANAGED_FILE_INITIALIZATION_MODE`。
- `source_index_first` 下停止批量创建工作副本导入任务。
- watcher 发现新增或变化文件时只更新修订并提交源侧分析。

### 阶段四：双范围检索

- 扩展现有 `hybrid-search`，并行检索工作副本和原始文件。
- 实现统一候选 Schema、来源去重、工作副本优先和完整性统计。
- 覆盖文件名、摘要、关键词、实体、正文 Chunk 和表格内容。

### 阶段五：源文件证据回答

- 实现受控 `managed-source-evidence-read`。
- 让 evidence-answer 接受统一资源引用，不依赖伪造的 Document。
- 前端复用现有文件卡、证据卡和回答样式，仅补充源文件资源类型处理。

### 阶段六：工作副本按需物化

- 实现 `MATERIALIZE_WORKING_COPY` 幂等任务。
- 持久化 `RelevantFileSet`，回复后对集合中全部相关原始文件异步触发物化，分页不得截断任务范围。
- 批量物化逐文件隔离失败并限流；操作类请求在生成 OperationPlan 前强制完成目标文件物化。
- 复用源侧分析结果，验证 `.doc`、`.xls` 不重复调用 LibreOffice。

### 阶段七：覆盖优化和历史迁移

- 回填已有工作副本的源修订及源侧检索资料。
- 实现全部物化后自动跳过原始文件检索。
- 清理或取消尚未执行的历史全量导入任务。
- 更新 runbook、部署配置和运维查询。

## 14. 测试要求

后端至少覆盖：

1. 启动和扫描只创建元数据及源分析任务，不创建普通原始文件工作副本。
2. `.doc`、`.xls` 仅在源分析首次需要时调用一次 LibreOffice。
3. 相同修订物化工作副本后复用解析结果，不再次调用 LibreOffice。
4. 搜索只命中源文件时仍可返回正文相关结果和证据。
5. 搜索同时命中源文件和工作副本时只展示工作副本。
6. 原始文件发生变化后，旧源索引失效，已有工作副本不被覆盖。
7. 全部文件物化且索引完整后，查询不执行源文件分支。
8. 某些源文件分析失败时，完整性状态为 `PARTIAL`，不得声称已经找全。
9. 表格可按 Sheet、表头和单元格正文命中，数字统计由确定性工具生成。
10. 相关文件集合批量物化时单文件失败不影响已生成的证据回答和其他文件物化，但该失败文件的修改请求必须停止。
11. 检索返回多页相关文件时，物化范围覆盖完整 `RelevantFileSet`，而不是当前展示页或答案引用文件。
11. LLM 不能传入绝对路径或绕过 Tool 修改原始文件。
12. 重命名、移动、删除和恢复只针对工作副本并继续要求 OperationPlan。

完整验证仍需执行：

```bash
cd apps/api
pytest -v

cd apps/web
npm run build
```

## 15. 验收标准

1. 新增大规模受管原始目录后，API 和 scheduler 能快速返回，扫描期间不复制全部文件。
2. 初始化后台能够为全部支持格式建立摘要、关键词、实体、正文分块和表格结构。
3. 用户查询同时覆盖工作副本和未物化原始文件，并返回可判断的完整性状态。
4. 已完成源侧分析的文件首次内容问答能够直接返回由当前原始文件修订证据支持的正确回答及可定位引用，不能返回“正在处理，稍后回答”代替答案。
5. 用户获得检索或证据回答后，系统自动为本轮 `RelevantFileSet` 中全部尚未物化的相关原始文件异步生成工作副本，不限于答案直接引用的文件或当前展示页。
6. 同一原始文件修订物化时不重复解析，不重复启动 LibreOffice。
7. 后续增删改查全部落在工作副本，受管原始目录不发生变化。
8. 全部原始文件都被当前工作副本覆盖后，源文件检索自动停用；出现新增或变化文件时自动恢复。
9. 任何搜索结果都能说明当前扫描、索引和失败覆盖情况，不把部分结果描述为已经找全。

## 16. 明确限制

- 初始化源侧全文分析本身仍需要时间；本方案消除的是全量工作副本复制和重复分析，不会让 LibreOffice 单次转换变快。
- 尚未完成源侧分析的文件无法保证正文内容被立即召回，因此必须返回 `PARTIAL` 完整性并提升相关任务优先级。
- 加密文件、损坏文件、宏内容和不支持格式不得尝试破解或执行，只能进入 `NEEDS_REVIEW` 或 `UNSUPPORTED`。
- 源文件只读回答不意味着允许直接下载或修改任意服务器路径，所有访问仍受 workspace、ManagedRoot 和 Tool Schema 校验。
