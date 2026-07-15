# 受管文件全局多标签分类开发计划

## 1. 文档信息

- 状态：阶段 A-E 已完成开发与自动化测试；阶段 F 已具备图谱关闭基线和 Shadow 配置能力，待部署环境验收与反馈样本积累。
- 目标：让用户通过对话对受管目录文件执行基于完整正文的全局多标签分类，同时让新上传文件复用同一分类候选空间。
- 依赖：受管目录扫描与快照、文档解析与 OCR、`DocumentClassificationService`、分类建议持久化、ChangeSet、Neo4j 图谱分类第二版本。
- 前置文档：
  - `docs/neo4j-graph-classification-overall-plan.md`
  - `docs/neo4j-graph-classification-v2-implementation-plan.md`
- 本计划不包含文件移动、复制、删除或按分类整理物理目录。

## 2. 已确认业务语义

### 2.1 分类目录是全局候选集

配置为分类来源的受管目录，其经过 Profile 识别为 `CATEGORY` 的目录路径共同形成一套全局分类候选集。
候选集不属于某个用户、某次 AgentRun 或某一个待分类目录。

全局分类候选必须同时供以下对象使用：

- 新上传文件。
- `NONE` 受管目录中的文件。
- `PATH_AS_WEAK_LABEL` 受管目录中的文件。
- `PATH_AS_CATEGORY` 分类来源目录中已有的文件。
- 其他受管根和后续新增受管根中的文件。

分类时不能因为文件位于某个受管根，就只在该根的子目录中查找分类。

### 2.2 目录位置只是弱信号

`PATH_AS_CATEGORY` 的正确含义是：

```text
受管目录中经过审核的 CATEGORY 路径
-> 可以进入全局分类候选目录
```

它不表示：

```text
文件位于该目录
-> 文件已经被确认属于该分类
```

因此，任何文件的当前父目录都只能提供弱位置证据，不能单独形成 `CONFIRMED_AS`，也不能成为跳过正文分类的理由。

### 2.3 一个文件允许多个分类

分类关系是多对多逻辑标签关系：

```text
一个 DocumentVersion
-> 可以有多个 SUGGESTED_AS
-> 可以有多个 CONFIRMED_AS
```

不同分类可以来自不同业务分支。系统不能只保留最高分，也不能把物理父目录当作唯一主分类。
每个分类建议必须分别保存分类 ID、完整路径、分量分数、置信度、状态、来源和正文证据。

### 2.4 物理位置与逻辑分类分离

当前阶段只处理逻辑分类，不执行文件移动：

```text
(DocumentVersion)-[:LOCATED_IN]->(ManagedFolder)
(DocumentVersion)-[:PATH_SUGGESTS]->(Category)
(DocumentVersion)-[:SUGGESTED_AS]->(Category)
(DocumentVersion)-[:CONFIRMED_AS]->(Category)
```

- `LOCATED_IN`：客观物理位置。
- `PATH_SUGGESTS`：目录位置提供的弱分类信号。
- `SUGGESTED_AS`：系统基于正文生成的多标签建议。
- `CONFIRMED_AS`：用户明确接受或更正后形成的确认关系。

是否把一个源文件移动、复制或链接到多个物理目录，后续通过独立 OperationPlan 方案讨论。

## 3. 当前实现与缺口

当前已有能力：

- 受管目录可扫描、搜索、排除隐藏文件并安全解析逻辑路径。
- `managed-file-read-document` 可为受管文件创建或复用只读快照，并写入 `document_pages`。
- `DocumentClassificationService` 可从完整正文生成多个分类建议。
- `document_classification_runs`、`document_category_suggestions` 和反馈表可保存逐文件多标签结果。
- ChangeSet 已支持受管快照、正文提取、分类建议和失败记录。
- Neo4j 已支持目录、分类、文件位置、弱标签、确认分类和语义向量投影。

必须先用测试证明并修复以下缺口：

