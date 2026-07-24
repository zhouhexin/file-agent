# 阶段四 CPU 默认两阶段文件检索开发计划

- 状态：代码实施完成；真实 PostgreSQL 升降级与 Windows 文件烟测按第 14 节在部署环境执行后关闭阶段
- 阶段目标：让普通用户通过自然语言稳定找到已经自动整理的活动工作副本
- 运行模式：CPU-only、低内存、无 GPU、默认不调用外部模型或互联网
- 上位方案：`docs/automatic-organization-conversational-access-implementation-plan.md`
- 前置条件：阶段三 PostgreSQL migration 与真实文件索引烟测通过
- 评审修订：数据冗余建议部分采纳，保留工作副本级瘦检索投影，不以 `DocumentSummary` 或物化视图
  替代在线主链路；中文检索建议采纳为“精确匹配、Jieba/GIN 主召回、受限 `pg_trgm` 补召回”

## 1. 本阶段解决的问题

阶段四直接服务于“上传后自动整理、以后通过对话访问文件”的产品闭环。用户不需要知道目录、
Skill、Tool、Chunk、全文索引或融合算法，只需要说：

```text
找我去年的奖学金材料。
找学生工作处发的资助通知。
刚才那批文件里，哪个提到了家庭经济困难认定？
找和国家励志奖学金申请有关的文件。
```

系统返回逐文件结果，包括整理后的文件名、分类、概览、用户可理解的命中原因和可定位位置。
搜索是只读操作，不修改原件或活动工作副本，不生成 OperationPlan。

本阶段不生成正式事实回答。用户追问“文件中具体怎么规定”时，本阶段只负责找出候选文件和原文
Evidence；带引用的正式回答、`qa_answers` 和 `answer_references` 持久化属于阶段五。

## 2. CPU 词法检索默认部署边界

默认配置必须保持：

```text
RETRIEVAL_MODE=lexical
CHINESE_TOKENIZER=jieba
EMBEDDING_ENABLED=false
EMBEDDING_PROVIDER=disabled
GRAPH_EMBEDDING_ENABLED=false
```

Neo4j 和现有 Graph 分类能力可以按既有部署要求继续启动，但阶段四文件检索不调用 Graph/GraphRAG，
也不要求为了搜索而停止已有 Graph 功能；两条运行链路必须相互独立。

阶段四不得：

- 下载或启动本地 embedding、reranker、LLM 或向量推理模型。
- 要求服务器安装 GPU、CUDA、独立向量服务、Redis、Celery 或新的常驻搜索进程。
- 在请求内扫描文件系统、重新解析文件、执行 OCR、构建 Chunk 或回填 embedding。
- 把全量文件摘要或正文加载到 Python 内存后再搜索。
- 把正文、Chunk、分词文本、向量、绝对路径或内部得分写入 AgentGraphState、日志或普通用户响应。
- 因 Neo4j、GraphRAG 或 embedding 不可用而阻断文件搜索。

阶段四 CPU 词法默认路径只复用 PostgreSQL、阶段三已有 GIN/`pg_trgm` 索引和应用层 Jieba。
SQLite 只用于确定性单元测试，不作为生产检索能力承诺。这里描述的是当时无 GPU 部署的默认检索
实现，不把项目运行能力定义为“低耗模式”；后续“低耗”一词只用于描述 LLM 调用预算。

## 3. 目标数据流

```text
用户自然语言消息
→ 后端解析主题、年份和明确文件范围
→ 确定 L0 当前附件 / L1 当前会话 / L4 当前用户逻辑可见的共享工作目录
→ 第一阶段：索引化的文件名、分类、年份、实体和摘要召回
→ 必要时：对原文 Chunk GIN 索引做一次有上限的候选补召回
→ 合并少量候选 DocumentVersion
→ 第二阶段：只在候选版本内检索最相关 Chunk/Evidence
→ 确定性融合、所有权与当前版本校验
→ UserTaskReceipt 文件搜索投影
→ 聊天页逐文件搜索结果卡
```

