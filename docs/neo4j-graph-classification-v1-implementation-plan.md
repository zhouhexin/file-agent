# Neo4j 图谱增强分类第一版本实施方案

## 1. 版本目标

- 实施状态：第一版本代码与自动测试已完成；真实 Neo4j 环境 smoke test 待部署连接配置完成后执行。
- 兼容说明：本版本中把“已分好类目录归属”视为确认分类的设计已由 `docs/managed-file-global-multi-label-classification-plan.md` 覆盖；目录只提供全局分类来源和弱位置证据。

第一版本以最小风险验证“ontology 图结构能否提升现有分类候选质量”。本版本只实现：

```text
taxonomy / 受管目录分类层级投影
-> 可信分类样本投影
-> 图谱候选扩展
-> 现有分类候选重排
-> 解释和降级
```

第一版本不建设完整文件知识图谱，不改变当前上传、解析、OCR、Docling、分类建议和 OperationPlan 主链路。

整体设计见 `docs/neo4j-graph-classification-overall-plan.md`。

## 2. 第一版本范围

### 2.1 必须实现

- Neo4j 配置和连接健康检查。
- `GraphClassificationContext` 抽象、Neo4j 实现、no-op 实现和 fake 实现。
- taxonomy v2 `Category/PARENT_OF` 幂等投影。
- `PATH_AS_CATEGORY` 的 `ManagedRoot/ManagedFolder/CHILD_OF` 幂等投影。
- 已确认分类和受控目录弱样本投影。
- 根据规则候选查询父节点、子节点、目录映射和已确认样本支持。
- 合并规则分数与图谱分数，输出分量分数和解释路径。
- `GRAPH_CLASSIFICATION_ENABLED` 开关和故障自动降级。
- 同步、查询、降级和分类回归测试。
- 运行手册和部署配置说明。

### 2.2 明确不实现

- 不把 `document_pages.text_content` 全文写入 Neo4j。
- 不建立 `Chunk`、实体、主题和自由关系图。
- 不使用 LLM 自动生成图 schema。
- 不使用 Text2Cypher。
- 不让 Agent Planner 直接选择 Neo4j Tool。
- 不建立 Neo4j 向量索引和 `SIMILAR_TO`。
- 不将图谱建议自动写为正式分类关系。
- 不改变现有分类反馈 API 和前端交互。
- 不强制使用 `SimpleKGPipeline`。

## 3. 业务假设

第一版本使用以下事实等级：

| 来源 | 信任等级 | 图谱用途 |
|---|---:|---|
| taxonomy v2 分类节点和父子关系 | 高 | ontology 结构 |
| 人工确认或人工修正的分类 | 高 | `CONFIRMED_AS` 支持 |
| 明确标记为已分好类的受管目录 | 中 | 目录弱样本和目录映射 |
| 当前分类建议 `SUGGESTED` | 低 | 仅审计，不参与支持传播 |
| 文件名、父目录名称 | 低 | 候选召回弱信号 |

如果当前系统还没有可稳定读取的正式确认分类，第一版本仍可完成 taxonomy 和受管目录图投影；`CONFIRMED_AS` 同步保持空实现或只同步明确人工确认的数据，不能把历史建议批量当作确认事实。

## 4. 第一版本图模型

### 4.1 约束

```cypher
CREATE CONSTRAINT category_identity IF NOT EXISTS
FOR (node:Category)
REQUIRE node.graph_key IS UNIQUE;

CREATE CONSTRAINT managed_root_identity IF NOT EXISTS
FOR (node:ManagedRoot)
REQUIRE node.root_key IS UNIQUE;

CREATE CONSTRAINT managed_folder_identity IF NOT EXISTS
FOR (node:ManagedFolder)
REQUIRE node.graph_key IS UNIQUE;

CREATE CONSTRAINT document_version_identity IF NOT EXISTS
FOR (node:DocumentVersion)
REQUIRE node.document_version_id IS UNIQUE;
```

`graph_key` 生成规则：

```text
Category:
taxonomy_key:taxonomy_version:category_id

ManagedFolder:
root_key:normalized_relative_path
```

不得只用分类显示名称或目录叶子名称作为唯一键。

### 4.2 节点属性

`Category`：