1. “对党办下文件进行分类”当前没有稳定路由到受管文件分类链路。
2. Planner 的普通分类分支要求上传附件 `document_ids`，会把受管目录请求误判为缺少文件范围。
3. 当前动态分类 ID 包含 `root_key`；相同分类路径来自多个根时会形成重复分类节点。
4. 当前 `PATH_AS_CATEGORY` 可能把文件位置提升为受管路径确认分类，与本计划语义冲突。
5. 当前目录 Profile 主要约束弱标签模式；`PATH_AS_CATEGORY` 仍可能把年份、批次和临时目录映射成分类。
6. 受管文件读取单次最多处理 20 个文件，没有完整的目录批量分类任务和异步回执。
7. 受管目录分类入口、上传文件分类入口尚未显式共享一个版本化全局候选目录服务。

## 4. 模式重新定义

### 4.1 `PATH_AS_CATEGORY`

- 该根是全局分类目录来源。
- 仍必须经过 Profile，只允许 `CATEGORY` 角色进入分类候选集。
- `DEPARTMENT` 可以作为分类路径中的业务域，但不能仅凭名称自动成为叶子分类。
- `YEAR`、`COLLECTION`、`TEMPORARY` 和 `UNKNOWN` 不得自动成为分类。
- 文件位于分类目录下只生成弱位置证据，不生成确认分类。

### 4.2 `PATH_AS_WEAK_LABEL`

- 该根默认不是新增全局分类的来源。
- 文件位置可以与已有全局分类进行规范化匹配，并生成 `PATH_SUGGESTS`。
- 无法映射到已有分类时只保留 `LOCATED_IN`，不得自动创建新分类路径。

### 4.3 `NONE`

- 既不贡献全局分类目录，也不产生路径分类弱信号。
- 文件仍可依据正文匹配全局分类候选。

## 5. 目标架构

```text
用户消息
-> Intent Planner 判断 CLASSIFY_MANAGED_FILES
-> Managed Scope Resolver 确定 root_key/path_prefix/过滤条件
-> GlobalManagedCategoryCatalogService 加载全局候选目录
-> classify-managed-files Tool 创建同步批次或异步 Job
-> ManagedFileSnapshotService 创建/复用只读快照
-> extract-document-text 写入/复用 document_pages
-> DocumentClassificationService 基于完整正文执行多标签分类
-> ClassificationRepository 保存运行和多条建议
-> ChangeSet 记录逐文件分类结果
-> Neo4j 异步投影多标签关系
-> Agent 返回逐文件回执
```

LLM 只负责判断用户是否要分类，以及提取高层过滤意图。真实受管根、目录范围、文件集合和 Tool 参数必须由后端确定性服务解析与校验。

## 6. 全局分类目录服务

新增 `GlobalManagedCategoryCatalogService`，作为上传文件和受管文件分类共同依赖。

业务分类目录来源规则：

- 只要存在启用的 `PATH_AS_CATEGORY` 分类来源根，业务分类候选就以全局受管目录为准。
- 不得把项目预置 taxonomy 的业务分类与受管目录分类静默混合，避免同一文件得到两套口径不同的分类。
- 项目预置 taxonomy 如需保留，只能承担文档类型等明确独立维度，不能覆盖受管目录业务分类。
- 分类来源配置存在但目录为空或 Profile 无有效 `CATEGORY` 时，返回 `CATEGORY_CATALOG_EMPTY` 并进入 `NEEDS_REVIEW`，不得静默切换分类体系。

职责：

1. 读取所有启用且模式为 `PATH_AS_CATEGORY` 的受管根。
2. 加载对应版本化目录 Profile。
3. 只保留 Profile 角色为 `CATEGORY` 的路径。
4. 规范化空格、路径分隔符和显示名称。
5. 按规范化完整分类路径全局去重。
6. 生成不依赖 `root_key` 的稳定 `category_id`。
7. 保存该分类来自哪些受管根和目录，供审计使用。
8. 生成目录快照版本或内容哈希，作为 `taxonomy_version` 的组成部分。

