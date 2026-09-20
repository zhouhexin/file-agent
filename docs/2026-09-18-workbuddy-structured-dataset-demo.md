# WorkBuddy 累计表格 Demo

> 2026-09-20 需求扩展：在现有图片 Demo 上增加 Word/PDF 内容抽取到 Excel；OCR 及 Word/PDF 正文、
> 表格提取全部由 WorkBuddy 完成，File Agent 负责接收、校验、保存和生成 Excel。详见
> [三格式扩展方案](2026-09-20-workbuddy-document-to-excel-demo-extension-plan.md)。该扩展目前处于方案阶段，
> 本文下面描述的现有实现仍以单页图片为范围。
> 实施扩展请按[可执行开发计划](2026-09-20-workbuddy-document-to-excel-development-plan.md)逐步开发和验收。

## 当前范围（2026-09-18 用户修订）

先增加可运行的小 Demo，尽量复用已完成能力。所有登录用户共同操作共享工作副本和 Demo 数据集；不增加所有者限制、用户授权管理或额外确认页面。原有插件入口及上传、OCR、分类和文件操作语义保持兼容。

本文件是本轮 Demo 的直接实施依据；[完整方案](2026-09-18-workbuddy-image-structured-extraction-integration-plan.md) 的 v2 OCR 调度、schema 演进、复杂权限和后台自动刷新留待 Demo 验证后完善。此前审查中的“旧入口冲突”修正为入口兼容提醒，不作为新增能力的阻塞条件。

## 最小链路

1. 图片经过现有 WorkBuddy 附件导入、OCR 和整理，成为共享 ACTIVE 工作副本。
2. 新增工具创建一个共享累计表格，首次固定字段 schema；用户不需要额外授权或选择所有者。
3. 新增 source 工具返回当前工作副本的版本、哈希和已持久化的完整 OCR 页面。WorkBuddy 按表格 schema 整理字段，回传每条记录的原文及页码。
4. 新增 append 工具验证版本、哈希、原文支持和字段类型，复用 StructuredExtractionRun/Field 保存单图结果；数据集只引用该运行。
5. 查询累计记录及确定性合计；显式导出工具生成当前 revision 的 XLSX 快照。不同用户可以继续追加、查询和下载。

不要求新的 OCR claim/submit，也不在默认导入的 user_request 中再次安排结构化任务。需要上传时沿用现有宿主入口；已有工作副本可直接开始本 Demo。

## 简化选择

- 首期仅支持单页 PNG/JPEG/WebP/BMP 图片，每图最多 100 条、表格最多 1000 条、字段最多 20 个。
- 固定 schema，首期类型为 string、person_name、organization、integer、decimal、date；不做 schema 升级、跨页 PDF、币种换算、多值字段或业务去重。
- 共享数据集显式创建或使用工具返回的稳定 ID，无 owner 权限。created_by 只用于审计。
- 工作副本版本和成功解析运行是源事实；服务端从完整页面生成结构化运行所需的可定位元素，记录来源运行，不调用 OCR/LLM Provider，不修改旧页面或索引。
- 非空字段必须能在指定页原文中定位。找不到或类型不符时保留待复核状态；缺失必填字段或任一待复核字段的记录不计入默认查询、合计和正式 Excel，返回排除数量。可查询全部记录用于检查。
- 一张图片内容版本在同一数据集只能追加一次；相同键同载荷返回原结果，不同载荷返回冲突。数据库锁保证成员和 revision 一起提交。
- Excel 按需生成，复用现有原子 XLSX writer 和公式安全处理，保存独立导出记录。导出失败不回滚追加；重复导出同一 revision 复用文件。
- 新写入通过固定白名单的最小 LangGraph dispatch 执行，沿用 AgentRun、ToolInvocation、ChangeSet 记录，不把正文写入审计快照。
- 保留现有登录、稳定 ID、版本校验及存储路径保护。这些是现有基础能力，不新增用户授权功能。

## 验证

使用隔离数据库和 deterministic fixtures 验证两张图片累计、跨用户访问、同请求重试、同版本防重复、源版本错误、无原文支持、数值过滤与合计、XLSX 内容及公式防护。回归现有结构化抽取、外部 OCR 和 MCP 契约；真实 WorkBuddy 试用在本地验证后进行，不把 fake 测试称为宿主实测。

## 启用和试用

在仓库根，使用现有 Python 环境执行迁移；不要创建新虚拟环境：

```bash
python -m alembic -c apps/api/alembic.ini upgrade head
```

新增迁移 `20260920_0001` 只增加累计表格的三张表。本次实现没有自动对已有部署数据库执行迁移。

在 API 的 `.env` 中设置：

```dotenv
WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED=true
```

在 WorkBuddy 现有 `file-agent` MCP 配置的 `env` 中额外添加同名值 `"true"`。API 和 MCP 都需重启，MCP
不自动读取 API 的 `.env`。API 端口和 Python 启动方式不变，无新增依赖、后台进程或模型安装。
更新插件目录时带上新增的 `skills/file-agent-structured-dataset/SKILL.md`，保留原 Skill 和 Hook。