```json
{
  "graph_key": "school_file_classification:2026-06-v2:category-id",
  "category_id": "category-id",
  "taxonomy_key": "school_file_classification",
  "taxonomy_version": "2026-06-v2",
  "name": "规章制度",
  "path": ["学校", "行政综合管理", "规章制度"],
  "description": "...",
  "aliases": ["制度", "管理办法"],
  "is_active": true
}
```

`ManagedFolder`：

```json
{
  "graph_key": "managed-root:党办/科学发展观",
  "root_key": "managed-root",
  "relative_path": "党办/科学发展观",
  "name": "科学发展观",
  "depth": 2,
  "classification_mode": "PATH_AS_CATEGORY",
  "is_active": true
}
```

`DocumentVersion` 第一版本只保存：

```json
{
  "document_version_id": "uuid",
  "document_id": "uuid",
  "sha256": "...",
  "filename": "...",
  "is_active": true
}
```

不保存绝对路径、全文、OCR 文本或敏感内容。

### 4.3 关系

```text
(parent:Category)-[:PARENT_OF]->(child:Category)
(root:ManagedRoot)-[:HAS_FOLDER]->(folder:ManagedFolder)
(child:ManagedFolder)-[:CHILD_OF]->(parent:ManagedFolder)
(folder:ManagedFolder)-[:MAPS_TO]->(category:Category)
(version:DocumentVersion)-[:LOCATED_IN]->(folder:ManagedFolder)
(version:DocumentVersion)-[:CONFIRMED_AS]->(category:Category)
```

第一版本不创建 `SUGGESTED_AS`，避免建议与事实混淆。

## 5. 推荐代码结构

新增：

```text
apps/api/app/modules/knowledge_graph/
├─ __init__.py
├─ schemas.py
├─ repository.py
├─ neo4j_repository.py
├─ projection_service.py
├─ classification_context.py
├─ candidate_retriever.py
├─ reranker.py
└─ health.py
```

测试：

```text
apps/api/app/tests/test_graph_classification_context.py
apps/api/app/tests/test_graph_projection_service.py
apps/api/app/tests/test_graph_classification_reranker.py
apps/api/app/tests/test_document_classifier_graph.py
```

需要修改：

```text
apps/api/app/core/config.py
apps/api/app/modules/agent/runtime.py
apps/api/app/modules/agent/service.py
apps/api/app/modules/classification/classifier_service.py
apps/api/app/modules/classification/matcher.py
apps/api/pyproject.toml
apps/api/.env.example
docs/runbook.md
```

若当前 `AgentRuntimeContext` 已把 `DocumentClassificationService` 作为完整运行时依赖，则 Neo4j Context 优先注入分类服务，不额外放入 Graph State。

## 6. 接口契约

### 6.1 图谱上下文协议

```python
class GraphClassificationContext(Protocol):
    def expand_candidates(
        self,
        *,
        candidates: list[GraphCandidateSeed],
        document_id: str,
        document_version_id: str | None,
        limit: int,
    ) -> GraphClassificationResult:
        ...
```

输入不得包含全文。图谱查询只需要稳定分类 ID、文档版本 ID 和候选分数。

### 6.2 输出结构

```json
{
  "status": "COMPLETED",
  "candidates": [
    {
      "category_id": "category-id",
      "graph_score": 0.42,
      "confirmed_support_score": 0.60,
      "support_count": 3,
      "paths": [
        {
          "type": "CONFIRMED_NEIGHBOR",
          "category_path": ["学校", "行政综合管理", "规章制度"],
          "source_document_ids": ["document-id"]
        }
      ]
    }
  ],
  "warnings": []
}
```

图谱不可用时：

```json
{
  "status": "DEGRADED",
  "candidates": [],
  "warnings": ["GRAPH_UNAVAILABLE"]
}
```

### 6.3 分类建议扩展字段

在不破坏前端现有结构的情况下，分类建议可增加：

```json
{
  "candidate_scores": {
    "rule": 0.72,
    "graph": 0.42,
    "confirmed_support": 0.60,
    "negative_penalty": 0.00,
    "combined": 0.63
  },
  "graph_evidence": [
    {
      "type": "category_path",
      "path": ["学校", "行政综合管理", "规章制度"],
      "support_count": 3
    }
  ]
}
```

现有 `evidence_items` 继续保存正文可定位证据。`graph_evidence` 不能代替原文证据。