建议稳定 ID：

```text
managed.global.<sha256(normalized_category_path)[:24]>
```

同一规范化路径来自多个根时映射到同一个 `Category`，但保留多个 `ManagedFolder -[:MAPS_TO]-> Category` 来源关系。

返回结构至少包含：

```json
{
  "category_id": "managed.global.xxxxx",
  "category_path": ["人事处", "职称评定"],
  "name": "职称评定",
  "aliases": [],
  "source_roots": ["classified_archive"],
  "source_folders": ["人事处/职称评定"],
  "taxonomy_key": "managed_global_categories",
  "taxonomy_version": "managed-global-<profile-and-path-hash>"
}
```

## 7. Planner 与范围解析

### 7.1 新意图与能力

新增：

- Intent：`CLASSIFY_MANAGED_FILES`。
- Capability：`managed_file_classification`。
- Tool：`classify-managed-files`。

Planner 必须支持：

```text
对党办下文件进行分类
对党办/2026下所有 PDF 分类
对 downloads 下文件名包含“科学发展观”的文件分类
重新按正文分类党办下的文件
```

### 7.2 确定性范围解析

新增 `_managed_file_classification_filters_from_request()`，解析：

- `root_key`
- `path_prefix`
- `extension`
- `filename_contains`
- `recursive`
- `force_reprocess`

处理顺序必须位于普通上传附件分类分支之前。

范围规则：

- 完整 `root_key/path_prefix` 优先。
- 用户只说唯一子目录名时，可从所有启用受管根中解析唯一逻辑目录。
- 同名目录出现在多个位置时，只要求用户确认目录范围，不要求逐个确认文件。
- 文件集合由数据库中的活动 `managed_files` 确定，排除隐藏项和 `MISSING`。
- LLM 输出的目录名称不能直接转换为服务器绝对路径。

## 8. 批量分类 Tool 与 Job

`classify-managed-files` 输入建议：

```json
{
  "root_key": "downloads",
  "path_prefix": "党办/2026",
  "extension": "pdf",
  "filename_contains": null,
  "recursive": true,
  "force_reprocess": false
}
```

执行策略：

- 匹配数量不超过同步阈值时，可在当前 AgentRun 中直接处理。
- 超过阈值时创建 `filesystem_jobs.job_type=CLASSIFY_MANAGED_FILES`。
- Worker 按配置批次领取文件，每个文件使用独立事务保存点。
- 一个文件失败不能回滚其他文件。
- Job 必须记录 `user_id`、`conversation_id`、`agent_run_id`、过滤条件、总数、成功、失败、跳过和复用数量。
- 大批量任务完成后必须更新原 AgentRun 结果，前端通过现有 Job 查询机制刷新回执。

缓存与重处理规则：

```text
source_sha256 未变化
+ extraction 已成功
+ taxonomy_version 未变化
+ classifier_version 未变化
-> 复用解析和分类结果

源文件变化 / Profile 变化 / taxonomy 变化 / 用户明确重新分类
-> 创建新的解析或分类运行
```

## 9. 多标签分类规则

每个文件按以下顺序处理：

```text
完整正文 / OCR / Sheet 内容
-> 全局候选召回
-> 文件名和标题信号
-> 当前目录弱信号
-> 语义相似文件与图谱候选
-> 负向信号过滤
-> 多标签排序与证据校验
-> 保存全部达到建议门槛的分类
```

要求：

- 分类候选不能按当前根或父目录裁剪。
- 当前目录只影响 `path_signal_score`，不能直接设置分类状态。
- 至少保留 Top N 多标签建议，不能只保留 Top 1。
- 不同分支的分类可以同时存在。
- 相同 `category_id` 必须去重并合并证据与分量分数。
- 非“其他”分类缺少可定位正文证据时必须为 `NEEDS_REVIEW`。
- 图谱不可用时仍使用全局目录、规则和正文完成基础分类。

