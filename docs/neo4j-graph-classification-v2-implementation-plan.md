# Neo4j 图谱增强分类第二版本实施方案

## 1. 文档信息

- 状态：第二版本代码实现已完成；目录分类关系语义将按 `docs/managed-file-global-multi-label-classification-plan.md` 继续修正，真实 Neo4j smoke、首批向量投影和 Shadow 观察待部署环境验收。
- 版本目标：完成真实 Neo4j 环境验证，引入相似已确认文件语义召回，并通过技术 Shadow、小范围建议上线和用户反馈回放逐步验证效果。
- 前置版本：`docs/neo4j-graph-classification-v1-implementation-plan.md`。
- 整体方案：`docs/neo4j-graph-classification-overall-plan.md`。
- 后续语义修正与受管文件分类：`docs/managed-file-global-multi-label-classification-plan.md`。
- 核心组件：Neo4j、Neo4j Python Driver、`neo4j-graphrag-python`、本地 Embedding Adapter。

## 2. 第二版本目标

第一版本已经建立 taxonomy、受管目录和可信分类关系的图谱投影，以及图谱候选重排和无损降级。
第二版本解决以下问题：

1. 第一版本尚未连接真实 Neo4j 完成开启和关闭 smoke test。
2. 当前图谱只能根据已有规则候选扩展父子关系，不能根据全文语义找到相似已确认文件。
3. 图谱同步缺少正式运行记录、失败重试和按范围重建能力。
4. 当前没有人工标注基准样本，不能直接声明准确率或把图谱结果写成正式分类，需要通过上线后的明确用户反馈逐步建立评测集。
5. 跨用户共享分类支持时，需要防止泄露其他用户文件名、正文和本地路径。

### 2.1 当前受管目录实际画像

2026-07-15 对 `.env` 当前配置的受管根执行只读文件系统统计，未读取正文、未修改目录，也未触发重新扫描。
统计结果：

```text
非隐藏文件                         14,695
非隐藏目录                          2,492
最大目录深度                           12
年份型目录                            170
临时/待处理型目录                       14
```

主要一级目录文件量存在明显不均衡：

```text
人事处            11,703
教务处             1,331
临时                 534
校办                 375
后勤处               162
纪委+廉政工作         155
国际交流处            117
科技处                 93
工程硕士               65
```

主要文件类型：

```text
DOC      4,239
PDF      2,810
DOCX     2,032
XLS      2,010
XLSX     1,297
JPG      1,246
RAR        495
ZIP        134
TXT        115
PNG         96
```

实际目录包含多种不同角色：

- 业务部门，例如人事处、教务处、校办。
- 稳定业务分类，例如职称评定、考核与聘任、会议纪要。
- 年份目录，例如 2020、2023、2024、2025。
- 项目、人员、提交批次和会议活动目录。
- 临时、照片、备份性质或泛化目录。

因此第二版本不得把所有子目录直接转换为同等级 `Category`，也不能按目录文件数量直接强化分类。
当前 `.env` 仅配置受管根和重命名权限，未显式配置 `PATH_AS_CATEGORY`；在完成目录画像和分类模式确认前，
不得把当前 14,695 个文件的路径当作可信分类事实。

目标链路：

```text
document_pages 完整正文
-> 本地分块和文档级语义向量
-> neo4j-graphrag VectorCypherRetriever
-> 相似已确认文件
-> 可信分类候选
-> taxonomy 图扩展
-> 规则、语义、图谱和确认支持重排
-> Shadow 对照或正式分类建议
-> PostgreSQL 持久化和可解释回执
```

## 3. 范围

### 3.1 必须实现

- 真实 Neo4j 连接、约束、投影和故障降级 smoke test。
- `graph_projection_runs` 持久化同步运行、范围、状态和错误。
- 支持全量、taxonomy、受管根和单文件范围的幂等同步。
- 增加受管目录分类 Profile，区分部门、业务分类、年份、项目/批次和排除目录。
- 增加 `PATH_AS_WEAK_LABEL` 模式；大型历史目录默认只能提供弱标签，不能直接产生 `CONFIRMED_AS`。
- 本地 `EmbeddingService`、deterministic fake 和模型版本治理。
- 对完整正文分块计算向量，并生成一个文档级聚合向量。
- Neo4j `DocumentVersion.embedding` 向量索引。
- 使用 `neo4j-graphrag-python` 固定 `VectorCypherRetriever` 召回相似已确认文件。
- 将相似文件支持转换为受控分类候选，不自由生成分类路径。
- `off`、`shadow`、`enabled` 三种图谱分类模式。
- 无标注冷启动、分类反馈沉淀、新旧候选差异日志和离线回放评测。
- 跨用户分类支持脱敏，不向普通用户暴露来源文件信息。
- 向量生成、索引、召回和降级回归测试。

