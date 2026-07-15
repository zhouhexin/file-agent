# File Agent Skills Catalog

本文定义 File Agent 项目级 Skill 清单。这里的 Skill 是产品内 Agent 能力单元，不是 Codex 本地开发技能。

每个 Skill 负责把用户意图转成受控 Tool 调用。Planner 选择 Skill，Skill 约束可调用 Tool，Tool 执行实际动作。

## 1. Skill 与 Tool 的关系

```text
User message
-> chat-intake
-> Planner selects Skills
-> Skill validates context and builds Tool steps
-> tool-dispatch validates whitelist and schemas
-> Tool executes
-> ChangeSet / OperationPlan / response
```

规则：

- Skill 不直接操作文件系统、数据库写接口或外部服务。
- Skill 只能调用白名单 Tool。
- 高风险 Skill 必须生成 OperationPlan。
- Skill 输出必须可审计，关键结论必须有 evidence。
- MVP 可以先用代码模块和 Markdown 规则实现 Skill，不做自动演化平台。

## 2. MVP Skills

MVP Skill 只保留业务编排边界。文件读取、表格读取、chunk、embedding、检索等底层能力不再作为独立 Skill，而作为 Tool Adapter 实现。

| Skill | 开源使用方式 | 触发条件 | 可调用 Tool | 主要输出 |
|---|---|---|---|---|
| `chat-intake` | 不直接使用开源 Skill；可经 Tool 读取自研索引 | 每次用户发送消息 | `job-status-read`, `document-lineage-read` | intent、slots、附件上下文、候选 Skill |
| `file-ingest` | 使用开源 Tool Adapter：Unstructured、Haystack、LlamaIndex、LangChain、Docling、openpyxl | 用户上传文件或要求读取/处理文件 | `document-register-upload`, `security-scan`, `document-convert`, `table-extract`, `artifact-write`, `metadata-extract`, `chunk-build`, `embedding-generate` | Document、Version、Artifact、pages/chunks、metadata、read_profile、read_quality、初始 ChangeSet |
| `document-classification` | 分类编排自研；证据召回可使用 LangChain/LlamaIndex/pgvector adapter；可参考文件整理类 Skill 的“先建议、后确认”模式 | 文档完成解析和索引；或存在 `PATH_AS_CATEGORY` 受管目录可作为动态分类来源 | `multi-label-classify`, `hybrid-search`, `document-lineage-read` | document_categories、动态目录分类建议、置信度、证据、NEEDS_REVIEW 原因 |
| `file-search` | 使用开源检索 adapter：LangChain、LlamaIndex、pgvector | 用户请求查找文件或材料 | `hybrid-search`, `document-lineage-read` | 分层检索结果、推荐理由 |
| `managed-file-query` | 不直接使用开源 Skill；自研受管目录元数据查询编排 | 用户请求列出、查看或搜索服务器受管目录文件 | `managed-file-list`, `managed-file-search`, `feedback-record` | 受管目录文件清单、元数据过滤条件、解析反馈样本 |
| `managed-file-classification` | 分类编排自研；正文解析使用现有 adapter，图谱增强可使用 Neo4j/neo4j-graphrag-python | 用户要求对受管目录范围内的文件分类或重新分类 | `classify-managed-files`, `job-status-read`, `feedback-record` | 同步逐文件多标签结果或异步 Job、分类建议、证据、ChangeSet |
| `file-rename` | 参考 tfeldmann/organize 与 F2 的规则化批处理思想；第一版使用自研 Native 执行器 | 用户要求按年份、文号和正文标题生成受管文件改名建议 | `generate-rename-suggestions`, `confirmed-file-action` | 字段证据、重命名建议、OperationPlan、确认后的 ChangeSet |
| `spreadsheet-workbench` | 使用 openpyxl、pandas 和可选 LibreOffice adapter | 用户请求表格 Profile、统计、校验、编辑、重算或格式转换 | `profile-spreadsheet`, `analyze-spreadsheet`, `validate-spreadsheet`, `operation-plan-create`；后续 `edit-spreadsheet`, `recalculate-spreadsheet` | 表结构、只读分析结果、校验报告、待确认编辑计划 |
| `evidence-answer` | 使用 LangGraph/LangChain 编排和结构化输出；业务证据规则自研 | 用户提出需要回答的问题 | `hybrid-search`, `evidence-answer` | answer、references、无依据说明 |
| `change-report` | 不直接使用开源 Skill；自研审计输出 | Tool 执行后需要回执 | `change-report` | ChangeSet 摘要、逐文件明细 |
| `operation-plan` | 不直接使用开源 Skill；自研高风险操作规划 | 用户请求改名、移动、复制、删除、导出、外发 | `operation-plan-create` | PLANNED OperationPlan |
| `confirmed-file-action` | 不直接使用开源 Skill；底层文件操作通过自研受控 Tool | 用户确认 OperationPlan | `confirmed-file-action`, `change-report` | 执行结果、ChangeSet |
| `feedback-and-memory` | 不直接使用开源 Skill；自研反馈和偏好存储 | 用户提交纠错，或明确要求记住/忘记偏好 | `feedback-record`, `operation-plan-create` | feedback、user_preferences、后续处理建议 |