候选分量至少包括：

```text
rule_score
semantic_score
graph_score
path_signal_score
confirmed_support_score
negative_penalty
```

## 10. 持久化与图谱关系

### 10.1 PostgreSQL 事实源

每个文件分类运行继续写入：

- `document_classification_runs`
- `document_category_suggestions`，一个分类一行
- `document_category_feedback`
- `AgentRun.graph_state_json.document_results`，只保存轻量回执
- `change_sets` / `change_items`

受管文件通过 `managed_file_snapshots` 关联源文件和快照 Document，不新增第二套分类建议表。

当前阶段分类建议不得自动写入正式 `document_categories`。只有用户明确接受或更正后，才进入后续正式关系流程。

### 10.2 Neo4j 可重建投影

目录投影：

```text
(ManagedRoot)-[:HAS_FOLDER]->(ManagedFolder)
(ManagedFolder)-[:MAPS_TO]->(Category)
```

文件位置和多标签分类投影：

```text
(DocumentVersion)-[:LOCATED_IN]->(ManagedFolder)
(DocumentVersion)-[:PATH_SUGGESTS]->(Category)
(DocumentVersion)-[:SUGGESTED_AS]->(Category)
(DocumentVersion)-[:CONFIRMED_AS]->(Category)
```

约束：

- `PATH_AS_CATEGORY` 只决定目录能否成为全局 `Category` 来源。
- `LOCATED_IN` 不得转写为 `CONFIRMED_AS`。
- `PATH_SUGGESTS` 不得作为用户确认事实。
- 一个 `DocumentVersion` 可以连接多个不同 `Category`。
- `CONFIRMED_AS` 只能来自明确接受或更正反馈。
- 普通 `SUGGESTED_AS` 不能作为强监督样本自我强化。

## 11. ChangeSet 与前端回执

批次 ChangeSet 至少记录：

- `MANAGED_FILE_SNAPSHOT_CREATED`
- `MANAGED_FILE_SNAPSHOT_REUSED`
- `TEXT_EXTRACTED` / `TEXT_REUSED`
- `CATEGORY_SUGGESTED` / `CATEGORY_SUGGESTION_REUSED`
- `DOCUMENT_PROCESSING_FAILED`

前端继续使用现有 `DocumentResultCard`，每个受管文件展示：

- 文件名和逻辑相对路径。
- 解析、OCR 或复用状态。
- 多个分类建议。
- 每个分类的置信度和证据。
- 失败和待复核原因。
- “本次仅生成分析和分类建议，原始文件未修改”。

不得展示服务器绝对路径，也不得把受管快照伪装成用户本次上传附件。

## 12. 开发阶段

当前实现状态：

| 阶段 | 状态 | 已实现内容 |
|---|---|---|
| A | 已完成 | 已补 Planner、全局目录、图谱关系、同步/异步批量和缓存回归测试。 |
| B | 已完成 | 已实现全局目录服务、Profile 过滤、跨根稳定 ID 和上传/受管文件共享目录版本。 |
| C | 已完成 | 已实现 Intent、Capability、Tool schema、范围解析、同步小批量和逐文件 ChangeSet。 |
| D | 已完成 | 已实现异步 Job、Worker 分批执行、单文件失败隔离、AgentRun 回写和前端轮询刷新。 |
| E | 已完成 | 已区分 `LOCATED_IN`、`PATH_SUGGESTS`、`SUGGESTED_AS` 和显式反馈产生的 `CONFIRMED_AS`。 |
| F | 待部署验收 | 图谱关闭时的基础分类已有自动化测试；Shadow 需在部署环境观察，不自动产生正式分类关系。 |

### 阶段 A：先补缺失测试