“摘要优先”不等于“摘要决定事实”。摘要只负责廉价缩小范围。如果摘要遗漏查询主题，系统必须用
原文 Chunk 索引补充候选，不能据此认定文件不相关。

## 4. 检索范围与权限

### 4.1 范围优先级

- `L0`：当前消息明确上传、点名或引用的文件。
- `L1`：当前会话已经上传、打开、引用或返回过的文件。
- `L4`：唯一系统共享工作目录中、当前用户逻辑可见且状态为 `ACTIVE` 的工作副本。default workspace
  仅保存会话与上传来源，不能再用于派生物理文件副本。

同等相关时按 `L0 > L1 > L4` 排序。范围只能由后端根据真实消息附件、会话记录、
`working_copy_id` 和所有权解析；Planner 或 LLM 不能自行猜测文件 ID。

### 4.2 严格范围与排序范围

- 用户说“这些文件”“刚上传的文件”“第二个附件”时，只搜索后端解析出的明确 L0 范围。
- 用户点名会话中的某个文件时，搜索精确文件或明确 L1 集合。
- 用户说“找我的……材料”等全局请求时，搜索 L4；L1 只提供排序加权，不能排除工作区内更相关文件。
- 不能唯一解析文件或目录时，停止扩大范围并请求用户补充完整名称或选择项。

### 4.3 所有权与状态过滤

每条查询都必须同时校验：

- `Document.user_id` 等于当前用户。
- `WorkingCopy.workspace_id` 属于当前用户 default workspace。
- `WorkingCopy.status = ACTIVE`。
- 结果版本等于 `WorkingCopy.current_version_id`。
- 对应 `DocumentIndexRun.status = COMPLETED`。

默认排除原始归档、隐藏临时文件、回收站文件、旧内容版本和其他用户文件。即使索引投影数据陈旧，
最终所有权与活动版本校验也不能省略。

## 5. 第一阶段：低成本文档级召回

### 5.1 新增可重建的工作副本级瘦检索投影

新增派生表 `document_search_profiles`，一条记录对应一个活动工作副本的当前版本。建议字段：

```text
id
user_id
workspace_id
working_copy_id
document_id
document_version_id
status
normalized_filename
filename_search_text
category_search_text
metadata_search_text
summary_search_text
combined_search_text
search_vector
source_fingerprint
created_at
updated_at
```

约束和索引：

- `working_copy_id` 唯一；切换内容版本时幂等更新，而不是产生多个活动投影。
- `(user_id, workspace_id, status)` B-tree 索引。
- `normalized_filename` 使用 B-tree 支持规范化后的完整文件名精确匹配。
- `search_vector` 使用 PostgreSQL `simple` 配置和 GIN 索引。
- 文件名、分类、元数据和摘要分别由应用层 Jieba 产生稳定词项，再通过 `setweight` 组成
  `search_vector`；不得保存任意 LLM 查询改写结果。
- `normalized_filename` 可以增加 `pg_trgm` GIN 索引，但只用于受限的长文件名短语和轻微错字补召回，
  不能代替中文分词全文索引。
- `source_fingerprint` 覆盖当前版本、最终文件名、采用的摘要记录、分类运行和分词器/业务词典版本，
  用于发现投影陈旧或判断是否需要重建。

这是可重建的检索派生数据，不替代 `WorkingCopy`、`DocumentSummary`、分类建议或 Evidence 等事实表。
投影损坏时可以重建，不能反向修改文件客观事实。

投影必须保持“瘦”：不复制完整 `category_paths_json`、年份/关键词/实体 JSON、`summary_preview` 或正文。
第一阶段只从投影返回 `working_copy_id`、`document_version_id`、排序信号和命中来源；候选数量收敛后，
再通过一次批量 JOIN 从事实表读取当前文件名、全部有效分类、年份和摘要，禁止逐文件 N+1 查询。

本阶段不采用以下两个替代方案：