### 3.2 明确不实现

- 不实现 LLM 自由实体构图。
- 不把全文、OCR 全文或文档页内容写入 Neo4j。
- 不实现 Text2Cypher。
- 不把图谱作为 taxonomy source of truth。
- 不通过图谱直接写正式 `document_categories`。
- 不实现 GraphRAG 文件问答。
- 不实现 Person、Organization、Project、Topic 等通用实体图。
- 不自动启用外部 Embedding 服务处理文件正文。
- 不自动根据分类结果移动文件。
- 不把年份、人员姓名、日期批次和临时目录自动注册为正式 taxonomy 分类。
- 不在第一批任务中对全部 14,695 个文件一次性生成向量。

## 4. 前置验收

开始开发向量召回前，必须先完成第一版本真实环境验收：

1. 安装 `requirements-graph.txt`。
2. 配置独立 Neo4j 实例和最小权限账号。
3. 执行 taxonomy 和 `PATH_AS_CATEGORY` 目录首次投影。
4. 重复投影两次，确认节点和关系没有重复增长。
5. 开启 `GRAPH_CLASSIFICATION_ENABLED` 完成上传文件分类。
6. 停止 Neo4j 或制造查询超时，确认分类回退且 API 不返回 500。
7. 检查 Neo4j 中不存在全文、绝对路径、JWT、API key 和其他敏感字段。

前置验收未通过时，不进入向量索引开发。

除 Neo4j 验收外，必须先在可访问 PostgreSQL 的部署环境生成受管目录基线报告：

- `managed_files` 活动和缺失数量。
- 快照数量和 SHA-256 重复组。
- 已完成解析文件和 `document_pages` 覆盖率。
- DOC、XLS、PDF、图片 OCR 和压缩包的解析成功率。
- `document_category_feedback` 中真正 `CONFIRMED`、`REJECTED` 和 `CORRECTED` 的样本数量；数量为零不阻塞技术开发，但必须作为冷启动基线记录。

当前开发沙箱无法访问远程 PostgreSQL，因此上述数据库覆盖率不得在计划中假设为已满足。

## 5. 数据边界

### 5.1 事实源

```text
PostgreSQL / 配置文件
├─ Document
├─ document_pages
├─ taxonomy v2
├─ managed_roots / managed_files
├─ classification runs / suggestions / feedback
└─ graph_projection_runs

Neo4j 可重建投影
├─ Category / PARENT_OF
├─ ManagedFolder / MAPS_TO
├─ DocumentVersion / CONFIRMED_AS
└─ DocumentVersion.embedding
```

Neo4j 中的 embedding 属于可重新计算的派生索引。删除或重建 Neo4j 不得影响 PostgreSQL 文件、正文、
分类建议和用户反馈。

### 5.2 当前 DocumentVersion 兼容策略

当前 PostgreSQL 模型尚无独立 `document_versions` 表，且内容变化会创建新的 `Document`。第二版本继续采用：

```text
graph DocumentVersion.document_version_id = PostgreSQL Document.id
```

同时保存 `document_id`、`sha256`、`embedding_model`、`embedding_version` 和 `embedding_dimension`。
正式 `document_versions` 表在文件版本模型升级时另行迁移，第二版本不为向量召回提前重构全部文件模型。

### 5.3 跨用户数据保护

全局相似文件可以提供分类支持，但普通用户回执只能看到：

- 支持分类路径。
- 相似度区间。
- 支持文件数量。
- 支持来源类型，例如 `confirmed_history` 或 `managed_path`。

不得返回其他用户的：

- `document_id`。
- 文件名。
- 正文、摘要和证据原文。
- 用户 ID、会话 ID 和工作区 ID。
- 存储路径或受管目录真实路径。

调试日志同样只记录聚合数量和脱敏分类 ID。

### 5.4 受管目录角色与弱标签

建议新增版本化 Profile：

```text
rules/managed-root-classification/<root_key>.json
```

示例结构：