被替换为 Tool Adapter 的旧底层 Skill：

```text
file-upload -> file-ingest + document-register-upload tool
document-router -> file-ingest + document-convert/document-router adapter
document-read -> file-ingest + document-convert adapter
spreadsheet-read -> file-ingest + table-extract adapter
metadata-extract -> file-ingest + metadata-extract tool
chunk-and-embed -> file-ingest + chunk-build/embedding-generate tools
multi-label-classify -> document-classification
classification-evidence-check -> document-classification
personalized-search -> file-search
feedback-learning + user-memory -> feedback-and-memory
```

### 2.1 MVP Skill Open Source Matrix

这里标注的是“该业务 Skill 是否调用了开源实现的 Tool Adapter”。业务 Skill 本身仍按本项目规则自研，不直接引入外部业务 Skill 包。

| Skill | 是否使用开源 | 使用位置 | 开源地址 |
|---|---|---|---|
| `chat-intake` | 间接 | LangGraph Agent Runtime 编排 | https://github.com/langchain-ai/langgraph |
| `file-ingest` | 是 | 文档解析、表格解析、chunk、可选安全扫描 | https://github.com/Unstructured-IO/unstructured, https://github.com/deepset-ai/haystack, https://github.com/docling-project/docling, https://github.com/run-llama/llama_index, https://github.com/langchain-ai/langchain, https://foss.heptapod.net/openpyxl/openpyxl, https://github.com/Cisco-Talos/clamav |
| `document-classification` | 间接 | 分类证据召回、相似文档检索；受管目录子目录可作为动态分类候选源 | https://github.com/langchain-ai/langchain, https://github.com/run-llama/llama_index, https://github.com/pgvector/pgvector |
| `file-search` | 是 | 混合检索、retriever/query engine adapter | https://github.com/langchain-ai/langchain, https://github.com/run-llama/llama_index, https://github.com/pgvector/pgvector |
| `managed-file-query` | 否 | 自研受管目录元数据查询和反馈样本记录 | 无 |
| `managed-file-classification` | 间接 | Docling/Office/OCR 正文解析与 Neo4j 图谱候选增强 | https://github.com/docling-project/docling, https://github.com/neo4j/neo4j, https://github.com/neo4j/neo4j-graphrag-python |
| `spreadsheet-workbench` | 是 | 表格 Profile、只读分析、校验、后续编辑和重算 | https://foss.heptapod.net/openpyxl/openpyxl, https://github.com/pandas-dev/pandas, https://www.libreoffice.org |
| `evidence-answer` | 是 | LangGraph 节点编排、LangChain 结构化输出/Tool 调用 | https://github.com/langchain-ai/langgraph, https://github.com/langchain-ai/langchain |
| `change-report` | 否 | 自研 ChangeSet 回执 | 无 |
| `operation-plan` | 否 | 自研高风险操作规划 | 无 |
| `confirmed-file-action` | 否 | 自研确认后文件操作 | 无 |
| `feedback-and-memory` | 否 | 自研反馈和偏好存储 | 无 |