- 不用 `DocumentSummary.search_vector` 完全替代该投影。摘要按文档版本、Provider 和缓存指纹管理，搜索
  范围却按工作副本、当前版本、用户、工作区和 `ACTIVE` 状态管理；改名、回收站、恢复和当前版本切换
  都可能独立于摘要发生，只在摘要表加列无法完整表达实时搜索边界。
- 不把 PostgreSQL 物化视图作为在线主链路。物化视图仍是需要显式刷新且允许陈旧的快照，不能直接
  承担改名、回收站和恢复后的即时可见性；应用层 Jieba 词项也必须先有可靠的数据来源。以后可以把
  物化视图用于离线统计或只读报表，但不能替代本阶段的幂等瘦投影。

### 5.2 投影更新时机

在以下事件后调用同一个幂等 `DocumentSearchProfileService.upsert_current_profile`：

- 首次整理完成、工作副本提交为 `ACTIVE`。
- 当前内容版本改变并且新版本索引完成。
- 用户确认后完成活动工作副本重命名。
- 摘要或多标签分类建议更新。
- 回收站恢复为 `ACTIVE`。

进入回收站时把投影标为非活动或删除。改名、移动不重建 Chunk；只更新文件名和范围投影。

工作副本首次提交、确认后改名、进入回收站和恢复等同步业务写入，应尽量在同一数据库事务内更新或
失效投影；摘要和分类异步完成后使用事件驱动的幂等 upsert。任何事件重试都不得产生重复投影。

增加一次性 backfill 命令或管理服务，为阶段四启用前已有的活动工作副本补齐投影。backfill 分页执行，
单批默认 100 条，不把整个工作区加载到内存。

同时增加分页 reconciliation：对比事实表当前状态与 `source_fingerprint`，修复漏事件、失败重试或
历史数据造成的缺失/陈旧投影。在线查询仍必须再次校验所有权、`ACTIVE` 状态和当前版本，不能因为投影
存在就信任其为最终事实。

### 5.3 查询解析

新增确定性的 `FileSearchQueryParser`，只解析受控字段：

- 去除“帮我找、查一下、文件、材料”等低信息量请求词。
- 使用 Jieba 与业务词典提取主题词。
- 用服务器时区确定性解析“今年、去年、前年”和显式年份。
- 提取已存在 taxonomy 别名、单位、人名、文号和文档类型候选。
- 生成绑定参数，不允许把用户文本拼接为 SQL 或原生 tsquery。

解析失败时保留安全的原始关键词检索；不能调用外部 LLM 兜底。

### 5.4 数据库侧召回

第一阶段只执行索引查询并返回少量候选，不能再沿用“先加载最近 500 个摘要，再在 Python 中遍历
评分”的方式。默认候选上限为 30，硬上限为 50。

召回信号包括：

- 规范化后的最终文件名精确匹配。
- Jieba 分词后的中文文件名 GIN 主召回。
- 所有有效多标签分类，不只读取排名第一的分类。
- 年份、关键词、实体、单位、文号和文档类型。
- 普通摘要。
- 受限的 `pg_trgm` 文件名模糊补召回。

中文文件名按以下顺序召回：

1. `normalized_filename` 完整精确匹配。
2. `filename_search_text` 经 Jieba 分词后进入 `search_vector`，使用 GIN 作为中文主题和文件名主召回。
3. 只有规范化查询达到配置的最小长度、精确匹配和全文召回不足时，才启用 `pg_trgm` 处理长短语、
   少量错字或业务词典未覆盖的情况；中文默认最小长度建议为 4，短查询必须跳过该分支。
4. 文档级结果不足时，再按第 6 节使用 Chunk `search_vector` GIN 补召回。

`pg_trgm` 使用连续三字符片段，并非中文语义分词。GIN trigram 查询必须先使用可命中索引的相似度或
模式谓词收窄集合，再排序并应用候选硬上限；如果需要纯距离 Top-K，必须通过 PostgreSQL
`EXPLAIN (ANALYZE, BUFFERS)` 评估 GiST，而不能假定 GIN 会执行最近邻检索。无法提取有效 trigram 的
短查询不得进入可能退化为大范围扫描的模糊分支。