```json
{
  "root_key": "downloads",
  "profile_version": "managed-path-profile-v1",
  "mode": "PATH_AS_WEAK_LABEL",
  "department_depth": 1,
  "candidate_category_depths": [2, 3],
  "excluded_path_patterns": ["临时", "待处理", "新建文件夹"],
  "year_patterns": ["^(19|20)\\d{2}年?$"],
  "batch_patterns": ["提交.*", ".*材料$", ".*附件$"],
  "manual_category_paths": [],
  "manual_non_category_paths": []
}
```

目录角色：

```text
DEPARTMENT       部门或业务域
CATEGORY         可作为分类候选的稳定业务目录
YEAR             时间维度，不作为分类
COLLECTION       项目、会议、人员或提交批次
TEMPORARY        临时目录，不参与分类
UNKNOWN          待复核
```

图谱投影规则：

- 所有安全目录可以继续投影为 `ManagedFolder`，用于路径追踪。
- 只有 `CATEGORY` 才映射动态 `Category`。
- `YEAR` 应保存为目录属性或后续时间节点，不映射分类。
- `TEMPORARY` 不提供候选支持。
- `UNKNOWN` 只能进入目录 Profile 待复核清单。
- `PATH_AS_WEAK_LABEL` 文件只建立 `LOCATED_IN` 和 `PATH_SUGGESTS`，不得创建 `CONFIRMED_AS`。
- `PATH_AS_CATEGORY` 只允许经过 Profile 审核的目录进入全局分类候选集；文件位于该目录仍然只是弱位置证据。
- 只有用户明确接受或更正后的分类关系才能创建 `CONFIRMED_AS`，目录位置不得自动提升为确认分类。
- 一个文件允许关联多个不同分类分支，分类候选空间对上传文件和所有受管目录文件全局有效。

## 6. 图模型扩展

### 6.1 DocumentVersion 新增属性

```json
{
  "document_version_id": "document-uuid",
  "document_id": "document-uuid",
  "sha256": "...",
  "filename": "仅供受控服务内部使用",
  "embedding": [0.1, 0.2],
  "embedding_model": "local-model-name",
  "embedding_version": "document-semantic-v1",
  "embedding_dimension": 384,
  "embedding_updated_at": "2026-07-15T00:00:00Z"
}
```

`filename` 不得通过图谱分类结果返回普通用户。后续可以评估删除该属性，仅保留 PostgreSQL 回查。

### 6.2 向量索引

索引名称固定由配置提供，例如：

```text
document_version_embedding_v1
```

约束：

- 索引维度必须等于当前 Embedding 模型维度。
- 模型或维度变化时创建新索引，不原地混用。
- 旧索引在新索引完成重建和 smoke test 后再停用。
- 召回必须过滤 `is_active=true` 和匹配的 `embedding_version`。

## 7. Embedding Service

### 7.1 接口

推荐新增：

```text
apps/api/app/modules/embeddings/
├─ schemas.py
├─ service.py
├─ local_provider.py
├─ fake_provider.py
├─ semantic_profile.py
└─ versioning.py
```

协议：

```python
class EmbeddingProvider(Protocol):
    model_name: str
    dimension: int

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        ...
```

### 7.2 本地优先

第二版本默认只启用本地 Embedding Provider：

```text
GRAPH_EMBEDDING_PROVIDER=local
GRAPH_EMBEDDING_ENABLED=false
```

模型文件应随离线部署包预置并校验 SHA-256，不允许生产服务器启动时自动从互联网下载。
如果本地模型缺失，语义召回降级关闭，基础图谱分类继续可用。

外部 Embedding Provider 暂不默认开放。后续如果允许把文件内容发送到外部服务，必须遵守外部服务确认、
审计和数据范围规则，不能仅通过修改模型地址绕过 OperationPlan 或管理员配置。

### 7.3 完整正文处理

不直接截取固定前缀。处理方式：

```text
document_pages 完整正文
-> 按页/Sheet 和字符上限分块
-> 对每块生成本地向量
-> 对有效块向量做归一化平均
-> 再归一化为文档级向量
-> 只把文档级向量写入 Neo4j
```

要求：

- 空页和重复块不参与聚合。
- 单块失败不阻塞其他块，文件级记录成功块和失败块数量。
- 全部块失败时不写 embedding，并返回 `NEEDS_REVIEW` 或可重试状态。
- 不把分块正文写入 Agent State、Neo4j 或日志。
- 相同 SHA-256、模型和 embedding 版本可以复用计算结果。

### 7.4 按当前目录规模分批处理

针对当前 14,695 个文件，采用以下批次：