## 3. Open-Source-Backed Tool Adapters

底层通用能力优先用成熟开源库实现为 Tool Adapter，而不是自定义 Skill。

| Adapter Tool | 首选开源实现 | 开源地址 | 备选/补充 | 替代的旧底层 Skill |
|---|---|---|---|---|
| `document-convert` | Unstructured partitioning | https://github.com/Unstructured-IO/unstructured | Haystack `MultiFileConverter`、Docling、LlamaIndex Readers、LangChain document loaders | `document-router`, `document-read` |
| `table-extract` | Haystack `XLSXToDocument` 或 openpyxl adapter | https://github.com/deepset-ai/haystack, https://foss.heptapod.net/openpyxl/openpyxl | Unstructured / LlamaIndex reader | `spreadsheet-read` |
| `chunk-build` | LangChain text splitters 或 LlamaIndex node parsers | https://github.com/langchain-ai/langchain, https://github.com/run-llama/llama_index | 自定义 evidence-aware chunker | `chunk-and-embed` 的 chunk 部分 |
| `embedding-generate` | OpenAI-compatible embedding client；必要时用 LangChain embedding interface 包装 | https://github.com/langchain-ai/langchain | 直接调用兼容 OpenAI 的 embedding API | `chunk-and-embed` 的 embedding 部分 |
| `hybrid-search` | LangChain retriever tool / pgvector retriever adapter | https://github.com/langchain-ai/langchain, https://github.com/pgvector/pgvector | LlamaIndex QueryEngineTool： https://github.com/run-llama/llama_index | `personalized-search` 底层检索 |
| `document-lineage-read` | 自研轻量 SQL adapter | 无 | 后续 Neo4j graph adapter： https://github.com/neo4j/neo4j | 无 |
| `evidence-answer` | LangGraph node + structured output parser | https://github.com/langchain-ai/langgraph, https://github.com/langchain-ai/langchain | LangChain tool/function calling | 无 |
| `security-scan` | 可选 ClamAV adapter | https://github.com/Cisco-Talos/clamav | MVP 可先做扩展名、MIME、大小和宏风险检查 | 无 |

可选补充库：

| 开源项目 | 地址 | 推荐用途 |
|---|---|---|
| Docling | https://github.com/docling-project/docling | PDF、Office、HTML 等文档转换备选 |
| LlamaIndex | https://github.com/run-llama/llama_index | Readers、node parser、QueryEngineTool 备选 |
| Haystack | https://github.com/deepset-ai/haystack | 文件 converter、检索 pipeline 备选 |
| LangGraph | https://github.com/langchain-ai/langgraph | MVP Agent Runtime 状态图主线 |

采用策略：

- Open-source adapter 可以替换底层 Skill，但不能替换业务 Skill。
- Adapter 输出必须归一化为项目内部 schema：pages、tables、chunks、evidence、artifacts。
- Adapter 不能绕过 StorageService、ChangeSet、OperationPlan 和权限检查。
- Adapter 失败时必须返回结构化错误，不能吞掉文件或证据。

## 4. Deferred Skills

以下 Skill 不进入 MVP 完整实现，但目录和命名可以预留：

| Skill | 阶段 | 说明 |
|---|---|---|
| `ocr-process` | Phase 2 | 扫描 PDF 和图片 OCR、质量评估 |
| `preview-generate` | Phase 2 | Office/PDF 预览和缩略图 |
| `lineage-build` | Phase 3 | 附件、相似文件、支撑材料关系 |
| `graph-search` | Phase 3 | Neo4j 图遍历检索 |
| `skill-evolution` | Phase 4 | 生成候选 Skill patch |
| `skill-evaluation` | Phase 4 | 结构、安全、回归、灰度、回滚 |

