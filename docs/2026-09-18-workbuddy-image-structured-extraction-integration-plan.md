# WorkBuddy 图片结构化提取回传与查询接入方案

> 2026-09-20 新需求：输入范围扩展为图片、Word 和 PDF，内容提取（含 OCR 和原生正文/表格读取）
> 全部由 WorkBuddy 完成，File Agent 接收校验并生成 Excel，继续采用增量复用的小 Demo 路线。
> 最新实施设计见[文档内容抽取到 Excel 扩展方案](2026-09-20-workbuddy-document-to-excel-demo-extension-plan.md)；
> 本文的完整 OCR v2 等后续设计不作为此次扩展的前置条件。

> 2026-09-18 范围修订：当前先实现[共享累计表格 Demo](2026-09-18-workbuddy-structured-dataset-demo.md)。
> 在已有功能基础上增量接入并尽量复用；所有登录用户共同操作共享工作副本及 Demo 数据集，不增加
> 用户授权管理或 owner 限制。以下章节作为后续完整设计参考，不作为 Demo 全部前置条件。

## 1. 目标

用户在 WorkBuddy 上传图片并明确要求提取图片中的文字或表格后：

1. File Agent 接收并保存原始图片，创建不可变 `DocumentVersion`。
2. WorkBuddy 领取受控提取任务，调用宿主已有识图能力。
3. WorkBuddy 把 OCR 正文、版面块、表格字段和逐字段证据回传 File Agent。
4. WorkBuddy 只回传本张图片的结构化 JSON 与证据，不维护累计 Excel。
5. File Agent 校验结果后，把本次记录原子追加到用户明确选择的结构化数据集。
6. File Agent 根据数据集当前 revision 生成或刷新版本化 XLSX 派生件，原图和旧导出均不被原地覆盖。
7. 后续 WorkBuddy 调用 File Agent 的数据集查询 Tool；File Agent 返回累计记录、字段结果和对应原图证据。

本功能不允许 WorkBuddy 直接写数据库、直接写 File Agent 存储目录或提交 SQL。WorkBuddy 返回的识别文字、
字段名和值都只能作为不可信数据处理，不能成为系统指令。

## 2. 当前能力与复用结论

| 现有能力 | 当前状态 | 复用方式 |
| --- | --- | --- |
| `workbuddy_attachment_ingest` | 已支持 WorkBuddy 附件缓存引用、冻结清单、上传和自动整理 | 直接复用为图片入口，不新增任意路径读取能力 |
| `ExternalExtractionTask/Page` | 已支持图片/PDF 外部 OCR 任务、租约、固定页范围、源哈希和版本绑定 | 扩展为版本化的 OCR + 结构化结果任务 |
| `extraction_claim/renew/submit` MCP | 已支持 WorkBuddy 领取图片页并回传 OCR 正文与 blocks | 保留 v1 OCR 契约；新增 `extraction_claim_v2`、`extraction_submit_v2`，内部复用原租约状态机 |
| `DocumentExtractionRun/Page/Element` | 已保存页文本、坐标和版面元素 | 继续作为原图证据与全文索引来源 |
| `StructuredExtractionRun/Field` | 已支持动态字段 schema、多记录、置信度、状态、页码和 bbox | 直接作为 WorkBuddy 结构化结果的正式事实模型 |
| `StructuredExtractionExportService` | 已支持 File Agent 本地幂等生成 CSV/XLSX | 抽取公共安全写入逻辑，并扩展为按数据集 revision 生成累计 Excel |
| chunk/evidence/index | 已支持页面和表格证据索引 | OCR 正文继续进入现有索引；结构化字段增加可查询投影 |
| `file_search`、`evidence_answer` | 已支持文件定位和基于原文的普通问答 | 继续用于文件发现和非确定性说明性问答 |
| 结构化数据精确查询 MCP | 当前缺失 | 新增白名单查询 API 与 MCP Tool |

结论：现有能力可覆盖约三分之二的基础设施。无需重做上传、原图保护、OCR 租约、单图结构化字段表和
安全 XLSX 写入；需要增加“外部结构化结果适配层”“累计数据集与并发追加层”“数据集 Excel 导出层”和
“结构化数据查询层”。