```text
批次 A：仅统计元数据、目录角色和解析覆盖率，不生成向量。
批次 B：选择 600–1,000 个分层样本生成向量并运行技术 Shadow；该批次不要求预先人工标注。
批次 C：每批最多 500 个文件增量扩展，持续检查质量和资源占用。
批次 D：技术验收通过后小范围展示 `SUGGESTED` 建议并收集反馈，形成评测集后再决定是否处理剩余可解析文件。
```

抽样规则：

- 按部门、稳定业务目录和文件格式分层。
- 大目录设置上限，不能让人事处样本占据绝大多数候选。
- 小目录至少保留必要样本，避免长尾分类完全缺失。
- 同一 SHA-256 只选择一个样本。
- `临时`、压缩包、可执行文件、数据库和未解析文件不进入首批向量任务。
- DOC/XLS 只有解析成功后才能进入语义样本；图片必须先通过 OCR 质量门槛。

## 8. 相似文件召回

### 8.1 GraphRAG Adapter

第一版本已有固定 `VectorCypherRetriever` Adapter。第二版本正式启用：

```text
query vector
-> Neo4j vector index
-> DocumentVersion
-> CONFIRMED_AS Category
-> 分类支持聚合
```

Retriever 的 traversal query 必须固定在代码中，不能接收用户生成的 Cypher。

### 8.2 召回过滤

- 排除当前 `document_version_id`。
- 排除相同 SHA-256，避免同内容重复上传放大支持。
- 只读取匹配 embedding 版本的活动节点。
- 只允许 `CONFIRMED_AS` 和明确受控的 `managed_path` 弱样本。
- 不使用普通 `SUGGESTED`。
- 默认 `top_k <= 20`。
- 默认相似度阈值由评测确定，不直接写死为最终置信度。
- 对单一目录的支持数量设置上限或对数缩放，避免人事处等大类凭文件数量压过正文信号。
- 相似文件支持必须按分类聚合并做类别平衡，不能把原始近邻数量直接作为置信度。

### 8.3 输出结构

```json
{
  "category_id": "school.hr.title-review",
  "semantic_score": 0.82,
  "support_count": 4,
  "support_source": "confirmed_history",
  "similarity_bucket": "high"
}
```

内部审计可以保存来源图节点 ID，但普通用户 API 和前端不得返回来源文件身份。

## 9. 分类重排 v2

候选分量：

```text
rule_score
semantic_score
graph_score
confirmed_support_score
negative_penalty
```

初始评测权重：

```text
rule_score                0.45
semantic_score            0.30
graph_score               0.15
confirmed_support_score   0.10
```

规则：

- 权重结果继续命名为 `candidate_score`，不能直接称为置信度。
- 负向信号由确定性代码扣分。
- 没有正文可定位证据时，语义或图谱候选不能自动变成正式分类。
- 相似文件分类与当前正文强冲突时进入 `NEEDS_REVIEW`。
- LLM 仍只能在合并后的候选集合中裁决。
- 自由分类路径规则保持不变，必须显式开启且只能待复核。

分类建议增加：

```json
{
  "candidate_scores": {
    "rule": 0.68,
    "semantic": 0.82,
    "graph": 0.35,
    "confirmed_support": 0.60,
    "negative_penalty": 0.00,
    "combined": 0.66
  },
  "semantic_evidence": {
    "support_count": 4,
    "similarity_bucket": "high",
    "source": "confirmed_history"
  }
}
```

## 10. Shadow 模式

新增配置：

```text
GRAPH_CLASSIFICATION_MODE=off
```

枚举：

- `off`：不查询图谱，保持现有分类。
- `shadow`：执行图谱和语义召回，记录差异，但用户结果仍使用基础分类。
- `enabled`：使用图谱增强结果生成候选和回执。

Shadow 记录：

- 基础 Top-1、Top-3。
- 图谱增强 Top-1、Top-3。
- 排名变化。
- 新增和移除候选。
- 每个分量分数。
- 查询耗时和降级原因。

Shadow 日志不得包含正文和其他用户文件身份。

### 10.1 无标注冷启动与反馈闭环

第二版本允许在没有历史人工标注集的情况下上线，但必须区分三个阶段：

```text
技术 Shadow
-> 小范围建议上线
-> 用户明确确认、拒绝或更正
-> 冻结反馈评测集
-> 离线回放和权重校准
-> 稳定版本晋级
```

阶段规则：