- 固化当前“对党办下文件进行分类”错误路由。
- 先写期望行为回归测试：`PATH_AS_CATEGORY` 只贡献全局分类候选，文件位置只能生成 `LOCATED_IN` 和 `PATH_SUGGESTS`，不得生成 `CONFIRMED_AS`；该测试应在修复前失败、修复后通过。
- 证明相同分类路径在不同根中产生重复 ID。
- 证明上传文件与受管文件尚未显式共享同一个全局目录版本。

### 阶段 B：全局分类目录

- 新增 `GlobalManagedCategoryCatalogService`。
- 统一 Profile 角色过滤。
- 改为不含 `root_key` 的全局稳定分类 ID。
- 为现有 `DocumentClassificationService` 注入全局目录 Provider。
- 保留旧 `managed_category_id` 的兼容映射，避免历史建议无法读取。

### 阶段 C：对话入口和同步小批量

- 新增 Intent、Capability、schema 和 Tool。
- 接通受管目录范围解析。
- 小批量复用快照、解析、分类和 ChangeSet。
- 返回逐文件多标签结果。

### 阶段 D：异步大批量

- 扩展 `filesystem_jobs` 和 worker。
- 增加进度、失败隔离、重试和 AgentRun 完成回写。
- 前端自动轮询 Job，并在完成后刷新当前对话结果。

### 阶段 E：图谱关系纠正

- 删除从文件位置直接生成 `CONFIRMED_AS` 的投影逻辑。
- 将目录位置统一投影为 `PATH_SUGGESTS`。
- 投影多条 `SUGGESTED_AS` 和用户反馈形成的多条 `CONFIRMED_AS`。
- 对旧 Neo4j 投影执行可重建清理，不修改 PostgreSQL 历史事实。

### 阶段 F：Shadow 与验收

- 基础分类先在图谱关闭模式验收。
- 开启 Shadow，确认图谱只增强候选而不改变可见结果。
- 通过用户明确接受、拒绝和更正积累多标签评测样本。
- 未形成冻结评测集前，不自动形成正式分类关系。

## 13. 重点修改文件

后端预计涉及：

- `apps/api/app/modules/agent/planner.py`
- `apps/api/app/modules/agent/capabilities/catalog.json`
- `apps/api/app/modules/agent/tool_schemas.py`
- `apps/api/app/modules/agent/tool_registry.py`
- `apps/api/app/modules/llm/schemas.py`
- `apps/api/app/modules/llm/prompts.py`
- `apps/api/app/modules/managed_files/repository.py`
- `apps/api/app/modules/managed_files/jobs.py`
- `apps/api/app/modules/managed_files/worker.py`
- `apps/api/app/modules/managed_files/snapshot_service.py`
- `apps/api/app/modules/classification/classifier_service.py`
- 新增 `apps/api/app/modules/classification/managed_catalog.py`
- `apps/api/app/modules/classification/repository.py`
- `apps/api/app/modules/knowledge_graph/projection_service.py`
- `apps/api/app/modules/knowledge_graph/neo4j_repository.py`
- `apps/api/app/modules/changesets/service.py`

前端预计涉及：

- `apps/web/src/features/chat/AgentRunReceipt.tsx`
- `apps/web/src/features/chat/DocumentResultCard.tsx`
- `apps/web/src/features/chat/CategoryChip.tsx`
- `apps/web/src/api/client.ts`

## 14. 测试清单

Planner：

- “对党办下文件进行分类”生成 `CLASSIFY_MANAGED_FILES`。
- 深层目录、扩展名和文件名条件正确进入 Tool schema。
- 同名目录歧义返回目录选择，不猜测路径。
- 没有上传附件也能识别受管文件分类。
- 上传附件分类行为保持不变。

全局目录：

- 不同根中的相同分类路径合并为同一 ID。
- 所有待分类根和上传文件都能读取同一候选集。
- 年份、批次、临时和未知目录不进入分类目录。
- Profile 或目录变化会生成新版本。

分类：

- 一个文件可以保存多个不同分支分类。
- 当前父目录不是唯一候选，也不会形成确认分类。
- 文件名、正文和路径冲突时以正文证据为主。
- 多标签排序、去重和证据合并正确。
- 无证据候选进入 `NEEDS_REVIEW`。