## 3. 推荐目标链路

```text
WorkBuddy 图片附件提交
-> workbuddy_attachment_ingest 固定附件清单
-> File Agent 保存原图与 DocumentVersion
-> ExternalExtractionTask(profile=STRUCTURED_TABLE) 等待领取
-> extraction_claim_v2 返回固定页面、source_sha256、source_version_id、requested_outputs
-> WorkBuddy 调用宿主识图能力
-> extraction_submit_v2 回传 OCR 页面 + 本图结构化记录 + 证据引用
-> File Agent 校验并写 DocumentPage / DocumentElement
-> 写 StructuredExtractionRun / StructuredExtractionField
-> 根据显式 dataset_id 原子追加本次记录并推进 dataset revision
-> File Agent 按新 revision 生成累计 XLSX 数据集导出
-> 生成 ChangeSet / ChangeItem
-> 继续分类、命名、落位和索引
-> WorkBuddy 使用 file_search 定位文件
-> structured_dataset_query 执行确定性字段查询
-> File Agent 返回记录、字段、页码、bbox 和原图引用
```

## 4. 外部提取契约改造

### 4.1 任务 Profile

`ExternalExtractionTask` 增加或在版本化任务配置中固定以下字段：

- `extraction_profile`: `OCR_TEXT` / `STRUCTURED_TABLE`
- `requested_outputs`: `ocr_text`、`layout_blocks`、`table_records`
- `schema_mode`: `EXPLICIT_FIELDS` / `AUTO_DISCOVER`
- `field_schema`: 用户明确字段或空数组
- `record_mode`: `SINGLE_RECORD` / `TABLE_ROWS` / `KEY_VALUE_GROUPS` / `AUTO`
- `presentation`: `TABLE` / `XLSX` / `CSV`
- `contract_version`: 新增 `workbuddy-structured-extraction-v1`

Profile 必须由本轮已提交的用户请求和后端 Planner 生成并冻结，WorkBuddy 不能在领取后扩大字段范围或修改
输出格式。

### 4.2 Claim v2 响应

`extraction_claim_v2` 在复用现有租约、固定页集合和来源快照的基础上返回以下字段；这些字段不加入原
`extraction_claim` 响应：

```json
{
  "requested_outputs": ["ocr_text", "layout_blocks", "table_records"],
  "schema_mode": "AUTO_DISCOVER",
  "field_schema": [],
  "record_mode": "TABLE_ROWS",
  "presentation": "XLSX"
}
```

WorkBuddy 必须逐页处理 claim 返回的真实资源。刷新或断线后继续使用后端任务状态恢复，不能从聊天文本重建
任务范围。

### 4.3 Submit v2 请求

保留现有 `worker_id`、`lease_token`、`submission_key`、`source_sha256`、`source_version_id` 和完整页集合。
新增严格结构化结果：

```json
{
  "structured_result": {
    "field_schema": [
      {"key": "name", "label": "姓名", "field_type": "string", "required": false}
    ],
    "records": [
      {
        "record_index": 1,
        "fields": {
          "name": {
            "raw_text": "张三",
            "value": "张三",
            "confidence": 0.98,
            "evidence_refs": [
              {"page_number": 1, "block_index": 12}
            ]
          }
        }
      }
    ]
  }
}
```

结构化提交必须同时携带后端签发或用户明确选择的 `dataset_id` 和领取时看到的 `expected_dataset_revision`。
WorkBuddy 不能用数据集名称猜测目标，也不能上传或覆盖累计 Excel。revision 已变化时，File Agent 应重新基于
当前数据集检查本次追加；如果字段 schema 未变化且幂等键尚未消费，可以安全重放，否则返回冲突并要求刷新。

后端必须限制字段数、记录数、单字段长度、总 JSON 体积和 blocks 数量。字段 key 使用安全稳定格式；字段
label 和值不能作为公式、路径、SQL 或指令执行。

## 5. File Agent 持久化适配

新增 `ExternalStructuredExtractionIngestService`，职责仅为把已校验的 WorkBuddy 结果转换为现有结构化抽取
领域对象，不复制现有抽取算法：

