# Neo4j 图谱增强文件分类整体方案

## 1. 文档信息

- 状态：方案已确认，分阶段实施。
- 目标：在不替换现有 taxonomy、全文解析、分类建议持久化和人工确认机制的前提下，引入 Neo4j 图谱增强分类候选召回、排序、解释和反馈复用。
- 技术选择：Neo4j、Neo4j Python Driver、`neo4j-graphrag-python`。
- 参考项目：
  - [neo4j-labs/llm-graph-builder](https://github.com/neo4j-labs/llm-graph-builder)
  - [angelosalatino/cso-classifier](https://github.com/angelosalatino/cso-classifier)
  - [neo4j/neo4j-graphrag-python](https://github.com/neo4j/neo4j-graphrag-python)
- 第一版本实施文档：`docs/neo4j-graph-classification-v1-implementation-plan.md`。
- 第二版本实施文档：`docs/neo4j-graph-classification-v2-implementation-plan.md`。
- 受管文件全局多标签分类后续计划：`docs/managed-file-global-multi-label-classification-plan.md`。

## 2. 当前基础

项目已经具备以下可复用能力：

- `document_pages.text_content` 保存解析后的完整正文。
- `DocumentClassificationService` 作为全文分类统一入口。
- taxonomy v2 提供稳定 `category_id`、别名、正负信号和版本。
- `recall_category_candidates()` 支持基于标题、文件名、正文和信号词的确定性候选召回。
- `PATH_AS_CATEGORY` 可以把经过 Profile 审核的受管目录路径作为全局动态分类候选；文件位于该目录只提供弱位置信号，不代表已确认分类。
- `document_classification_runs`、`document_category_suggestions` 和 `document_category_feedback` 保存分类运行、建议和反馈。
- LLM 分类只能在候选集合中裁决；自由路径需要显式开启并进入 `NEEDS_REVIEW`。
- 分类结果必须带正文页码、Sheet、原文片段或人工确认来源等证据。

当前不足：

- taxonomy 和受管目录分类主要按文本信号独立匹配，缺少分类节点之间的结构关系参与排序。
- 已确认的相似文件及其分类尚未成为可复用候选信号。
- 目录层级、文件版本、派生关系和分类反馈分散在关系表中，跨关系查询和解释能力有限。
- 当前规则召回可以说明命中了什么词，但不能完整解释“哪些相似文件和哪条分类路径支持该建议”。

## 3. 研究结论

### 3.1 `llm-graph-builder` 可借鉴内容

借鉴其分层构图和可追踪处理方式：

```text
Document
-> DocumentVersion / Chunk
-> Entity
-> Relationship
-> Embedding / Similarity
```

重点借鉴：

- 文档、分块、实体和关系分层建模。
- 分块记录来源、顺序、页码和位置。
- 构图、实体解析、去重和向量索引分离。
- 构图过程记录状态、模型、耗时、错误和可重建信息。

不直接采用其完整应用：

- 它自带上传、解析、前后端和问答链路，与 File Agent 已有模块重复。
- File Agent 必须使用 `document_id`、`document_version_id`、`extraction_run_id` 和稳定 `chunk_id`，不得用文件名充当身份。
- File Agent 已经完成原件保护、解析和 OCR，图谱层不得再次直接读取原始文件。

### 3.2 `cso-classifier` 可借鉴内容

借鉴其 ontology 驱动的三阶段分类模式：

```text
语法候选
-> 语义候选
-> ontology 后处理
```

映射到 File Agent：

- 语法模块：现有 taxonomy 名称、别名、正向信号、负向信号和 n-gram 召回。
- 语义模块：全文 embedding、相似已确认文件和相似分类候选召回。
- ontology 后处理：父子路径扩展、候选合并、离群候选过滤和上位分类解释。
- 输出：分别保留规则、语义、图谱和人工支持分数，不能只输出一个不可解释的总分。

`cso-classifier` 的计算机科学 ontology 和预训练模型不能直接用于学校文件分类；项目必须使用自己的 taxonomy v2 和受管目录分类结构。

### 3.3 `neo4j-graphrag-python` 的使用边界

适合使用：

- Neo4j Retriever 和向量检索适配。
- 手工 schema 驱动的受控构图。
- 后续基于图路径的证据检索和 GraphRAG 回答。
- 从项目已经抽取的文本或结构化数据构图。

第一阶段不直接依赖：

- 自动 schema 生成。
- `SimpleKGPipeline(from_file=True)` 重新读取文件。
- LLM 自由抽取任意节点和关系。
- 普通用户输入直接转换为 Cypher。

KG Builder 的实验接口必须封装在项目 Adapter 后面，不能让 `DocumentClassificationService` 或 Agent Graph 直接依赖实验 API。

## 4. 总体定位

Neo4j 是可重建的分类与文件关系投影，不是业务事实唯一来源。

```text
Persistent Stores
├─ PostgreSQL：文件、版本、正文、taxonomy 版本、建议、反馈、ChangeSet
├─ 对象存储：原件和派生件
└─ Neo4j：分类层级、目录映射、可信分类、文件关系和检索索引
```

事实源规则：

- taxonomy v2 配置继续作为正式分类目录的 source of truth。
- `PATH_AS_CATEGORY` 受管目录配置和扫描结果继续保存在 PostgreSQL。
- 分类运行、建议、反馈和正式分类关系继续以 PostgreSQL 为准。
- Neo4j 数据必须能从 PostgreSQL、taxonomy 配置和受管目录扫描结果重建。
- Neo4j 写入失败不得导致文件解析、OCR、上传或基础分类失败。

## 5. 目标分类链路

```text
文件解析 / OCR / Docling
-> document_pages 完整正文和结构化证据
-> 规则候选召回
-> 语义相似候选召回
-> ontology 图谱扩展与排序
-> 候选合并、去重和负向信号过滤
-> LLM 在受控候选内裁决
-> document_category_suggestions
-> 用户确认或纠正
-> 正式分类事实
-> 异步投影到 Neo4j
```

图谱增强只负责候选与解释，不能绕过：

- taxonomy 枚举校验。
- `document_pages` 完整正文证据。
- LLM Tool 输入 schema。
- 分类建议持久化。
- 用户确认和反馈。

## 6. 图模型

### 6.1 第一层节点

| 节点 | 稳定键 | 用途 |
|---|---|---|
| `Document` | `document_id` | 文件逻辑身份 |
| `DocumentVersion` | `document_version_id` | 内容版本和哈希 |
| `Category` | `taxonomy_key + taxonomy_version + category_id` | taxonomy 分类节点 |
| `ManagedRoot` | `root_key` | 受管目录根 |
| `ManagedFolder` | `root_key + relative_path` | 动态目录分类节点 |

### 6.2 后续节点

| 节点 | 稳定键 | 用途 |
|---|---|---|
| `Chunk` | `chunk_id` | 页、Sheet 或结构块 |
| `Organization` | 规范化实体 ID | 机构关系 |
| `Person` | 规范化实体 ID | 人员关系 |
| `Topic` | 规范化主题 ID | 非正式主题概念 |
| `Project` | 规范化项目 ID | 项目关系 |

### 6.3 核心关系

```text
(Document)-[:HAS_VERSION]->(DocumentVersion)
(Category)-[:PARENT_OF]->(Category)
(ManagedRoot)-[:HAS_FOLDER]->(ManagedFolder)
(ManagedFolder)-[:CHILD_OF]->(ManagedFolder)
(ManagedFolder)-[:MAPS_TO]->(Category)
(DocumentVersion)-[:LOCATED_IN]->(ManagedFolder)
(DocumentVersion)-[:PATH_SUGGESTS]->(Category)
(DocumentVersion)-[:SUGGESTED_AS]->(Category)
(DocumentVersion)-[:CONFIRMED_AS]->(Category)
(DocumentVersion)-[:SIMILAR_TO]->(DocumentVersion)
(DocumentVersion)-[:HAS_CHUNK]->(Chunk)
(Chunk)-[:MENTIONS]->(Organization|Person|Topic|Project)
```

分类建议默认不作为可信图事实传播。若为审计需要投影 `SUGGESTED_AS`，查询时必须明确排除它作为已确认支持信号。
`PATH_AS_CATEGORY` 只决定目录能否贡献全局分类候选，`LOCATED_IN` 和 `PATH_SUGGESTS` 均不得自动提升为 `CONFIRMED_AS`。
一个 `DocumentVersion` 可以同时关联多个分类；分类关系与物理目录关系彼此独立。

### 6.4 关系溯源属性

图谱事实至少保留：

- `source_type`
- `source_id`
- `taxonomy_version`
- `classifier_version`
- `confidence`
- `created_at`
- `updated_at`
- `is_active`

正文和 OCR 全文不写入 Neo4j。证据正文仍由 `document_pages` 和 evidence 表提供，图谱只保存证据 ID、页码、Sheet、短摘要或内容哈希。

## 7. 候选召回与排序

### 7.1 候选来源

候选集合由四类信号组成：

1. `rule_candidates`：现有 taxonomy/目录名称、别名、正负信号匹配。
2. `semantic_candidates`：与全文或标题语义相近的已确认文件及分类。
3. `graph_candidates`：候选分类的父节点、子节点、受控相关节点和目录映射。
4. `confirmed_support`：历史人工确认或纠正后的同类文件支持。

### 7.2 分数结构

第一阶段必须保存分量，不能只保存总分：

```json
{
  "category_id": "category-id",
  "rule_score": 0.72,
  "semantic_score": 0.61,
  "graph_score": 0.40,
  "confirmed_support_score": 0.80,
  "negative_penalty": 0.08,
  "candidate_score": 0.65
}
```

初始排序权重可以从以下比例开始评测：

```text
rule_score                0.45
semantic_score            0.30
graph_score               0.15
confirmed_support_score   0.10
```

权重结果只能称为 `candidate_score`，不能未经校准直接作为最终 `confidence`。负向信号必须由确定性逻辑执行扣分。

### 7.3 图谱传播限制

- 默认只允许 1 到 2 跳分类路径扩展。
- `CONFIRMED_AS` 可以提供强支持。
- `SUGGESTED_AS` 不参与支持传播。
- 受管目录归属只能作为弱信号，不能单独形成最终分类。
- 上位分类可以作为 `RELATED` 或解释，不自动替代更具体分类。
- 图谱与正文冲突时，以正文可定位证据为准，并进入 `NEEDS_REVIEW`。

## 8. 代码边界

推荐目录：

```text
apps/api/app/modules/knowledge_graph/
├─ client.py
├─ schemas.py
├─ repository.py
├─ neo4j_repository.py
├─ projection_service.py
├─ classification_context.py
├─ candidate_retriever.py
├─ reranker.py
└─ health.py
```

职责：

- `Neo4jClientFactory`：根据配置创建请求级或进程安全 Driver，负责连接生命周期。
- `GraphProjectionService`：把 taxonomy、受管目录和可信分类事实幂等同步到 Neo4j。
- `GraphClassificationContext`：分类服务使用的抽象协议。
- `OntologyCandidateRetriever`：只读查询分类邻居、目录映射和已确认样本。
- `GraphClassificationReranker`：合并规则、语义、图谱和确认支持分数。
- `NoOpGraphClassificationContext`：图谱关闭或故障时无损降级。
- `FakeGraphClassificationContext`：单元测试使用，不连接 Neo4j。

运行时边界：

- Neo4j Driver、Repository 和 Retriever 属于 `AgentRuntimeContext` 或分类服务运行时依赖。
- 这些对象不得进入 `AgentGraphState`、checkpoint 或 `graph_state_json`。
- `DocumentClassificationService` 仍是 Agent 的唯一分类入口。
- Agent Planner 不直接选择 Cypher，不直接感知 Neo4j。
- LLM 不得生成 Cypher 写语句。

## 9. Tool 与任务边界

建议增加内部能力：

| 能力 | 用途 | 副作用 | 用户确认 |
|---|---|---:|---:|
| `classification-graph-sync` | 同步 taxonomy、目录和可信分类事实 | Neo4j 写入 | 否，后台/admin |
| `ontology-candidate-recall` | 查询分类候选和支持路径 | 无 | 否 |
| `graph-sync-status-read` | 查询同步状态和失败原因 | 无 | 否 |

普通分类请求仍调用现有文档分类 Tool。`ontology-candidate-recall` 优先作为 `DocumentClassificationService` 的内部能力，不要求 Planner 直接暴露给用户。

## 10. 同步与一致性

### 10.1 同步原则

- 使用稳定键和 `MERGE` 实现幂等写入。
- 每次同步记录 `projection_version` 和来源版本。
- taxonomy 更新后按版本重新投影，旧版本标记非活动，不立即物理删除。
- 文件内容 SHA-256 变化时创建新的 `DocumentVersion` 节点，旧版本保留用于审计。
- 用户确认、拒绝或修正分类后发布图谱同步事件。
- 支持按 root、taxonomy 和 document 执行重建。

### 10.2 故障处理

- Neo4j 不可用时返回空图谱上下文，继续执行现有 rule/LLM 分类。
- 图谱查询设置短超时和熔断，不能拖慢聊天请求。
- 写入失败记录结构化日志、同步状态和可重试错误码。
- 不允许在 API 请求事务中用 Neo4j 成功替代 PostgreSQL 提交。

## 11. 配置建议

```text
GRAPH_CLASSIFICATION_ENABLED=false
NEO4J_URI=bolt://127.0.0.1:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=
NEO4J_DATABASE=neo4j
NEO4J_QUERY_TIMEOUT_SECONDS=3
NEO4J_SYNC_ENABLED=false
GRAPH_CLASSIFICATION_MAX_HOPS=1
GRAPH_CLASSIFICATION_TOP_K=8
GRAPH_CLASSIFICATION_MIN_SUPPORT=1
```

密钥不得写入日志、State、ChangeSet 或文档示例的真实值中。

## 12. 分阶段路线

### 阶段 1：轻量 ontology 分类增强

- 建立配置、客户端抽象、健康检查和 no-op 降级。
- 投影 taxonomy v2、`PATH_AS_CATEGORY` 目录层级和可信分类关系。
- 在现有规则候选之后增加图谱候选扩展和重排。
- 保存分量分数和图谱解释路径。
- 不使用 LLM 自动构图，不把全文写入 Neo4j。

详细任务见第一版本实施文档。

### 阶段 2：语义相似文件增强

- 先对大型受管目录进行目录角色治理，区分部门、稳定分类、年份、批次和临时目录。
- 大型历史目录默认使用 `PATH_AS_WEAK_LABEL`，不能把路径归属直接提升为人工确认分类。
- 生成或复用文档版本 embedding。
- 使用 Neo4j vector index 或 `neo4j-graphrag-python` Retriever 召回相似已确认文件。
- 增加 `SIMILAR_TO` 和 embedding 版本治理。
- 评测语义信号对 Top-K 召回率和误分类率的影响。
- 详细任务、Shadow 模式和验收标准见 `docs/neo4j-graph-classification-v2-implementation-plan.md`。

### 阶段 3：结构化实体与关系

- 从 Docling 结构化块中抽取机构、项目、会议、政策和主题。
- 使用手工 schema 和白名单关系，禁止自动扩展生产 schema。
- 增加实体归一化、去重、来源证据和撤销机制。
- 通过异步任务构图，不阻塞 API 请求。

### 阶段 4：GraphRAG 证据回答

- 将图路径作为 `evidence-answer` 的检索来源之一。
- 组合 PostgreSQL 全文、向量检索和 Neo4j 图路径。
- 只允许参数化只读 Cypher 模板。
- 输出文件、页码、Sheet、关系路径和事实来源。

### 阶段 5：反馈和治理

- 用户确认、拒绝和修正成为版本化图事实。
- 建立分类效果评测集、错误关系撤销和全量重建机制。
- 增加 Neo4j 备份、容量、慢查询、索引和同步延迟监控。

## 13. 测试与评测

### 13.1 自动测试

- taxonomy 层级投影幂等。
- 受管目录层级投影幂等。
- 同一文档版本重复同步不产生重复节点和关系。
- `SUGGESTED_AS` 不参与可信分类支持。
- 图谱不可用时回退结果与关闭图谱时一致。
- 图谱候选不能越过 taxonomy 白名单。
- 图谱路径、证据 ID 和分量分数可以序列化并持久化。
- Agent State 中不存在 Driver、Repository 或全文。

### 13.2 离线评测

至少建立以下指标：

- Top-1 准确率。
- Top-3 / Top-5 候选召回率。
- 多标签 precision、recall、F1。
- `NEEDS_REVIEW` 比例。
- 人工确认通过率和修正率。
- 图谱增强前后的分类差异。
- P50/P95 图谱查询耗时。
- 图谱故障时的主链路成功率。

图谱增强上线条件是候选召回或人工确认通过率有可量化提升，且不能显著增加错误自动分类。

## 14. 安全和禁止事项

- 禁止 LLM 直接访问 Neo4j Driver。
- 禁止 LLM 生成或执行 Cypher 写语句。
- 禁止普通用户通过 Text2Cypher 查询任意图数据。
- 禁止把 API key、JWT、本地绝对路径和全文写入图谱。
- 禁止让图谱建议直接写入正式 `document_categories`。
- 禁止用文件名或父目录单独裁定最终分类。
- 禁止把不同 taxonomy 版本的同名节点错误合并。
- 禁止因 Neo4j 故障中断上传、解析、OCR 和基础分类。

## 15. 完成标准

整体方案完成需满足：

1. 图谱投影可重建、可审计、可关闭。
2. taxonomy、目录、分类建议和正式事实边界清晰。
3. 图谱增强不改变 `DocumentClassificationService` 的统一入口。
4. 每个增强候选能解释规则信号、相似文件或图谱路径。
5. 反馈能够形成可信分类关系，但建议不会自我强化。
6. GraphRAG 仅通过受控 Adapter 和 Retriever 使用。
7. 图谱关闭或不可用时，现有系统功能和结果结构保持兼容。