第一阶段索引查询只携带稳定业务 ID、排序信号和命中来源，不返回正文。候选收敛后以一次批量查询补齐
受控概览和显示字段，所有权、活动状态与当前版本校验必须基于事实表再次执行。

## 6. 摘要遗漏时的原文候选补召回

为了满足“摘要未写到、正文实际存在时仍能找到”，需要保留一个有严格上限的原文索引补召回分支：

- L0/L1 明确范围不超过候选上限时，直接在这些版本内执行 Chunk 词法检索。
- L4 全工作区查询中，如果第一阶段没有强主题命中、候选不足，或最长业务词没有命中，执行一次全局
  Chunk GIN 候选查询。
- 全局补召回必须联结当前用户 `ACTIVE` 工作副本和当前版本，按版本聚合，最多补充 10 个版本。
- 该查询只返回 `document_version_id`、最佳 Chunk ID、位置和分数，不读取或返回整段正文。
- 全局补召回失败时降级为第一阶段结果并给出“部分原文索引暂不可用”的用户可理解提示，不让整个
  文件查找失败。

此分支复用阶段三索引，不增加常驻进程，也不构建第二份全文索引。

## 7. 第二阶段：候选版本内 Chunk/Evidence 检索

复用并扩展 `DocumentChunkLexicalSearchService`：

- 输入只能是服务端已校验的候选 `document_version_ids`。
- 默认最多精查 12 个候选版本，硬上限 20。
- 每个版本最多保留 3 个 Chunk，全局最多 24 个 Chunk。
- PostgreSQL 使用 Jieba 词项对应的 `search_vector` GIN 为主；`pg_trgm` 只在满足最小查询长度和
  候选硬上限时做长短语、错字或分词遗漏补充。
- SQLite 继续使用 deterministic token coverage，仅保护业务逻辑测试。
- embedding 分支保留接口，但 `EMBEDDING_ENABLED=false` 时不得初始化 provider 或发起任何调用。

新增 `SearchEvidenceProjector`，按 Chunk ID 读取已持久化 Evidence 并再次校验用户、工作副本、当前版本
和索引运行。普通搜索结果最多展示一条短预览和位置：

- PDF：页码。
- Word/TXT/MD：页或段落定位信息；没有可靠页码时不能伪造。
- Excel/XLS：Sheet 和单元格范围。

短预览用于解释“为什么推荐这个文件”，不代表阶段五的正式事实回答或持久化引用。

## 8. 确定性融合与排序

新增 `TwoStageFileSearchService` 作为唯一编排入口。它组合文档召回、原文补召回和候选 Chunk 检索，
但不直接访问文件系统。

排序使用版本化、可测试的确定性权重，建议初始权重：

```text
文档级文件名/分类/元数据/摘要相关度：40%
候选内最佳 Chunk 词法相关度：35%
范围优先级 L0/L1/L4：20%
轻量时间并列项：5%
```

实施时先把不同检索分数按本次候选集合归一化到 `[0, 1]`，再融合；精确文件名、明确年份和完整文号
可以获得固定加权。向量分支关闭时，其权重重新分配给 Chunk 词法相关度，不能留下空分导致整体降分。

排序必须满足：

- 同等相关时 `L0 > L1 > L4`。
- 正文强命中可以超过只有模糊摘要命中的文件。
- 最近时间只能作为并列项，不能压过更相关的旧文件。
- 最终并列使用稳定 `working_copy_id`，保证测试和分页顺序稳定。
- 分数只用于后端排序；普通用户看到“文件名命中、分类命中、正文第 2 页命中”等原因，不显示内部
  数值和算法术语。

## 9. Agent、API 与普通用户回执

### 9.1 Agent Runtime

- 保留 `SEARCH_FILES` 意图和 `hybrid-search` 内部白名单 Tool 名称，避免破坏已有审计契约。
- Tool handler 改为调用 `TwoStageFileSearchService`，不再只调用摘要检索。
- `ConversationAttachmentContextService` 继续负责真实附件范围；新增会话文件范围读取服务负责 L1。
- Planner 只输出查询和声明式范围意图，真实 `user_id`、`workspace_id`、`conversation_id` 和文件 ID 由
  Runtime 注入并校验。