1. 校验任务归属、租约、幂等键、来源 SHA-256、DocumentVersion 和完整页面范围。
2. 把 OCR blocks 规范化为 `LayoutParseResult`，复用 `StructuredExtractionRepository.create_layout_extraction`。
3. 把表格记录规范化为 `CandidateExtraction`。
4. 复用现有字段类型归一化、证据校验和质量分级逻辑。
5. 写入 `StructuredExtractionRun` 与 `StructuredExtractionField`。
6. 把本次 `StructuredExtractionRun` 原子加入目标数据集，推进 revision。
7. 由 File Agent 生成该 revision 的累计 XLSX，写入 `StructuredDatasetExport` 并通过复用后的受控下载
   公共能力发布；不得把跨文档聚合导出伪装成某一个 Document 的派生件。
8. 写 ChangeSet/ChangeItem，至少记录正文、结构化字段、数据集追加和导出派生件事实。
9. 成功后继续现有查重、归档、分类、命名、落位和索引流程。

为避免调用现有 `StructuredExtractionService.execute_run` 后再次触发 File Agent 自己的版面 Provider，应把该
服务中“候选归一化、证据验证、完成运行、导出和审计”提取为可复用 finalizer。外部适配器调用 finalizer，
不能调用本地/云端 OCR Provider。

证据校验至少包括：

- `evidence_refs.page_number` 必须属于 claim 固定页集合。
- `block_index` 必须引用本次提交中的真实 block。
- bbox 必须是有限数值并位于页面范围内。
- `raw_text` 应能在引用 block 中定位；不能定位时字段降级为 `NEEDS_REVIEW`。
- 数字、日期、金额由 File Agent 确定性规范化，不能直接信任 WorkBuddy 的计算结果。
- 低置信度或冲突字段允许保存，但不得伪装为高质量完成。

## 6. 累计数据集与 Excel 保存

### 6.1 数据集模型

单张图片继续使用现有 `StructuredExtractionRun/Field` 保存识别事实。新增轻量数据集层负责组合多次运行，
不复制每个字段值：

```text
structured_datasets
- id
- workspace_id
- owner_user_id
- name
- schema_json
- schema_version
- revision
- status
- created_at / updated_at

structured_dataset_members
- id
- dataset_id
- structured_extraction_run_id
- source_document_id
- source_document_version_id
- source_record_index
- append_idempotency_key
- appended_revision
- status
- created_at

structured_dataset_exports
- id
- dataset_id
- dataset_revision
- export_type
- storage_backend / storage_path
- content_type / size_bytes / sha256
- status
- created_at / completed_at
```

唯一约束至少覆盖 `dataset_id + structured_extraction_run_id + source_record_index`，防止网络重试重复插入同一
图片记录。内容相同但来自不同图片的业务重复默认保留并标记为“疑似重复”，不能在没有用户规则时自动丢弃，
因为每条记录都需要保留独立原图证据。

目标数据集必须由用户首次创建或明确选择。可以在会话内复用最近一次明确选择，但不能仅根据 Excel 文件名、
字段相似或图片内容自动合并到已有数据集。

### 6.2 Schema 演进

数据集使用固定且版本化的字段 schema：

- 完全匹配：直接追加。
- 仅字段别名变化：通过受控 alias 映射后追加。
- 新增字段：默认停止追加并返回 schema 变更预览，由用户确认后升级 schema version。
- 缺少非必填字段：写空值并保留来源证据。
- 缺少必填字段或字段类型冲突：记录进入 `NEEDS_REVIEW`，不得污染正式导出。

### 6.3 XLSX 快照

Excel 是数据集某个 revision 的导出快照，不是主数据存储，也不应原地修改：

- 每次成功追加后异步生成新 revision 的 XLSX；高频批量场景可做短时间合并刷新。
- 文件名可以稳定，但 export ID、SHA-256 和 `dataset_revision` 必须变化。
- 下载默认返回最新 `COMPLETED` revision；旧导出按保留策略用于审计或回滚。
- 生成过程继续使用公式注入防护，不执行公式、宏或外部链接。
- 生成失败不回滚已提交的数据记录；导出任务可独立幂等重试。
- 数据集 revision 与导出 revision 不一致时，界面必须显示“数据已更新，Excel 正在刷新”。