## 5. External Skill And Tooling References

已检索过的开源方向里，暂时没有可以直接替换本项目业务 Skill 的成熟文件智能体 Skill 包。可复用内容主要分三类：

| 来源 | 地址 | 类型 | 可复用点 | 结论 |
|---|---|---|---|---|
| `langchain-ai/langchain-skills` | https://github.com/langchain-ai/langchain-skills | Agent Skill 集合 | `SKILL.md` 组织方式、LangGraph/LangChain 开发经验 | 适合作为开发规范参考，不直接替换业务 Skill |
| `Lubu-Labs/langchain-agent-skills` | https://github.com/Lubu-Labs/langchain-agent-skills | LangChain/LangGraph 编码助手 Skill | 项目初始化、LangGraph 开发、监控、调试 | 面向编码代理，不是运行时文件业务 Skill |
| `SpillwaveSolutions/mastering-langgraph-agent-skill` | https://github.com/SpillwaveSolutions/mastering-langgraph-agent-skill | LangGraph 学习/开发 Skill | LangGraph 状态图、Tool、HITL、部署指导 | 可作为 LangGraph 实现参考，不替换业务 Skill |
| LangChain / LangGraph Tools | https://github.com/langchain-ai/langchain, https://github.com/langchain-ai/langgraph | Tool 框架 | Tool schema、retriever tool、graph orchestration | 适合实现 `ToolRegistry` 和 `tool-dispatch` |
| LlamaIndex Readers / Tools | https://github.com/run-llama/llama_index | 数据连接器和查询工具 | PDF、文档 reader、query engine tool、多文档 agent 思路 | 可替换或补充部分 Tool，不替换 Skill |
| Unstructured | https://github.com/Unstructured-IO/unstructured | 文档解析组件 | 文件 partition、元素化解析 | 可用于 `document-convert` 的底层实现 |
| Semantic Kernel Plugins | https://github.com/microsoft/semantic-kernel | Plugin/Skill 概念 | Plugin 分组、函数暴露、自动调用 | 概念可参考；Python/FastAPI/LangGraph 主线下不优先采用 |

采纳策略：

- 不把开源“coding-agent skill”直接放进产品运行时 Skill。
- 采用 Agent Skill 标准的目录形态：`skills/<skill>/SKILL.md`。
- 采用 LangChain/LangGraph 的 Tool schema 和 tool-dispatch 思路。
- 文档解析、检索、query tool 等底层能力可以按需引入 LlamaIndex、LangChain 或 Unstructured 作为 Tool 实现。
- 本项目的 Skill 仍以学工文件业务流程为边界，因为分类、证据、ChangeSet、OperationPlan、原件保护都具有强业务约束。

## 6. Required SKILL.md Template

每个 `skills/<skill-name>/SKILL.md` 必须包含：

```text
# <skill-name>

## Trigger

## Inputs

## Outputs

## Allowed Tools

## Open Source Backing

## Steps

## Evidence Rules

## ChangeSet Rules

## OperationPlan Rules

## Failure Handling

## Tests

## Forbidden
```

## 7. MVP Skill Files To Create

```text
skills/chat-intake/SKILL.md
skills/file-ingest/SKILL.md
skills/document-classification/SKILL.md
skills/file-search/SKILL.md
skills/managed-file-query/SKILL.md
skills/managed-file-classification/SKILL.md
skills/spreadsheet-workbench/SKILL.md
skills/evidence-answer/SKILL.md
skills/change-report/SKILL.md
skills/operation-plan/SKILL.md
skills/confirmed-file-action/SKILL.md
skills/feedback-and-memory/SKILL.md
```

MVP 实施时至少要创建这些文件的规则骨架；自动评测、灰度和发布记录可以后续实现。