1. 技术 Shadow 只验证投影、向量、召回、耗时和故障降级，不改变用户结果，也不计算无依据的准确率。
2. 技术验收通过后，可以小范围启用图谱增强候选，但结果必须保持 `SUGGESTED` 或 `NEEDS_REVIEW`，不得自动写入正式 `document_categories`。
3. 用户明确点击或回复“正确”时，生成目标分类正样本。
4. 用户明确点击或回复“错误”时，生成当前分类负样本。
5. 用户更正分类时，同时生成原分类负样本和目标分类正样本。
6. 用户没有反馈不能视为分类正确，文件被打开、下载或继续对话也不能作为确认信号。
7. 同一文件后续反馈覆盖关系必须可追踪，不能物理删除历史反馈。

每条可用于评测的反馈至少绑定：

```text
document_id / document_version_id
sha256
category_id
feedback_action
corrected_category_id
taxonomy_version
classifier_version
embedding_model / embedding_version
candidate_scores
created_at
```

正文、OCR 全文和其他用户文件身份不得进入反馈日志。分类反馈继续以 PostgreSQL 为事实源，Neo4j 只接收经过审核后允许投影的派生关系。

反馈收集优先级：

- 规则、LLM、语义和图谱排名不一致的文件。
- 分数接近建议阈值的文件。
- 新目录、新分类和长尾分类文件。
- 用户重复纠正的分类。
- 相同或近似正文却产生不同分类的文件。

首个反馈评测集的形成规则：

- 累积约 100–200 条明确的接受、拒绝或更正反馈后再冻结第一版评测集。
- 按部门、业务分类和文件格式分层，不能让单一大目录主导评测结果。
- 相同 SHA-256 只保留一个评测样本。
- 评测集与后续权重调优集分开，避免用同一批反馈同时调参和验收。
- 样本覆盖不足时继续保持建议模式，不阻塞分类功能使用，但不得把新权重晋级为稳定默认版本。

反馈不会直接修改 ACTIVE taxonomy、线上权重或图谱可信关系。反馈只能先生成候选配置版本，经过历史反馈离线回放和人工批准后再发布，且必须支持回滚。

## 11. 图谱同步任务

### 11.1 PostgreSQL 表

新增 `graph_projection_runs`：

```text
id uuid primary key
projection_type varchar(40)
scope_type varchar(40)
scope_id varchar(255) nullable
projection_version varchar(80)
status varchar(40)
nodes_written integer
relationships_written integer
items_succeeded integer
items_failed integer
error_code varchar(100) nullable
error_message text nullable
started_at timestamptz
finished_at timestamptz nullable
created_at timestamptz
```

状态：

```text
PENDING
RUNNING
COMPLETED
PARTIAL
FAILED
```

### 11.2 同步范围

- `FULL`
- `TAXONOMY`
- `MANAGED_ROOT`
- `DOCUMENT`
- `CONFIRMED_CLASSIFICATION`

### 11.3 执行入口

优先实现受控命令和 admin/ops API：

```text
POST /api/admin/knowledge-graph/projections
GET  /api/admin/knowledge-graph/projections/{run_id}
GET  /api/admin/knowledge-graph/health
```

普通用户不得触发全量重建。启动时同步仅作为开发兜底，生产应使用显式任务。

## 12. 推荐代码改动

新增：

```text
apps/api/app/modules/embeddings/
apps/api/app/modules/knowledge_graph/vector_index.py
apps/api/app/modules/knowledge_graph/semantic_retriever.py
apps/api/app/modules/knowledge_graph/projection_repository.py
apps/api/app/modules/knowledge_graph/projection_router.py
apps/api/app/modules/knowledge_graph/projection_worker.py
apps/api/alembic/versions/<revision>_create_graph_projection_runs.py
```

修改：

```text
apps/api/app/core/config.py
apps/api/app/modules/classification/classifier_service.py
apps/api/app/modules/knowledge_graph/graphrag_adapter.py
apps/api/app/modules/knowledge_graph/reranker.py
apps/api/app/modules/knowledge_graph/neo4j_repository.py
apps/api/app/modules/agent/service.py
apps/api/app/db/models.py
apps/api/pyproject.toml
requirements-graph.txt
.env.example
deploy/.env.production.example
docs/runbook.md
agent.md
```

## 13. 实施步骤

### 任务 0：第一版本真实验收