现有 `StructuredExtractionExportService` 中的 `_tabular_rows`、单元格安全处理、原子写入和导出元数据投影应
抽成公共组件，并增加 `StructuredDatasetExportService` 按数据集 revision 聚合所有成员生成 XLSX。公共组件
不得假定导出一定属于单一 Document；现有单图导出仍由 `DocumentArtifact` 保存。

### 6.4 系统内表格

`StructuredExtractionField` 继续表达单图的“记录 × 字段”；数据集查询通过成员关系聚合多个运行，不新增一张
重复宽表。只有后续出现高频百万级统计需求时，再增加按数据集 revision 构建的专用物化投影。

## 7. 后续查询改造

现有 `file_search` 和 `evidence_answer` 可以复用，但不能单独承担所有结构化查询：金额汇总、日期范围、精确
字段过滤和排序必须由确定性工具执行。

新增 API：

```text
GET  /api/integrations/v1/structured-datasets/{dataset_id}
POST /api/integrations/v1/structured-datasets/{dataset_id}/query
GET  /api/integrations/v1/structured-datasets/{dataset_id}/exports/latest
```

新增 MCP Tool：

- `structured_dataset_get`：读取数据集 schema、revision、累计记录数、质量状态和最新 Excel 信息。
- `structured_dataset_query`：跨所有已追加图片执行字段白名单查询。

查询输入只允许：

- 后端返回且当前用户有权访问的稳定 `dataset_id`。
- 可选 `dataset_revision`；省略时读取最新已提交 revision。
- `select_fields`。
- 白名单过滤操作：`eq`、`contains`、`in`、`gte`、`lte`。
- 白名单排序字段和 `limit`，建议最大 200。

禁止输入 SQL、数据库列名、任意表达式、文件路径和聚合代码。需要 `sum/count/min/max` 时由后端按已声明
字段类型执行确定性聚合。

查询响应必须同时返回：

- 命中记录和字段值。
- `document_id`、`document_version_id`、`structured_extraction_run_id`。
- 每个字段的页码、bbox、原始文字和置信度。
- 原图预览或受控资源引用。
- 截断、低置信度和待复核提示。

普通“这张图讲了什么”仍可走现有 `evidence_answer`；“列出累计表中金额大于 1000 的记录”“合计所有图片
金额”等请求必须走 `structured_dataset_query`。

## 8. 状态、失败和重试

建议沿用现有任务状态，并增加结构化阶段信息：

```text
WAITING_EXTERNAL_EXTRACTION
-> CLAIMED
-> OCR_RECEIVED
-> STRUCTURED_VALIDATING
-> STRUCTURED_PERSISTING
-> DATASET_APPENDING
-> EXPORT_REFRESHING
-> COMPLETED / PARTIAL / FAILED
```

- OCR 成功但表格结构失败：保存 OCR，结构化结果标记 `PARTIAL/NEEDS_REVIEW`，不得丢失正文。
- 数据集追加成功但 Excel 生成失败：数据事实保持成功，导出状态单独失败并允许幂等重试。
- 并发追加 revision 冲突：在 schema 未变化时按幂等成员关系重新校验并追加；schema 已变化时停止并要求刷新。
- 租约过期或来源版本变化：拒绝提交，WorkBuddy 重新 claim 或发起新任务。
- 同一 submission key 同一摘要：返回已存在结果；同键不同结果：返回幂等冲突。
- WorkBuddy 识图失败：允许按现有 partial policy 提交真实失败页，不得构造空成功结果。

## 9. 需要修改的模块

### 后端

