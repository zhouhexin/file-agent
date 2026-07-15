# Docling 与 Neo4j 图谱增强分类计划

> 本文保留早期 Docling 与 Neo4j 组合设想。经仓库研究和现有代码对齐后，统一实施依据已拆分为：
>
> - `docs/neo4j-graph-classification-overall-plan.md`：整体架构、数据边界和分阶段路线。
> - `docs/neo4j-graph-classification-v1-implementation-plan.md`：轻量第一版本任务、测试、发布和回滚方案。
>
> 如本文与上述两份文档冲突，以上述两份新文档为准。

## 1. 当前状态

- 状态：暂缓实施。
- 优先级：后续增强阶段，不纳入当前 Docling 第一阶段。
- 实施前提：统一文档解析接口、Docling 试点、结构化证据和基础分类链路已经稳定。

本计划用于记录通过 Docling 结构化解析结果和 Neo4j 文件知识图谱增强分类候选召回、关系推理和分类排序的后续方案。Neo4j 不替代 taxonomy、正文证据或人工确认。

## 2. 目标架构

```text
Docling / 现有解析器
-> document_pages + 结构化 Artifact
-> 标题、正文、表格、实体、机构、年份和主题提取
-> taxonomy 候选召回
-> Neo4j 关联分类、相似文件和事实关系查询
-> 规则 / LLM 受控裁决
-> 分类建议 + 可定位证据
-> 用户确认
-> 正式分类关系和图谱事实更新
```

知识图谱主要提供以下辅助信号：

- 同一机构、项目、会议、政策和业务主题下的已确认分类。
- 文件引用、附件、派生版本、模板和相似文件关系。
- 分类目录的上下位关系、别名和受管目录映射。
- 用户确认或纠正后的稳定分类事实。

## 3. 图谱数据边界

建议节点：

- `Document`
- `DocumentVersion`
- `Category`
- `Organization`
- `Person`
- `Topic`
- `Project`
- `ManagedPath`

建议关系：

```text
(Document)-[:HAS_VERSION]->(DocumentVersion)
(Document)-[:MENTIONS]->(Organization|Person|Topic|Project)
(Document)-[:RELATED_TO]->(Document)
(Document)-[:DERIVED_FROM]->(Document)
(Document)-[:SUGGESTED_AS]->(Category)
(Document)-[:CONFIRMED_AS]->(Category)
(Category)-[:PARENT_OF]->(Category)
(ManagedPath)-[:MAPS_TO]->(Category)
```

边界要求：

- 正文、OCR、表格内容和可定位证据仍保存在现有 Persistent Stores 中，不把全文复制到图谱。
- Neo4j client 和图谱查询服务属于 `AgentRuntimeContext`，不得进入 `AgentGraphState`。
- 图谱事实属于 `Persistent Stores`，必须记录来源、版本、置信度和更新时间。
- Agent 和 LLM 不得直接执行 Cypher 写操作，图谱写入必须经过白名单 Tool 或后台同步服务。

## 4. 分类约束

- taxonomy 仍是正式分类目录的 source of truth。
- 正文、表格和 OCR 内容仍是主要分类证据。
- 文件名、父目录和 `ManagedPath` 只能作为弱证据。
- `CONFIRMED_AS` 可以参与候选增强和排序。
- `SUGGESTED_AS` 不得用于自动强化其他文件，避免错误循环传播。
- 图谱召回结果不能直接写入正式 `document_categories`。
- 低置信度或图谱与正文冲突的结果必须进入 `NEEDS_REVIEW`。
- 每个分类建议必须保留页码、段落、Sheet 或人工确认来源等可追溯证据。

## 5. 推荐代码边界

```text
apps/api/app/modules/knowledge_graph/
├─ schemas.py
├─ repository.py
├─ neo4j_repository.py
├─ service.py
└─ classification_context.py
```

`DocumentClassificationService` 保持分类总入口，通过抽象接口获取图谱上下文：

```text
taxonomy_candidates = taxonomy_recaller.recall(...)
graph_context = graph_classification_context.load(...)
categories = classification_judge.judge(
    taxonomy_candidates,
    graph_context,
    document_evidence,
)
```

分类服务不得直接依赖 Neo4j SDK，以便测试时使用 deterministic fake，并为图谱关闭、故障降级或替换实现保留边界。

## 6. 后续实施阶段

### 阶段 A：解析与证据底座

- 建立统一 `DocumentParser`、`ParseRequest` 和 `ParseResult`。
- 接入 `LocalDoclingParser`，保留现有解析器和 OCR 回退。
- 稳定输出正文、Markdown、结构化 Artifact、实体和可定位证据。
- 将解析器名称、版本和配置版本纳入结果复用判断。

### 阶段 B：图谱契约与同步

- 定义节点、关系、唯一键、来源和版本契约。
- 实现 `GraphClassificationContext` 抽象及 no-op/fake 实现。
- 建立 PostgreSQL 到 Neo4j 的幂等后台同步。
- 增加同步失败重试、审计日志和数据重建能力。

### 阶段 C：只读图谱增强分类

- 根据文档实体、主题和候选分类查询关联事实。
- 将图谱结果作为 taxonomy 候选的附加排序信号。
- 图谱不可用时自动降级为现有正文分类，不影响主链路。
- 对比仅正文分类和正文加图谱分类的效果。

### 阶段 D：反馈闭环

- 将人工确认分类同步为 `CONFIRMED_AS`。
- 将文件关系、实体关系和分类反馈纳入版本化事实。
- 对图谱传播深度、关系类型和置信度设置硬限制。
- 建立错误关系撤销、重建和审计流程。

## 7. 启动条件

满足以下条件后再启动本计划：

1. Docling 或统一解析接口已稳定运行，并具备可重复的质量评测样本。
2. 分类建议、证据、用户反馈和 taxonomy 版本已经持久化。
3. 已有足够数量的人工确认分类，可形成可信图谱信号。
4. 当前规则/LLM 分类存在可量化、且图谱有望改善的问题。
5. 团队可以承担 Neo4j 部署、备份、监控、同步和数据治理成本。

## 8. 验收标准

- 图谱关闭或故障时，现有解析和分类链路正常运行。
- 图谱只能增强候选召回和排序，不绕过 taxonomy 和证据校验。
- `SUGGESTED_AS` 不会作为可信事实传播。
- 图谱增强后，分类 Top-K 召回率或人工确认通过率有可量化提升。
- 每个图谱增强分类均能解释所使用的关系、来源和正文证据。
- 图谱写入、重试、撤销和重建均有审计记录。