- 安装依赖并连接真实 Neo4j。
- 执行首次投影、重复投影和断连降级 smoke test。
- 记录节点、关系和查询耗时基线。
- 生成当前受管根数据库覆盖率报告，确认快照、解析、OCR、重复哈希和人工确认样本数量。

### 任务 0.1：目录 Profile 与角色治理

- 为当前受管根创建版本化目录 Profile。
- 先输出目录角色候选报告，不直接修改 taxonomy。
- 人工确认部门、稳定业务分类和排除目录。
- 增加 `PATH_AS_WEAK_LABEL`，避免大型历史目录被当作确认分类库。
- 验证年份、人员、项目、照片和临时目录不会变成正式分类节点。

### 任务 1：同步运行持久化

- 先写失败测试，证明当前同步无正式运行记录。
- 增加迁移、ORM、Repository 和 Service。
- 支持幂等、部分失败和重试。

### 任务 2：Embedding 抽象

- 增加 Provider 协议和 deterministic fake。
- 实现完整正文分块、向量聚合和版本校验。
- 本地模型不存在时结构化降级。

### 任务 3：Neo4j 向量索引

- 固定索引名称、维度和 embedding 版本。
- 按文档范围增量写入向量。
- 支持索引重建和模型升级并行切换。

### 任务 4：GraphRAG 相似召回

- 启用固定 `VectorCypherRetriever`。
- 排除当前文件和相同 SHA-256。
- 只聚合可信分类关系。
- 输出脱敏语义支持。

### 任务 5：分类重排 v2

- 增加 `semantic_score`。
- 保留所有分量和正文证据。
- 实现 `off/shadow/enabled`。
- 多文件任务逐文件隔离失败。

### 任务 6：评测和上线

- 在没有人工标注集时先运行技术 Shadow，验证链路、耗时和故障降级。
- 小范围展示 `SUGGESTED` 分类建议，复用或补齐接受、拒绝和更正反馈入口。
- 将明确反馈按文件哈希、taxonomy 和分类器版本沉淀为可回放样本。
- 累积约 100–200 条有效反馈后，按部门、分类和格式分层冻结第一版评测集。
- 使用反馈评测集校准阈值和权重，候选配置通过离线回放和人工批准后才能晋级。
- 样本不足时允许继续建议模式，但不得宣称准确率或自动形成正式分类。

## 14. 测试方案

### 14.1 单元测试

- 全文分块覆盖首、中、尾页面。
- 聚合向量维度、归一化和空文本处理正确。
- 相同 SHA-256、模型和版本复用 embedding。
- 模型或维度变化时拒绝复用旧向量。
- 相同内容重复上传不会重复增加分类支持。
- 只读取 `CONFIRMED_AS`，忽略 `SUGGESTED`。
- 相似候选输出不含来源文件身份。
- Shadow 模式不改变用户最终结果。
- 用户未反馈不会被转换为正样本。
- 更正反馈同时形成原分类负样本和目标分类正样本。
- 相同 SHA-256 不会在反馈评测集中重复计数。

### 14.2 集成测试

- `graph_projection_runs` 状态正确推进。
- 同步中单文件失败不阻塞其他文件。
- taxonomy 和向量索引重复同步幂等。
- Neo4j 超时、断连和索引缺失时自动降级。
- 多附件只有一个文件语义召回失败时，其他文件继续分类。
- PostgreSQL 分类建议继续正常持久化。
- 接受、拒绝和更正反馈能够关联 taxonomy、分类器和 embedding 版本。
- 反馈不会直接修改 ACTIVE taxonomy、线上权重或正式分类关系。
- Agent State 不包含全文、向量、Driver 和 Retriever。

### 14.3 真实环境测试

- 从当前受管目录按部门、业务目录和格式选择 600–1,000 份首批语义处理样本。
- 无标注状态先完成技术 Shadow，不要求上线前集中人工标注。
- 小范围建议上线后，通过用户明确接受、拒绝和更正逐步积累约 100–200 条有效反馈，并冻结第一版分层评测集。
- 反馈样本不足或覆盖失衡时继续建议模式，不把候选权重晋级为稳定默认版本。
- 覆盖 DOC、DOCX、PDF、XLS/XLSX、图片 OCR 和纯文本。
- 验证 Neo4j 向量索引、Retriever 和图遍历查询。
- 记录 P50/P95 embedding 与检索耗时。
- 验证关闭 Neo4j 后基础分类结果结构兼容。

## 15. 效果指标

没有冻结反馈评测集时，第二版本只评估以下运行和反馈指标：