- `external_extraction/schemas.py`：新增 v2 结构化请求/响应 schema。
- `external_extraction/service.py`：冻结结构化 profile、验收结构化回传并调用适配器。
- `db/models.py` 与 Alembic：给外部任务增加 profile/结构化运行关联或等价版本化配置字段。
- `structured_extraction/service.py`：提取可复用 finalizer，避免外部结果再次 OCR。
- `structured_extraction/repository.py`：增加外部运行创建/复用和记录聚合读取。
- `structured_extraction/export.py`：抽取表格投影、公式安全和原子写入公共逻辑。
- 新增结构化数据集模型、Alembic 迁移、Repository、原子追加 Service 和 dataset revision 导出 Service。
- 新增结构化查询 Service、严格 schema 和 integration router。
- 索引流程：把结构化记录的可读文本投影到当前版本索引，同时保留字段证据关联。
- 审计：补充 ToolInvocation、ChangeSet、ChangeItem 和结构化日志事件。

### MCP

- 新增 `extraction_claim_v2`：复用现有 claim/lease 内部机制并返回冻结的结构化 profile；现有
  `extraction_claim` 请求和响应保持不变。
- 新增 `extraction_submit_v2`；不得扩展原 `extraction_submit` 的必填字段，也不得以任意 dict 长期承载契约。
- 新增 `structured_dataset_create/select`，要求用户首次明确数据集目标。
- 新增 `structured_dataset_get`、`structured_dataset_query`。
- WorkBuddy Skill 文档明确调用顺序、展示规则和刷新恢复方式。

### 前端与回执

- 文件整理回执增加“已提取 N 条记录、M 个字段、X 个待复核字段”。
- 展示 Excel/CSV 派生件下载入口。
- 表格结果按现有统一 WorkBuddy 风格展示，不把内部 run、表名或本地路径暴露给普通用户。

## 10. 测试范围

至少覆盖：

1. 单张图片、单表、多行记录成功回传并加入新数据集，File Agent 生成首个 XLSX revision。
2. 多张图片连续追加到同一数据集，累计 Excel 内容和 revision 正确。
3. WorkBuddy 自动发现字段与用户明确字段两种模式。
4. source SHA/version、租约、页集合、幂等键和 dataset revision 冲突。
5. 同一图片重试不重复追加，不同图片的相同业务记录保留来源并标记疑似重复。
6. schema 新增字段、缺失字段、类型冲突和用户确认升级。
7. 越权任务、越权数据集查询和越权数据集导出下载。
8. 字段/记录/体积上限及恶意嵌套 JSON。
9. bbox 越界、证据块不存在、raw text 无法定位时降级。
10. Excel 公式注入字符串不会执行，安全文本不会被误判。
11. OCR 成功但结构化失败、导出失败、部分页失败的可恢复行为。
12. 并发追加后 Excel 最终包含全部已提交 revision，不能发生最后写入覆盖。
13. 刷新后恢复 task、run、dataset revision、数据集导出和查询结果。
14. `file_search/dataset_get -> structured_dataset_query` 的 WorkBuddy MCP 端到端链路。
15. 原始图片哈希和存储内容始终不变。
16. LLM、OCR 和 embedding 测试使用 deterministic fake。

## 11. 推荐实施顺序

1. 冻结 `workbuddy-structured-extraction-v1` 输入输出契约和迁移。
2. 实现外部结果适配器，复用现有结构化 finalizer。
3. 实现数据集模型、明确选择、幂等追加和 revision 并发控制。
4. 实现按 dataset revision 生成版本化 XLSX 数据集导出。
5. 接通 `extraction_claim_v2 -> WorkBuddy 识别 -> extraction_submit_v2 -> dataset append -> export`；v2 Tool
   复用现有任务租约 Service，不复制任务状态机。
6. 实现结构化数据集只读查询 API。
7. 暴露 `structured_dataset_get/query` MCP Tool 并更新 WorkBuddy Skill。
8. 补齐回执、刷新恢复、审计和端到端测试。

第一阶段不建议做任意 SQL、跨文件自由 Join、用户自定义公式或直接覆盖生成的 Excel。这些能力会扩大权限和
数据一致性边界，应在稳定的单文件结构化查询之后独立设计。

## 12. 现有功能兼容与非回归约束

本功能必须采用增量接入，不能通过改写现有语义实现。以下约束属于上线门禁：

### 12.1 接口和 Tool 兼容