批量：

- 多个文件全部处理并保持顺序。
- 单文件失败不影响其他文件。
- 未变文件复用快照、解析和分类。
- 源文件变化后创建新快照和分类运行。
- 隐藏文件和 `MISSING` 文件不参与任务。
- 大批量 Job 可查询进度并最终回写 AgentRun。

图谱：

- `PATH_AS_CATEGORY` 目录只创建分类来源，不创建文件确认分类。
- `LOCATED_IN` 与多条分类关系彼此独立。
- 一个文件可投影多条 `SUGGESTED_AS` 和 `CONFIRMED_AS`。
- 用户接受、拒绝和更正能正确更新可重建投影。
- Neo4j 关闭或故障时基础多标签分类继续成功。

安全与回执：

- 不修改受管源文件。
- 不泄露绝对路径和其他用户信息。
- ChangeSet 逐文件记录成功、失败、跳过和复用。
- 刷新会话历史后仍能展示完整多标签结果。

## 15. 验收标准

1. 用户输入“对党办下文件进行分类”能稳定解析受管目录范围并启动分类。
2. 分类使用所有分类来源根共同生成的全局候选集。
3. 新上传文件和受管目录文件使用同一全局目录版本和统一分类服务。
4. 配置受管分类来源后，系统不会静默混入或回退到另一套预置业务分类目录。
5. 文件所在父目录只作为弱信号，不生成确认分类，也不跳过正文处理。
6. 一个文件可以持久化并展示多个分类，且分类可来自不同业务分支。
7. 每个非“其他”分类有可定位正文证据，否则进入 `NEEDS_REVIEW`。
8. 多文件批次逐文件失败隔离，大批量任务不会阻塞 API 请求。
9. 分类只生成逻辑建议，不移动、复制、删除或覆盖源文件。
10. PostgreSQL 是事实源，Neo4j 投影可以清理并完整重建。
11. 图谱关闭、未安装或故障时，基础分类和上传链路无损降级。

## 16. 明确后续再做

- 根据主分类自动选择目标物理目录。
- 将一个源文件复制、链接或导出到多个分类目录。
- 批量移动前的 OperationPlan 和冲突策略。
- 分类关系管理后台和人工目录治理界面。
- 基于冻结反馈集自动调优多标签阈值和权重。

## 17. 本次实现结果

已完成：

- 新增全局受管分类目录服务，分类 ID 不再包含 `root_key`，相同完整路径跨根去重。
- `PATH_AS_CATEGORY` 和目录位置不再生成 `CONFIRMED_AS`；只保留目录来源映射、`LOCATED_IN` 和 `PATH_SUGGESTS`。
- 新增 `CLASSIFY_MANAGED_FILES`、Capability、Planner 范围解析、Tool schema 和白名单 Tool。
- 小批量受管文件复用快照与全文解析，生成逐文件多标签建议、分类运行和 ChangeSet。
- 大批量自动创建 `CLASSIFY_MANAGED_FILES` Job，worker 分页处理、隔离单文件失败并回写原 AgentRun。
- 前端支持自动轮询异步分类 Job；页面刷新后仍会继续跟踪等待中的 AgentRun。
- 上传文件和受管文件统一使用 `DocumentClassificationService` 和同一全局目录版本。
- 分类缓存按文件版本、taxonomy 版本和 classifier 版本复用；“重新分类”显式绕过缓存。
- Neo4j 投影支持多条 `SUGGESTED_AS`，只有用户接受或更正生成 `CONFIRMED_AS`。

已完成自动化验证：Planner 路由、跨根目录去重、Profile 角色过滤、同步分类持久化、
异步 Job 回写、分类复用、目录弱关系、多标签建议投影、前端 TypeScript 与生产构建。

待部署环境验证：真实 Neo4j 连接、全量投影清理重建、Shadow 对照指标和生产规模性能。