- 投影、向量生成和召回成功率。
- P50/P95 图谱检索耗时。
- Neo4j 故障时基础分类成功率。
- 建议展示数量、明确反馈数量和反馈覆盖率。
- 用户接受、拒绝和更正数量。
- 规则、LLM、语义和图谱候选分歧率。

冻结第一版反馈评测集后，再评估以下质量指标：

- Top-1 准确率。
- Top-3、Top-5 候选召回率。
- 多标签 precision、recall、F1。
- `NEEDS_REVIEW` 比例。
- 用户确认通过率和修正率。
- 图谱新增正确候选数量。
- 图谱错误提升候选数量。
- P50/P95 图谱检索耗时。
- 图谱故障时基础分类成功率。

无标注冷启动的小范围建议上线门槛：

- 真实 Neo4j 开启、关闭和故障 smoke test 通过。
- Shadow 不改变基础分类结果，且链路没有新增 500 错误。
- 用户可见结果始终为 `SUGGESTED` 或 `NEEDS_REVIEW`。
- 分类反馈可以持久化、追踪版本并撤销或更正。
- 图谱不可用时基础分类成功率保持不变。
- P95 图谱查询耗时不超过配置预算。

反馈评测集形成后的稳定版本晋级门槛：

- Top-3 召回率相对基础分类有稳定提升。
- Top-1 准确率不得明显下降。
- 错误自动分类不得增加。
- 图谱不可用时基础分类成功率保持不变。
- P95 图谱查询耗时不超过配置预算。

具体数值应根据冻结的第一版用户反馈评测集确定，不能在无数据时伪造准确率或业务阈值。

## 16. 配置建议

```text
GRAPH_CLASSIFICATION_MODE=off
GRAPH_CLASSIFICATION_ENABLED=false
GRAPH_EMBEDDING_ENABLED=false
GRAPH_EMBEDDING_PROVIDER=local
GRAPH_EMBEDDING_MODEL_PATH=/models/<model-directory>
GRAPH_EMBEDDING_MODEL_NAME=<model-name>
GRAPH_EMBEDDING_VERSION=document-semantic-v1
GRAPH_EMBEDDING_DIMENSION=384
GRAPH_VECTOR_INDEX_NAME=document_version_embedding_v1
GRAPH_VECTOR_TOP_K=12
GRAPH_VECTOR_MIN_SCORE=0.0
GRAPH_PROJECTION_WORKER_ENABLED=false
GRAPH_FEEDBACK_COLLECTION_ENABLED=true
GRAPH_CLASSIFICATION_ROLLOUT_PERCENT=10
GRAPH_FEEDBACK_EVAL_MIN_SAMPLES=100
MANAGED_PATH_CLASSIFICATION_PROFILE_DIR=./rules/managed-root-classification
MANAGED_PATH_DEFAULT_MODE=NONE
MANAGED_PATH_VECTOR_PILOT_LIMIT=1000
GRAPH_PROJECTION_BATCH_SIZE=500
```

`GRAPH_VECTOR_MIN_SCORE` 初始为评测占位，不在开发前擅自设定业务阈值。首次小范围上线只用于生成建议；稳定默认值根据冻结的用户反馈评测集更新。

## 17. 日志和审计

新增事件：

```text
graph.embedding.started
graph.embedding.completed
graph.embedding.failed
graph.vector_index.checked
graph.semantic_retrieval.completed
graph.semantic_retrieval.degraded
graph.projection_run.created
graph.projection_run.completed
classification.graph_shadow.compared
```

日志可以记录：

- 文档 ID。
- SHA-256 的短前缀或内部哈希。
- 模型名、版本和维度。
- 块数量、成功和失败数量。
- 候选数量、分类 ID 和耗时。

日志不得记录：

- 文档全文和分块正文。
- 向量完整数值。
- 其他用户文件名和 document_id。
- Neo4j 密码和连接凭据。

## 18. 发布和回滚

发布顺序：

```text
1. 发布同步运行和 Embedding 代码，全部开关保持关闭。
2. 完成真实 Neo4j 第一版本 smoke test。
3. 创建新向量索引并生成测试样本向量。
4. 开启 GRAPH_CLASSIFICATION_MODE=shadow。
5. 收集新旧分类差异，完成技术链路和故障降级验收。
6. 在测试环境开启 enabled，确认所有结果仍为分类建议。
7. 按 GRAPH_CLASSIFICATION_ROLLOUT_PERCENT 小范围启用生产建议并收集明确用户反馈。
8. 累积有效反馈后冻结第一版评测集，离线回放并校准阈值和权重。
9. 候选配置经人工批准后晋级稳定默认版本，再分批扩大范围。
```