- 现有 `workbuddy_attachment_ingest` 输入、返回值和普通附件整理行为保持不变。
- 现有 `workbuddy-ocr-v1`、`extraction_claim`、`extraction_renew`、`extraction_submit` 的请求、响应及行为
  保持原契约；只要求 OCR 的任务继续走原路径，不携带 profile 或 dataset 字段。
- 新能力使用独立 `workbuddy-structured-extraction-v1` 契约以及 `extraction_claim_v2`、
  `extraction_submit_v2`。v2 Tool 复用原 claim/lease/renew/submit 的内部 Service 和状态机，但使用独立 schema，
  不在原 v1 schema 中增加字段，包括可选响应字段。
- `structured_dataset_*` 使用独立 MCP Tool 名称；不得改变 `file_search`、`file_read`、
  `evidence_answer`、分类、重命名和移动 Tool 的现有语义。
- 普通 File Agent 聊天页仍要求附件附带任务文字；本功能不得放宽聊天页提交规则。

### 12.2 路由隔离

- 只有同时满足“WorkBuddy 结构化入口、图片/PDF 类型、用户明确要求累计表格、功能开关开启”时才创建
  `STRUCTURED_TABLE` Profile。
- 普通图片上传、只做 OCR、只做总结、分类或证据问答时继续走现有流程。
- 目标数据集没有被用户明确创建或选择时不得猜测；返回结构化选择请求，不回落到任意已有数据集。
- v2 失败不得自动改走本地/外部结构化 Provider，除非部署配置显式启用降级且回执清楚标注 Provider。

### 12.3 数据库兼容

- 新增 `structured_datasets`、成员和导出表，不改变现有 `StructuredExtractionRun/Field` 的历史含义。
- 对现有表新增字段时必须允许 `NULL` 或提供不改变旧记录解释的默认值；迁移不得强制重算历史文件。
- 新外键使用现有 UUID 与 `timestamptz` 规范；删除策略不得级联删除原始 Document、DocumentVersion、
  DocumentPage、已有结构化运行或已有 Artifact。
- 新数据集成员只引用现有单图结构化运行，不搬迁或覆盖原有字段记录。
- 新导出使用独立 `StructuredDatasetExport` 记录和导出类型，例如 `STRUCTURED_DATASET_XLSX`；不能复用并改变
  `STRUCTURED_EXTRACTION_XLSX` 的含义，也不能为满足 `DocumentArtifact` 外键而伪造所属 Document。

### 12.4 文件与导出兼容

- 现有单图 `StructuredExtractionExportService.ensure_export` 方法签名、输出和缓存规则保持兼容。
- 公共 XLSX 写入逻辑通过新增公共函数抽取，旧服务继续调用同一逻辑；不能删除原有单图导出路径。
- 数据集 Excel 写入新的 `StructuredDatasetExport` 版本化记录和受控存储对象，不覆盖已有单图 Excel、
  用户上传 Excel 或原始图片。其下载接口复用现有鉴权、路径校验和流式响应公共能力，但不改变
  `DocumentArtifact` 的单文档派生件语义。
- 数据集导出失败只影响该导出任务，不回滚单图 OCR、分类、命名、索引或已经提交的数据集 revision。

### 12.5 配置与灰度

新增配置默认关闭：

```text
WORKBUDDY_STRUCTURED_DATASET_ENABLED=false
WORKBUDDY_STRUCTURED_EXTRACTION_V2_ENABLED=false
STRUCTURED_DATASET_AUTO_EXPORT_ENABLED=false
```

建议分阶段开启：

1. 先部署迁移和只读代码，功能开关关闭。
2. 对测试用户开启 v2，只持久化单图结构化结果，不追加正式数据集。
3. 开启数据集追加，Excel 导出处于 shadow 校验模式。
4. 对比结构化记录和 shadow Excel 后，再发布正式下载链接。
5. 最后开启自动导出；旧 v1 和现有单图路径始终保留。

关闭开关必须立即停止创建新数据集任务，但不能影响历史数据集只读查询和已有导出下载。若数据集任务处理器
出现问题，可关闭 dataset task type 的派发与消费；共享的 `FilesystemJobQueue` 和普通文件生命周期 worker
仍须继续处理既有任务类型，不新建第二套队列。