先用原有流程整理两张内容清楚的图片，再对 WorkBuddy 说：

> 新建一张“登记汇总”累计表，字段是姓名和金额。把已整理的这两张图片里的每条记录追加进去，告诉我累计多少条，最后导出 Excel。

字段类型示例：姓名 `string`，金额 `decimal`。WorkBuddy 依次执行 `structured_dataset_create`、每图
`structured_dataset_source/append`、`structured_dataset_query`、`structured_dataset_export`。
要测试另一用户，只需用另一登录用户现有 Token 启动 MCP，使用 list/get 找到同一表格，再查询或追加。
不需要授予所有者权限，不需要二次批准。

后续可以说：

> 继续把这张已整理图片追加到“登记汇总”，再统计金额大于 1000 的记录及合计。

工具返回的字段置信度用于字段映射质量，不是 OCR 服务评分。无法评估时保留 null 并标记待复核；不能为了
让导出有数据而强填高分。当前没有复核编辑页面；修正/复核、复杂 schema 演进等在 Demo 验证后增加。

## API 契约

所有接口沿用现有 Bearer 登录；数据集和工作副本不按创建人限制访问。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/structured-datasets` | 共享表格列表，最多 100 个 |
| `POST /api/structured-datasets` | 固定字段 schema 创建，含 name、fields、idempotency_key |
| `GET /api/structured-datasets/sources/{document_id}` | ACTIVE 图片的完整持久化 OCR、版本、源哈希 |
| `GET /api/structured-datasets/{dataset_id}` | schema、revision、图片数、记录数 |
| `POST /api/structured-datasets/{dataset_id}/records` | 单图结构化候选追加 |
| `POST /api/structured-datasets/{dataset_id}/query` | 字段过滤、分页、确定性求和 |
| `POST /api/structured-datasets/{dataset_id}/exports` | 生成或复用当前 revision 的 XLSX |
| `GET /api/structured-datasets/{dataset_id}/exports/{export_id}/content` | 下载固定快照 |

创建请求：

```json
{
  "name": "登记汇总",
  "idempotency_key": "demo-create-001",
  "fields": [
    {"key": "name", "label": "姓名", "field_type": "string", "required": true},
    {"key": "amount", "label": "金额", "field_type": "decimal", "required": true}
  ]
}
```

追加必须原样使用 source 返回的 `document_id`、`document_version_id`、`extraction_run_id`、
`source_sha256`，另外传 `idempotency_key` 和 `records`。每条记录为
`{"fields":{"name":{"raw_text":"张三","page_number":1,"confidence":0.98}}}`。
该数值只是格式示例，实际应按真实映射结果提供；不得照抄。缺失必填字段、无法在原文定位、类型不符或低
置信度会使记录进入待复核。数字规范化值由后端产生，不接受客户端传入计算结果。

查询示例：

```json
{
  "filters": [{"field": "amount", "op": "gte", "value": "1000"}],
  "sum_field": "amount",
  "include_review": false,
  "offset": 0,
  "limit": 100
}
```

返回 `revision`、`total_records`、`review_records`、`matched_records`、`sum`、`records`、`truncated`。
金额使用 Decimal 运算，sum 为十进制字符串，过滤后先汇总再分页。即使 include_review=true，待复核记录
也不纳入 sum。字段证据包含原文、真实页码；本 Demo 从旧 OCR 全文复用，缺失 bbox 保持空，不补造坐标。

导出 API URL 需要现有登录。WorkBuddy 的 export Tool 会使用 MCP Token 下载到已有受控缓存，并返回
`ResourceLink`，用户可从宿主拿到实际 XLSX，不必自己处理 API 认证。

关闭开关会关闭全部 Demo 路由和新 MCP 工具；旧能力继续工作，Demo 数据保留。此阶段没有历史 revision 查询
接口，但已经生成的固定 export ID 保留旧快照；当前查询只读最新 revision。

## 2026-09-20 验证记录

- 新增 API Demo 测试覆盖共享累计、版本和幂等、证据缺失、字段类型/置信度、日期查询、导出失败恢复和迁移；隔离 SQLite 显式开启外键。
- 迁移在隔离 SQLite 上验证升级/回退，并离线编译 PostgreSQL JSONB/timestamptz/RESTRICT DDL；没有对现有部署数据库做变更，也没有声称已完成 PostgreSQL 并发压力验证。
- 新增 Demo、配置及现有结构化抽取/外部 OCR 联合验证通过 93 项；新增 MCP 工具、下载与旧 MCP 清单联合验证通过 31 项。
- MCP 全量运行 90 项通过、1 项因工作区缺少既有 `integrations/workbuddy/file-agent-marketplace-0.1.10.zip` 发布包失败；未修改旧断言，也未伪造旧版本发布包。
- 尚未连接真实 WorkBuddy 宿主做人工试用；该步骤用于判断字段识别效果，再决定后续完善内容。