快速回滚：

```text
GRAPH_CLASSIFICATION_MODE=off
GRAPH_CLASSIFICATION_ENABLED=false
GRAPH_EMBEDDING_ENABLED=false
GRAPH_PROJECTION_WORKER_ENABLED=false
```

关闭后保留 PostgreSQL 事实和 Neo4j 投影，现有规则/LLM 分类立即恢复。

## 19. 验收标准

必须全部满足：

1. 第一版本真实 Neo4j 开启、关闭和故障 smoke test 通过。
2. 同步运行可查询、可重试、可按范围重建。
3. 当前受管根已经有版本化目录 Profile，目录角色经过人工确认。
4. 年份、人员、批次、临时目录不会被直接投影为正式分类。
5. `PATH_AS_WEAK_LABEL` 不会生成 `CONFIRMED_AS`。
6. Embedding 使用完整正文分块聚合，不依赖短 `text_preview`。
7. 首批向量任务按类别和格式分层，不被单一大目录支配。
8. 相同内容和相同模型版本能够复用向量。
9. `VectorCypherRetriever` 只使用后端固定查询模板。
10. 只从可信分类关系或明确弱标签生成受控语义候选。
11. 跨用户支持不泄露来源文件身份或内容。
12. Shadow 模式不改变用户可见分类结果。
13. `enabled` 模式下每个分类仍有正文证据和分量分数。
14. Neo4j、Embedding 或索引故障不会导致分类主链路失败。
15. 完整后端回归测试通过。
16. 无标注状态只允许按配置比例小范围展示分类建议，且不得自动形成正式分类。
17. 用户未反馈不会被当作正样本，接受、拒绝和更正反馈均可追踪到对应版本。
18. 首个反馈评测集冻结并完成离线回放前，新权重不得晋级为稳定默认版本。

## 20. 第二版本之后

第二版本稳定后，再评估：

- Docling 结构块和 `Chunk` 图投影。
- 机构、项目、政策和主题实体关系。
- GraphRAG 证据回答。
- 图谱反馈撤销、关系治理和管理页面。
- 正式 PostgreSQL `document_versions` 模型。

未通过效果评测时，不进入自动实体构图和 GraphRAG 问答阶段。

## 21. 实施结果（2026-07-15）

已完成代码范围：

- 增加 `graph_projection_runs`、分类建议候选分量、语义证据和可追溯反馈字段及 Alembic 迁移。
- 增加受管目录 Profile、`PATH_AS_WEAK_LABEL`、目录角色识别和安全默认模式。
- 增加完整正文分块、文档级向量聚合、相同 SHA/模型版本复用和单文件失败隔离。
- 增加 Neo4j `DocumentVersion.embedding` 索引维护和分批向量投影命令。
- 增加固定查询的 `VectorCypherRetriever` 适配器，只沿 `CONFIRMED_AS` 和 `PATH_SUGGESTS` 生成脱敏分类支持。
- 增加规则、语义、图谱和确认支持的受控重排；语义独立候选保持 `NEEDS_REVIEW`。
- 增加 `off`、`shadow`、`enabled` 模式和稳定用户哈希灰度比例。
- 增加分类建议接受、拒绝和更正接口；用户未反馈不会被视为正样本。
- 增加前端分类反馈入口，不展示其他用户的来源文件身份和正文。
- 更新部署环境示例、运行手册、API 契约和项目架构规则。

已完成自动验证：

```text
后端全量测试：316 passed, 1 skipped
前端生产构建：TypeScript 与 Vite build 通过
Python compileall：通过
pyproject.toml / docker-compose.production.yml 解析：通过
Alembic：20260715_0001 为唯一 head
git diff --check：通过
```

尚需部署环境验证：

1. 安装 `requirements-graph.txt` 并连接独立 Neo4j 实例，执行开启、关闭、超时和断连 smoke test。
2. 根据 Profile 选择 600–1,000 份分层样本生成首批向量，验证索引、召回和 P50/P95 耗时。
3. 先以 `GRAPH_CLASSIFICATION_MODE=shadow` 观察差异，再按灰度比例展示建议并积累明确用户反馈。

上述三项未完成前，第二版本不能宣称已经通过真实分类效果验收，也不能把图谱建议写入正式
`document_categories`。