### 12.6 回归测试门禁

除第 10 节新增测试外，每次合并前必须执行现有完整验证：

```bash
cd apps/api
pytest -v

cd apps/mcp
pytest -v

cd apps/web
npm run build
```

并增加以下兼容测试：

- 原 v1 OCR 请求和响应 JSON 快照完全兼容。
- 功能开关关闭时，现有图片上传、OCR、分类、命名、查重和索引结果不变。
- 非图片附件和普通聊天上传不创建 dataset 记录。
- 现有 `STRUCTURED_EXTRACTION_XLSX` 下载、复用和权限行为不变。
- 新数据集 worker 失败时，普通文件生命周期 worker 和 MCP 只读 Tool 仍可工作。
- 同时运行 v1 OCR、v2 结构化提取和现有本地结构化 Provider 时，任务、缓存和结果互不串用。
- 数据库迁移升级及回滚检查不丢失现有 Document、WorkingCopy、分类、证据和 Artifact。

### 12.7 验收标准

只有满足以下条件才允许默认开启：

1. 所有现有自动测试保持通过，不能用删除或放宽旧断言换取通过。
2. 旧 v1 WorkBuddy 客户端无需升级即可继续使用。
3. 关闭新功能开关后，程序行为与改造前一致。
4. 新功能产生的表、dataset task type 和数据集导出均能独立停用；过期数据按保留策略清理，来源证据、
   ChangeSet、ToolInvocation 和其他必须保留的审计记录不得随功能停用删除；共享队列中的既有任务不受影响。
5. 原图、已有 Excel、已有分类、既有工作副本和历史审计记录均未被覆盖或重新解释。

## 13. 复用优先实施约束

本功能不是另起一套上传、OCR、表格导出或文件查询系统。实现时必须先扩展现有服务；只有现有领域模型确实
无法表达“多张图片持续追加到同一个结构化数据集”时，才允许增加新模型和 Service。代码评审应按下表核对：

| 能力 | 必须复用的现有实现 | 本次允许的扩展 | 禁止重复实现 |
|---|---|---|---|
| WorkBuddy 附件接收 | `AttachmentTransferService` | 复用其产出的受控 import/document 引用；profile 和 dataset 目标由后续 v2 编排契约携带 | 修改原 `workbuddy_attachment_ingest` 契约，或增加第二套附件下载、路径解析、入库流程 |
| 外部 OCR 任务 | `ExternalExtractionService`、现有 claim/renew/submit 租约机制 | 新增 v2 Tool/schema、冻结 profile 和结构化结果适配器，内部调用原任务状态机 | 修改旧 Tool schema，或新建独立任务表、租约管理器、重试框架 |
| 接口幂等 | `IntegrationRequest` 及现有 integration request 持久化流程 | 增加 dataset append 对应的 operation/key | 仅靠进程内缓存、临时文件或前端状态去重 |
| 页面与版面证据 | 现有 `DocumentExtractionRun`、`DocumentPage`、`DocumentElement` | 保存 WorkBuddy 返回的 bbox、页码和可定位证据 | 另建一套 OCR page/element 真相表 |
| 单图结构化事实 | `StructuredExtractionRun`、`StructuredExtractionField`、`StructuredExtractionRepository.create_layout_extraction` | 增加外部 Provider 适配和运行来源 | 把同一字段事实复制到数据集表中 |
| 结构化完成链路 | `StructuredExtractionService`、`StructuredExtractionRepository.complete_run`、`record_changeset` | 抽取可复用 finalizer，让外部结果跳过二次 OCR 后进入同一校验与审计链路 | 为 WorkBuddy 单独复制规范化、质量门槛和 ChangeSet 逻辑 |
| XLSX 安全生成 | `StructuredExtractionExportService`、`_safe_spreadsheet_text`、`_write_xlsx_atomic` | 抽取公共投影/写入能力；新增数据集导出编排器 | 新写一套 openpyxl 写入、公式防护或原子发布代码 |
| 导出对象管理 | 现有 Artifact 的哈希、元数据、授权下载、路径校验和流式响应公共能力 | 由 `StructuredDatasetExport` 保存 dataset revision 与导出对象关系；抽取并复用不绑定单 Document 的公共能力 | 把聚合导出伪装成某一 Document 的派生件、改变 `DocumentArtifactRepository.upsert_link` 语义、直接暴露本地路径 |
| 文件存储 | `FileLifecycleStorageService` | 增加数据集导出命名空间和受控发布方法 | 在 MCP Tool 或路由中直接拼接、读写本地路径 |
| 异步执行 | `FilesystemJobQueue` 和现有 worker 扩展机制 | 新增可独立开关的 dataset append/export task type 和 handler | 引入第二套后台队列或轮询守护进程 |
| 索引与自然语言检索 | `DocumentIndexService`、现有 `file_search` 和 `evidence_answer` | 把结构化记录投影进既有索引，并新增确定性 dataset query | 复制全文/向量索引或让 WorkBuddy 自建长期查询库 |
| 权限与租户隔离 | 现有 JWT/current-user、workspace ownership 和 Artifact 下载校验 | 为 dataset、member、export 增加同样的所有权检查 | MCP 自报 user/workspace 后直接信任，或创建旁路鉴权 |
| 审计与回执 | `ChangeSetRepository`、ToolInvocation、结构化日志和现有回执模型 | 增加数据集追加、导出 revision 的变更类型与用户可读摘要 | 只写普通日志而不进入现有审计链路 |