## 7. 分类执行顺序

`DocumentClassificationService.classify()` 第一版本调整为：

```text
1. 从 document_pages 读取完整正文。
2. 使用现有 matcher 召回规则候选。
3. 若图谱开关关闭，按现有逻辑继续。
4. 若图谱开启，把 category_id 和规则分数交给 GraphClassificationContext。
5. 查询候选的父子结构、目录映射和可信分类支持。
6. GraphClassificationReranker 合并候选并排序。
7. LLM judge 只在合并后的 Top N 候选中裁决。
8. 使用现有逻辑补充正文 evidence_items。
9. 分类建议照常持久化到 PostgreSQL。
10. 图谱不可用时记录警告并返回原有分类结果。
```

注意：现有 `_classify_with_available_taxonomy()` 当前直接返回建议结构。实施时应先把“候选召回”和“建议转换”分开，避免图谱只能处理已经丢失分量的最终建议。

## 8. 图谱排序规则

第一版本只对已经存在于 taxonomy 或受管目录分类集合中的候选排序，不自由创建分类路径。

初始公式：

```text
combined_score =
    rule_score * 0.65
  + graph_score * 0.20
  + confirmed_support_score * 0.15
  - negative_penalty
```

第一版本暂不加入语义分数，因此提高规则信号权重。具体要求：

- 完全没有正文或标题规则信号的图谱节点不能单独成为 Top-1。
- 父分类扩展只增加解释或较低权重候选。
- 子分类只有存在正文信号或可信样本支持时才进入候选。
- 图谱只能将候选扩展到 `GRAPH_CLASSIFICATION_MAX_HOPS` 范围。
- 如果图谱结果与强负向信号冲突，保留候选但降级为 `NEEDS_REVIEW`。
- `combined_score` 只用于排序，不直接替换现有 `confidence`。

## 9. 投影流程

### 9.1 taxonomy 投影

输入：`school_file_classification.json` 经 loader 校验后的 taxonomy 对象。

流程：

```text
加载 taxonomy
-> 展平节点
-> MERGE Category
-> MERGE PARENT_OF
-> 标记本版本活动节点
-> 记录 projection_version
```

不允许从 Neo4j 反向覆盖 taxonomy 配置。

### 9.2 受管目录投影

只同步 `classification_mode=PATH_AS_CATEGORY` 的根：

```text
ManagedRoot
-> 一级 ManagedFolder
-> 多级 ManagedFolder
-> CHILD_OF
-> LOCATED_IN
```

目录本身可以作为动态 category namespace，但不得与 taxonomy v2 中同名分类自动合并。需要明确映射时创建 `MAPS_TO`。

### 9.3 可信分类投影

同步优先级：

1. 用户明确确认或修正后的正式分类。
2. 受管根中的目录归属只投影为 `PATH_SUGGESTS`，不得转为 `CONFIRMED_AS`。
3. 其他建议不投影为 `CONFIRMED_AS`。

如果当前数据库还没有正式分类关系写入链路，先实现同步接口和空结果，不得把 `document_category_suggestions` 直接升级为确认事实。

## 10. 配置和依赖

### 10.1 配置

`Settings` 增加：

```text
graph_classification_enabled: bool = False
neo4j_uri: str = ""
neo4j_username: str = ""
neo4j_password: str = ""
neo4j_database: str = "neo4j"
neo4j_query_timeout_seconds: int = 3
neo4j_sync_enabled: bool = False
graph_classification_max_hops: int = 1
graph_classification_top_k: int = 8
```

默认关闭，未配置连接信息时必须构造 no-op 实现。

### 10.2 依赖

建议将图谱依赖设为 optional dependency：

```toml
[project.optional-dependencies]
graph = [
  "neo4j>=5,<7",
  "neo4j-graphrag>=1,<2",
]
```

第一版本核心查询优先使用 Neo4j Driver 和参数化 Cypher；`neo4j-graphrag-python` 通过 Adapter 预留，暂不让实验 KG Builder 成为启动必需依赖。安装基础 API 依赖但未安装 `graph` extras 时，图谱开关关闭的服务必须可以正常启动。

## 11. 实施步骤

### 任务 1：先补缺失测试

证明当前功能缺失：