- `result_summary.workspace_file_search` 只保存轻量搜索投影，不能保存 Chunk 正文或运行依赖。
- 搜索为只读操作，不创建 ChangeSet；ToolInvocation 继续记录安全输入摘要、状态和耗时。

### 9.2 API

普通用户主入口保持：

```text
POST /api/conversations/{conversation_id}/messages
```

补齐或复用：

```text
POST /api/search
```

两个入口必须调用同一个检索服务和权限校验。`/api/search` 是兼容能力，不能替代聊天主入口，也不能
接受宿主机路径、任意用户 ID 或未校验的 DocumentVersion ID。

### 9.3 UserTaskReceipt

扩展普通用户投影，建议增加：

```text
response_type = file_search_results
search_result:
  query
  total_returned
  files[]
  partial
  user_message
```

每个 `files[]` 只允许包含：

- `working_copy_id`
- `document_id`
- 当前 `document_version_id`
- 整理后的 `filename`
- 分类路径、年份和简短概览
- 用户可理解的 `match_reasons`
- 页码或 Sheet/单元格等 `match_location`
- 受限长度的 `evidence_preview`
- 打开、下载或继续询问所需的稳定业务 ID

不得包含 Skill、Tool、AgentRun、内部队列、原文件路径、上传原文件名、SQL 分数、任何 `*_search_text`、
embedding 或完整正文。

## 10. 前端任务

聊天页新增或补齐文件搜索结果卡：

- 每个文件独立展示整理后名称、分类、概览、推荐原因和原文位置。
- 支持按稳定业务 ID 打开详情或下载，不依赖相对路径。
- 零结果时提示用户补充主题、年份、单位或文档类型。
- 部分降级时显示“部分文件原文索引暂不可用”，不能显示异常栈或内部服务名。
- 默认展示前 10 个结果；“查看更多”每次最多再取 10 个，不能一次渲染全工作区。
- 普通用户界面不得出现 Tool、Skill、Chunk、FTS、向量、Graph 或内部相关度数字。

## 11. 资源保护与降级

建议新增以下配置，并在 `README.md`、`.env.example` 和 `docs/runbook.md` 同步说明：

```text
TWO_STAGE_RETRIEVAL_ENABLED=true
RETRIEVAL_DOCUMENT_CANDIDATE_LIMIT=30
RETRIEVAL_DOCUMENT_DETAIL_LIMIT=12
RETRIEVAL_CHUNK_LIMIT_PER_DOCUMENT=3
RETRIEVAL_CHUNK_GLOBAL_LIMIT=24
RETRIEVAL_QUERY_MAX_CHARS=500
RETRIEVAL_PREVIEW_MAX_CHARS=240
RETRIEVAL_STATEMENT_TIMEOUT_MS=2000
RETRIEVAL_FILENAME_TRGM_MIN_CHARS=4
RETRIEVAL_FILENAME_TRGM_CANDIDATE_LIMIT=20
RETRIEVAL_FILENAME_TRGM_SIMILARITY_THRESHOLD=0.25
```

代码中还要设置不可被环境变量放大的硬上限。单次检索最多执行固定数量的索引查询，不允许 N+1
逐文件查询。trigram 相似度阈值必须设置安全下限，部署配置不能把它降为近似无过滤；应用层只保留
候选投影和少量 Evidence 预览，不能保留全文。

降级顺序：

1. 第二阶段正常：返回文档级结果、正文命中位置和短预览。
2. Chunk 查询超时或索引暂缺：返回可靠的第一阶段文件结果并标记部分降级。
3. 检索投影缺失：对明确 L0 文件做小范围受控查询；L4 不做全表 Python 扫描。
4. PostgreSQL 检索不可用：返回结构化失败和重试建议，不伪造“没有文件”。