### 13.1 真正需要新增的领域能力

新增代码应收敛在以下范围：

1. `StructuredDataset`、成员关系和导出 revision，用来表达跨图片累计数据集；成员只引用既有
   `StructuredExtractionRun`，不复制单图字段真相。
2. 数据集 Repository/Service，负责 schema 兼容、幂等追加、revision 乐观锁和疑似业务重复标记。
3. 数据集导出编排器，负责把指定 revision 的累计记录投影给复用后的 XLSX 公共写入逻辑。
4. 数据集确定性查询 Service，只开放字段筛选、排序、分页和聚合白名单，不接收任意 SQL。
5. WorkBuddy v2 结构化 schema、外部结果适配器及 `structured_dataset_*` MCP Tool。

如果实施过程中发现需要新增上述范围之外的上传、OCR 调度、Excel Writer、Artifact 下载、权限、幂等、索引
或审计实现，必须先证明现有服务无法安全扩展，并同步更新本方案；否则视为不符合方案。

### 13.2 复用验收

测试除验证最终业务结果外，还必须保护复用边界：

- 外部结构化提交最终调用现有结构化完成和 ChangeSet 持久化链路；数据集导出复用 Artifact 的公共安全能力，
  但保存到独立 `StructuredDatasetExport`，不伪造单文档关联。
- 数据集导出与单图导出共享公式防护和原子写入测试用例。
- WorkBuddy 附件仍经过 `AttachmentTransferService` 的授权根、MIME、大小和哈希校验。
- MCP 查询仍经过当前用户和 workspace 所有权校验；数据集下载复用既有下载鉴权和路径安全规则，并对
  dataset ownership 进行额外校验。
- 关闭新功能开关后，不注册或不执行新数据集写入路径，原有 Tool 与文件生命周期测试结果保持不变。

### 13.3 第 12 节与第 13 节的适用顺序

“复用”指复用经过验证的内部 Service、状态机和安全公共能力，不等于必须复用同一个公共 Tool 名称、请求
schema 或数据库行类型。两节发生表面张力时，按以下顺序处理：

1. 第 12 节的旧接口、旧数据语义和关闭开关后的非回归约束优先，不能为了减少代码而修改旧契约。
2. 能在不改变旧语义的前提下复用时，通过新增方法、适配器或 v2 Tool 调用现有内部 Service。
3. 现有领域模型无法正确表达新对象时，新增窄范围模型并抽取公共安全能力；不得用伪造外键或重解释旧记录
   的方式追求表面复用。`StructuredDatasetExport` 即属于该情形。
4. 任何新增公共抽取都必须让旧调用路径继续通过原回归测试；不得要求旧客户端、历史数据或既有配置迁移到
   新语义。