- 当前 taxonomy 召回不会利用父子图关系补充候选。
- 当前分类不会利用历史确认样本支持。
- 当前图谱不可用场景没有显式降级结构。

测试必须先失败，再开始实现。

### 任务 2：配置和抽象边界

- 增加 Settings 字段和 `.env.example`。
- 新建 Graph Context Protocol、no-op 和 fake。
- 测试默认关闭、配置缺失、连接失败三种情况。

### 任务 3：Neo4j Repository

- 实现 Driver 生命周期管理。
- 创建约束和索引。
- 使用参数化 Cypher 实现 taxonomy、目录和可信关系 `MERGE`。
- 实现只读候选扩展查询。
- 所有查询设置超时并转换为项目内错误码。

### 任务 4：投影服务

- taxonomy v2 幂等投影。
- `PATH_AS_CATEGORY` 目录树幂等投影。
- 可信分类关系投影。
- 提供全量重建和按分类版本重建的服务入口。

第一版本可以提供启动后手工命令或 admin/service 调用，不要求聊天请求触发全量同步。

### 任务 5：分类上下文和重排

- 从规则候选构造 `GraphCandidateSeed`。
- 查询图谱候选支持和路径。
- 合并候选、去重、限制跳数。
- 保存分量分数和图谱解释。
- LLM judge 仍只能使用白名单候选。

### 任务 6：接入运行时

- 在每次 AgentRun 构造分类服务时注入 Graph Context。
- Driver/Repository 不进入 State。
- 图谱关闭、查询超时或 Neo4j 不可用时自动 no-op。
- 增加 `classification.graph_context_loaded` 和降级日志事件。

### 任务 7：文档和部署

- 更新 `docs/runbook.md`。
- 更新部署环境变量示例。
- 记录 Neo4j 初始化、健康检查、同步、关闭和回滚步骤。
- 不要求本地开发必须启动 Neo4j。

## 12. 参数化查询示例

分类候选邻居查询必须使用固定模板，示意如下：

```cypher
UNWIND $category_keys AS category_key
MATCH (category:Category {graph_key: category_key})
OPTIONAL MATCH (parent:Category)-[:PARENT_OF]->(category)
OPTIONAL MATCH (category)-[:PARENT_OF]->(child:Category)
OPTIONAL MATCH (version:DocumentVersion)-[:CONFIRMED_AS]->(category)
RETURN
  category.graph_key AS category_key,
  collect(DISTINCT parent.graph_key) AS parent_keys,
  collect(DISTINCT child.graph_key) AS child_keys,
  count(DISTINCT version) AS confirmed_support_count
LIMIT $limit
```

不得把用户消息、分类名或路径拼接进 Cypher 字符串。

## 13. 测试清单

### 13.1 单元测试

- graph key 对同一输入稳定，对不同 taxonomy 版本不同。
- taxonomy 树展平后父子关系正确。
- 多级受管目录生成完整 `CHILD_OF` 链。
- fake graph 返回父分类支持后，候选顺序按预期变化。
- 没有正文规则信号时，图谱节点不能直接成为高置信度结果。
- 强负向信号不会被图谱支持覆盖。
- `SUGGESTED` 数据不会生成 `CONFIRMED_AS`。
- no-op 结果不改变当前分类输出。

### 13.2 集成测试

- taxonomy 重复同步两次，节点和关系数量不增加。
- 受管目录重扫后删除目录被标记为非活动。
- Neo4j 连接失败，分类仍返回 `COMPLETED`，并带降级警告。
- Neo4j 查询超时不会让聊天请求长期等待。
- 图谱增强结果仍包含正文 `evidence_items`。
- 分类建议仍写入现有 PostgreSQL 表。
- `AgentGraphState` 序列化不包含 Neo4j 对象和全文。

### 13.3 回归测试

- rule-only 模式结果保持兼容。
- hybrid/review-only 模式仍只在候选内调用 LLM。
- 多文件分类逐文件隔离图谱失败。
- 部分文件没有图谱支持时正常返回规则结果。
- 纯聊天、文件总结、文件重命名和受管目录查询不受影响。

### 13.4 真实环境 smoke test

准备少量已知分类文件：

1. 同步 taxonomy 和一个 `PATH_AS_CATEGORY` 受管根。
2. 同步至少 3 个可信分类样本。
3. 对一个同类新文件分类。
4. 确认返回规则分数、图谱支持数和解释路径。
5. 停止 Neo4j，再次分类。
6. 确认系统降级到现有分类且无 500 错误。