若候选版本尚未完成阶段三索引，只能返回“文件已找到但原文索引尚未就绪”，不得在搜索请求中同步
解析或索引该文件。

## 12. 实施顺序与代码边界

### 任务 4.0：前置基线

1. 在 PostgreSQL 执行阶段三 Alembic migration。
2. 用 PDF、DOCX、XLSX、TXT 各完成一次索引烟测。
3. 确认现有后端全量 pytest 和前端 build 通过。
4. 固化搜索回归样本，至少包含摘要命中和摘要遗漏但正文命中两类。

退出条件：阶段三真实索引可用，当前工作树只含阶段四相关变更。

### 任务 4.1：检索投影与迁移

1. 新增瘦 `DocumentSearchProfile` ORM 模型和 Alembic migration，不复制分类、实体和摘要的完整显示
   JSON。
2. 实现投影 upsert、失效、分页 backfill、分页 reconciliation 和 fingerprint 幂等判断。
3. 接入首次整理、内容新版本、确认后重命名、分类更新、回收站和恢复事件；同步工作副本变更尽量与
   投影更新处于同一事务，异步摘要/分类事件必须可重试。
4. 为规范化文件名添加精确匹配索引，为 Jieba 词项 `search_vector` 添加 GIN 主索引，并为满足长度门槛
   的原始文件名模糊补召回添加受限 `pg_trgm` 索引。
5. 保留 SQLite deterministic 测试降级，并用测试确认线上链路不依赖物化视图刷新。

退出条件：重复构建不产生重复记录，漏事件可由 reconciliation 修复，重命名不重建 Chunk，回收站
文件不再被召回，显示数据只在候选收敛后批量读取。

### 任务 4.2：查询解析与范围解析

1. 实现 `FileSearchQueryParser`。
2. 实现 `FileSearchScopeResolver`，明确 L0/L1/L4、严格范围和排序范围。
3. 后端注入用户、工作区和会话上下文，拒绝 Planner 伪造 ID。
4. 添加年份、附件指代、会话文件和歧义范围测试。

退出条件：范围确定、跨用户隔离、相对时间解析可重复验证。

### 任务 4.3：第一阶段数据库召回

1. 把摘要检索从 Python 全量候选遍历改为 PostgreSQL 索引查询。
2. 按“规范化文件名精确匹配 → Jieba/GIN 文件名与文档信号主召回 → 受限 `pg_trgm` 补召回”实现
   文档级候选查询。
3. 对全部有效分类、年份、实体、关键词和摘要词项执行加权召回；第一阶段只返回稳定 ID、分项得分和
   命中来源。
4. 候选收敛后用一次批量 JOIN 补齐真实文件名、分类、年份和摘要，禁止逐文件读取。
5. 对 trigram 分支增加中文默认 4 字最小长度、相似度阈值、候选硬上限和安全绑定参数。
6. 用 `EXPLAIN (ANALYZE, BUFFERS)` 验证典型中文查询命中预期索引，并据实决定 trigram 使用 GIN
   谓词过滤还是 GiST 距离排序。
7. 保留 SQLite deterministic fake，用于无 PostgreSQL 的业务测试。

退出条件：旧文件不会因“最近 500 条”限制被漏掉，中文文件名主召回不依赖 trigram，短查询不会触发
模糊全表扫描，应用内存与工作区总文件数不线性增长。

### 任务 4.4：原文补召回与候选内精查

1. 扩展 Chunk 词法检索支持受控全局候选补召回。
2. 在候选版本内执行第二阶段精查并按文件限制 Chunk 数量。
3. 实现 Evidence 位置和短预览投影。
4. embedding 关闭、超时、无索引和单文件索引失败时按约定降级。

退出条件：摘要遗漏但正文存在的主题可以找到，且不读取无关文件全文。

### 任务 4.5：确定性融合和服务统一

1. 实现 `TwoStageFileSearchService`。
2. 固化版本化权重、归一化、稳定并列排序和推荐原因代码。
3. 消除 N+1 查询，增加候选、Chunk、预览和超时硬上限。
4. 记录安全的结构化耗时和降级状态，不记录正文和查询扩展词全文。

