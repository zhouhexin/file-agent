# WorkBuddy 累计表格 Demo

> 当前状态（2026-10-09）：应用户要求，WorkBuddy 插件已移除 `file-agent-structured-dataset` Skill 及其任务参数参考。下文保留为历史方案；服务端 `structured_dataset_*` 工具与 API 本次未删除。教授汇总表 B–I/L 填写、按文件名追加和手工补全回传分别由当前专用 Skill 处理。

> 2026-09-20 需求扩展：在现有图片 Demo 上增加 Word/PDF 内容抽取到 Excel；OCR 及 Word/PDF 正文、
> 表格提取全部由 WorkBuddy 完成，File Agent 负责接收、校验、保存和生成 Excel。详见
> [三格式扩展方案](2026-09-20-workbuddy-document-to-excel-demo-extension-plan.md)。该扩展目前处于方案阶段，
> 本文下面描述的现有实现仍以单页图片为范围。
> 实施扩展请按[可执行开发计划](2026-09-20-workbuddy-document-to-excel-development-plan.md)逐步开发和验收。
>
> 2026-09-21 保存规则修订：显式内容提取按本轮提示词进入复核后保存或明确授权的直接保存路径；未明确跳过
> 复核时仍默认先展示并等待确认。详见
> [显式内容提取复核分流方案](2026-09-20-workbuddy-explicit-extraction-confirmation.md)。
>
> 2026-09-20 故障修订：MCP 的 `structured_dataset_append` 必须使用 JSON 模式序列化嵌套
> `evidence_refs.source_unit_id`，避免 Python UUID 导致请求发送前失败。append 失败或正式查询结果为零时，
> Skill 必须停止，不能继续导出只有标题行的 Excel，也不能把该链路报告为完成。

## 当前范围（2026-09-18 用户修订）

先增加可运行的小 Demo，尽量复用已完成能力。所有登录用户共同操作共享工作副本和 Demo 数据集；不增加所有者限制或用户授权管理。显式内容提取根据提示词进入复核确认或明确授权的直接保存路径，不新增确认页面。原有插件入口及纯归档、OCR、分类和文件操作语义保持兼容。

本文件是本轮 Demo 的直接实施依据；[完整方案](2026-09-18-workbuddy-image-structured-extraction-integration-plan.md) 的 v2 OCR 调度、schema 演进、复杂权限和后台自动刷新留待 Demo 验证后完善。此前审查中的“旧入口冲突”修正为入口兼容提醒，不作为新增能力的阻塞条件。

## 最小链路

1. 明确提取请求固定使用 `WORKBUDDY_DOCUMENT`，并按提示词冻结为
   `REVIEW_REQUIRED + CONFIRM_BEFORE_SAVE` 或 `DIRECT_SAVE + DIRECT_ARCHIVE`。
2. 复核路径读取固定文件后调用 preview，展示逐文件结果并等待后续确认，再 confirm/submit；直接路径完成同等
   证据判定后直接 submit。两条路径都在 submit 成功前不创建数据集、不保存正文、不追加记录、不导出 Excel。
3. 新增工具创建一个共享累计表格，首次固定字段 schema；用户不需要额外权限授权或选择所有者。
4. source 工具返回按冻结策略提交并验收的工作副本版本、哈希和完整页面。WorkBuddy 按表格 schema 整理字段，回传每条记录的原文及定位证据。
5. append 工具验证版本、哈希、原文支持和字段类型，复用 StructuredExtractionRun/Field 保存单文件结果；数据集只引用该运行。
6. 查询累计记录及确定性合计；显式导出工具把当前 revision 发布为服务器上的唯一普通工作副本。
   后续导出只为同一逻辑文件增加 `DocumentVersion` 并更新当前版本，不生成第二个独立文件。
   不同用户可以继续追加、查询和下载。

纯归档产生的既有 ACTIVE 工作副本可直接开始旧图片 Demo；用户在本轮明确要求提取新附件内容时，必须走
`document_extraction_get/claim` 后按冻结路径执行 `preview/confirm/submit` 或直接 `submit`，不得在没有本轮
明确跳过复核表达时把默认归档授权替代结果确认。

## 简化选择

- 首期仅支持单页 PNG/JPEG/WebP/BMP 图片，每图最多 100 条、表格最多 1000 条、字段最多 20 个。
- 固定 schema，首期类型为 string、person_name、organization、integer、decimal、date；不做 schema 升级、跨页 PDF、币种换算、多值字段或业务去重。
- 共享数据集显式创建或使用工具返回的稳定 ID，无 owner 权限。created_by 只用于审计。
- 工作副本版本和成功解析运行是源事实；服务端从完整页面生成结构化运行所需的可定位元素，记录来源运行，不调用 OCR/LLM Provider，不修改旧页面或索引。
- 非空字段必须能在指定页原文中定位。找不到或类型不符时保留待复核状态；缺失必填字段或任一待复核字段的记录不计入默认查询、合计和正式 Excel，返回排除数量。可查询全部记录用于检查。
- 一张图片内容版本在同一数据集只能追加一次；相同键同载荷返回原结果，不同载荷返回冲突。数据库锁保证成员和 revision 一起提交。
- Excel 按需生成，复用现有原子 XLSX writer 和公式安全处理，发布为单一普通工作副本并保留版本记录。
  导出失败不回滚追加；重复导出同一 revision 复用同一文件版本。
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

字段类型示例：姓名 `string`，金额 `decimal`。WorkBuddy 先提取并展示两张图片的结果；用户回复明确确认后，
才依次执行 confirm/submit、`structured_dataset_create`、每图 `structured_dataset_source/append`、
`structured_dataset_query`、`structured_dataset_export`。
要测试另一用户，只需用另一登录用户现有 Token 启动 MCP，使用 list/get 找到同一表格，再查询或追加。
不需要授予所有者权限；这里的内容确认只用于证明用户接受已展示的提取结果，不是数据集权限审批。

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

导出 API URL 需要现有登录。服务器先把 XLSX 版本保存到
`WORKING_COPY_STORAGE_ROOT/structured-datasets` 并注册为普通工作副本；WorkBuddy 的 export Tool 再使用
MCP Token 下载当前版本到已有受控缓存并返回 `ResourceLink`。宿主缓存只是交付副本，不是唯一事实存储。
完整发布约束见 [累计表格单一工作副本发布方案](2026-09-20-structured-dataset-working-copy-publication.md)。

关闭开关会关闭全部 Demo 路由和新 MCP 工具；旧能力继续工作，Demo 数据保留。此阶段没有历史 revision 查询
接口，但已经生成的固定 export ID 保留旧快照；当前查询只读最新 revision。

## 2026-09-20 验证记录

- 新增 API Demo 测试覆盖共享累计、版本和幂等、证据缺失、字段类型/置信度、日期查询、导出失败恢复和迁移；隔离 SQLite 显式开启外键。
- 迁移在隔离 SQLite 上验证升级/回退，并离线编译 PostgreSQL JSONB/timestamptz/RESTRICT DDL；没有对现有部署数据库做变更，也没有声称已完成 PostgreSQL 并发压力验证。
- 新增 Demo、配置及现有结构化抽取/外部 OCR 联合验证通过 93 项；新增 MCP 工具、下载与旧 MCP 清单联合验证通过 31 项。
- MCP 全量运行 90 项通过、1 项因工作区缺少既有 `integrations/workbuddy/file-agent-marketplace-0.1.10.zip` 发布包失败；未修改旧断言，也未伪造旧版本发布包。
- 尚未连接真实 WorkBuddy 宿主做人工试用；该步骤用于判断字段识别效果，再决定后续完善内容。