## 14. 日志和可观测性

新增事件：

```text
graph.health.checked
graph.projection.started
graph.projection.completed
graph.projection.failed
classification.graph_query.completed
classification.graph_query.degraded
classification.graph_rerank.completed
```

日志字段至少包含：

- `request_id`
- `agent_run_id`
- `document_id`
- `taxonomy_key`
- `taxonomy_version`
- `status`
- `duration_ms`
- `candidate_count`
- `graph_candidate_count`
- `error_code`

不得记录 Neo4j 密码、全文、OCR 内容、完整 prompt 和本地绝对路径。

## 15. 迁移、发布和回滚

第一版本原则上不需要修改 PostgreSQL 主业务表。若需要保存同步游标，优先增加独立 `graph_projection_runs` 表，不把同步状态塞进 Agent State。

发布顺序：

```text
1. 发布代码，保持 GRAPH_CLASSIFICATION_ENABLED=false。
2. 安装 graph optional dependencies。
3. 配置并启动 Neo4j。
4. 执行健康检查和约束初始化。
5. 同步 taxonomy 和目录。
6. 在测试环境开启图谱分类。
7. 完成 smoke test 和离线评测。
8. 再按环境逐步开启。
```

回滚：

```text
GRAPH_CLASSIFICATION_ENABLED=false
NEO4J_SYNC_ENABLED=false
```

关闭后无需删除 Neo4j 数据，现有分类链路立即恢复为当前行为。图谱投影可独立清理或重建，不影响 PostgreSQL 事实。

## 16. 第一版本验收标准

必须全部满足：

1. 图谱默认关闭，现有服务无需 Neo4j 也能启动和运行。
2. taxonomy 和多级受管目录能够幂等投影。
3. 图谱候选只能来自现有 taxonomy 或动态目录分类集合。
4. 图谱增强保留规则、图谱和可信支持分量，不伪装成校准置信度。
5. 最终分类仍需要正文可定位证据。
6. `SUGGESTED` 分类不会成为可信传播来源。
7. Neo4j 故障不会造成分类 500 或阻塞其他文件。
8. 所有 Neo4j 查询使用参数化固定模板。
9. Driver、Repository 和全文不进入 `AgentGraphState`。
10. 相关单元、集成和分类回归测试通过。
11. 真实环境完成开启和关闭图谱两组 smoke test。

## 17. 第一版本之后

下一版本的具体实施依据为 `docs/neo4j-graph-classification-v2-implementation-plan.md`。

通过第一版本评测后，再决定是否进入：

- Neo4j vector index 和相似已确认文件召回。
- Docling 结构块到 `Chunk` 的投影。
- 手工 schema 驱动的机构、项目、政策和主题关系抽取。
- `neo4j-graphrag-python` Retriever 与证据回答链路。
- 图谱反馈撤销、重建和管理页面。

如果第一版本不能提升 Top-K 召回率或人工确认通过率，应保留抽象边界但关闭图谱分类，不继续扩大构图范围。

## 18. 本次实施结果

已完成：

- 图谱配置、健康状态、进程级 Driver 生命周期和 no-op/故障降级。
- 固定参数化 Cypher Repository 和查询超时限制。
- taxonomy v2、动态受管目录层级和可信分类关系投影服务。
- 规则、图谱、人工支持分量重排和图谱解释字段。
- `DocumentClassificationService` 统一入口及 AgentRun 运行时注入。
- `neo4j-graphrag-python` 固定 VectorCypher Retriever Adapter；第一版本未启用向量索引。
- 可选依赖、环境变量、Docker 构建开关、运行手册和回滚说明。
- 图谱对象和全文不进入 `AgentGraphState` 的回归保护。

自动验证结果：

```text
定向分类与 Agent 回归：134 passed
完整后端测试：301 passed, 1 skipped
```

尚待真实环境验证：

- 安装 `requirements-graph.txt`。
- 连接真实 Neo4j 并执行首次 taxonomy/受管目录投影。
- 分别在图谱开启和关闭状态完成分类 smoke test。
- 使用人工标注样本对比 Top-1、Top-K 召回和人工确认通过率。