退出条件：相同数据和查询得到稳定顺序，正文强命中能够超过弱摘要命中。

### 任务 4.6：Agent、API 和用户投影

1. 把 `hybrid-search` handler 接入统一检索服务。
2. 补齐 L1 会话上下文，保持 Planner 声明式边界。
3. 扩展 `UserTaskReceipt` 和普通消息历史投影。
4. 补齐 `POST /api/search` 兼容接口，并复用同一权限与服务。
5. 确保 admin/ops 审计仍可查看 ToolInvocation，普通用户接口完全不返回内部载荷。

退出条件：聊天请求和搜索 API 结果一致，普通用户看不到 Skill/Tool/路径/全文。

### 任务 4.7：前端文件搜索结果卡

1. 更新 TypeScript 类型和 API 映射。
2. 实现逐文件搜索结果卡、空结果、部分降级和查看更多状态。
3. 复用现有文件详情/下载权限入口。
4. 执行前端单测和生产构建。

退出条件：用户能从聊天结果直接识别并访问文件，不需要理解内部处理过程。

### 任务 4.8：文档、回归和真实烟测

1. 更新 `README.md`、`docs/runbook.md`、`docs/api-contract.md`、数据库 schema 文档和测试文档。
2. 执行 Alembic upgrade/downgrade/upgrade 验证。
3. 执行后端全量测试和前端 build。
4. 在 Windows 与部署目标 PostgreSQL 环境完成真实文件烟测。
5. 记录资源观察值，再决定是否调整默认候选上限；不能为追求召回率无限扩大上限。

退出条件：本文件第 14 节全部验收项通过。

## 13. 自动化测试矩阵

后端至少覆盖：

1. 规范化后的整理文件名精确命中，并优先于模糊结果。
2. 中文主题词通过 Jieba/GIN 文件名主召回，不依赖 `pg_trgm` 才能命中。
3. 长文件名短语、单字遗漏或少量错字可以通过受限 `pg_trgm` 补召回。
4. 1～2 字和低信息量短查询跳过 trigram；3 字查询按配置边界执行且不能退化为无界扫描。
5. 主分类和第二分类都可召回。
6. 显式年份和“去年”可召回正确文件。
7. 实体、单位、关键词和摘要命中。
8. 摘要没有主题词、原文 Chunk 有主题词时仍可召回。
9. PDF 返回真实页码，XLS/XLSX 返回 Sheet 和单元格范围。
10. L0/L1/L4 同等相关时顺序正确。
11. 明确“这些附件”不会扩大到工作区。
12. 全局查询不会因会话里存在旧附件而错误限制为 L1。
13. 不能召回其他用户、回收站、隐藏临时文件、旧版本或失败索引。
14. 重命名后新名称立即可搜、旧名称不再命中，Chunk ID 不变。
15. 内容新版本只召回当前版本。
16. projection backfill 和重复 upsert 幂等；模拟漏事件后 reconciliation 可修复缺失或陈旧投影。
17. 候选显示字段来自事实表的一次批量 JOIN，投影不保存完整分类/实体/摘要 JSON。
18. Chunk 超时、索引缺失和 embedding disabled 可正确降级。
19. SQL/tsquery 特殊字符只作为数据处理。
20. 候选、查询词、Chunk 和预览长度硬上限有效。
21. 查询数量固定，无逐文件 N+1。
22. PostgreSQL 集成测试对精确中文文件名、Jieba 主题词、单字错漏、两字短查询和长短语执行
    `EXPLAIN (ANALYZE, BUFFERS)`，确认使用预期索引且受保护分支不发生无界全表扫描。
23. AgentGraphState、日志和普通用户回执不含正文、任何 `*_search_text`、embedding、路径、Skill 或 Tool。
24. 普通消息入口和 `/api/search` 使用相同所有权边界和排序结果。
25. SQLite deterministic 测试和 PostgreSQL 集成测试都不调用互联网、LLM 或 embedding。
26. Windows 路径、事件循环和测试临时目录不参与搜索逻辑，测试可跨平台完成。

LLM 和 embedding 相关测试必须使用 deterministic fake，并额外断言阶段四 CPU 词法默认配置下
fake provider 也没有被调用。

## 14. 手工烟测与阶段退出条件

准备同一用户的以下文件：

- 一份整理后文件名含“国家励志奖学金”的 PDF。
- 一份摘要含“家庭经济困难认定”的 DOCX。
- 一份摘要不含“公示期限”、正文某页包含该词的 PDF。
- 一份多个 Sheet、某个单元格包含“资助金额”的 XLSX。
- 另一个用户拥有一份主题相同的文件。

通过聊天依次测试：

```text
找我去年的奖学金材料。
找国家励志奖学金申请材料。
找国家励志奖学申报材料。（验证少量错字或漏字的受限补召回）
找资助材料。（验证短查询不触发无界 trigram 扫描）
找家庭经济困难认定相关文件。
哪个文件提到了公示期限？
找包含资助金额的表格。
刚才这些文件里找学生工作处的通知。
```

再通过对话确认一次活动工作副本改名，确认后立即分别用新、旧名称搜索；新名称必须可搜，旧名称不得
继续从陈旧投影命中，且该文件的 Chunk/Evidence ID 不变。

阶段四完成必须同时满足：

- 每条查询返回正确的活动工作副本，逐文件显示整理后名称和推荐原因。
- 摘要遗漏但原文存在的内容可以命中，并显示真实页码或 Sheet/单元格。
- 同等相关时 L0、L1 优先，但 L4 全局请求不会漏掉更相关文件。
- 另一个用户的同主题文件永远不出现。
- 原件和工作副本在搜索前后字节、路径和版本不变。
- 普通用户页面和接口不出现 Skill、Tool、Chunk、内部路径或分数。
- embedding、Graph 和外部模型全部关闭时搜索完整可用，服务器不需要 GPU。
- 中文文件名主要由规范化精确匹配和 Jieba/GIN 召回；`pg_trgm` 只在满足长度、阈值和候选上限时
  补召回，短查询不会造成模糊全表扫描。
- 投影只保存检索必需词项和稳定 ID，候选显示信息从事实表一次批量补齐；改名、回收站和恢复结果
  立即反映到搜索，漏事件可由 reconciliation 修复。
- 查询超时或个别索引缺失时返回部分结果或明确失败，不能伪造“没有找到”。
- `cd apps/api && pytest -v` 全部通过。
- `cd apps/web && npm run build` 成功。
- Alembic 保持单一 head，migration upgrade/downgrade/upgrade 通过。

## 15. 建议提交拆分

按可验证单元提交，避免把迁移、Agent 和前端混在一个提交：

```text
docs: define low-resource stage-four retrieval plan
feat: add rebuildable document search profiles
feat: add controlled file search scope and query parsing
feat: implement indexed document candidate retrieval
feat: add bounded chunk fallback and deterministic fusion
feat: expose safe conversational file search receipts
feat: add chat file search result cards
test: cover low-resource two-stage retrieval end to end
docs: document stage-four deployment and smoke testing
```

每次提交前运行对应局部测试；最终提交前运行后端全量 pytest、前端 build 和 PostgreSQL migration 验证。

## 16. 明确延后到阶段五或以后

- 基于 LLM 的事实回答和正式引用持久化。
- embedding/reranker/GPU 推理和向量召回上线。
- 默认外部模型或互联网检索。
- Neo4j/GraphRAG 参与本阶段主检索排序。
- 自动学习查询改写、用户隐式长期记忆和行为画像。
- 根据搜索结果自动移动、重命名、覆盖或删除文件。
- 让普通用户选择检索引擎、Skill、Tool 或模型。

未来启用 GPU 或向量 provider 时，只能作为候选版本内的可选并行信号，必须保留当前 CPU 词法路径、
稳定 Chunk/Evidence ID、权限校验和无模型降级能力。
