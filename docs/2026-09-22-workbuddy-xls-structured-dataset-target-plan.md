# WorkBuddy 上传 XLS 作为累计表格目标的转换方案

## 0. 2026-09-22 实施记录

本方案的服务端主链路已实现：

- `structured_dataset_create_from_excel` 已增加 `extraction_run_id` 与 `legacy_target_policy`；`.xlsx` 保持同步注册，`.xls` 必须使用 `CONVERT_TO_XLSX`。
- `.xls` 首次调用返回 HTTP 202、`PREPARING_TARGET` 与稳定 `filesystem_job_id`；任务进入现有 `STRUCTURED_EXTRACTION` worker 队列，由 `STRUCTURED_DATASET_TARGET_PREPARE` handler 消费。
- worker 复用 `OfficeDerivativeService + XLS_TO_XLSX`，不新增第二套 XLS 解析器；转换前固定 WorkBuddy `COMPLETED` 的 `SHEET` 提取运行，转换后交叉核对工作表与标题。
- 提升后继续使用同一 `Document` 和 `WorkingCopy`，建立以源 `.xls` 为父版本的 `.xlsx` `DocumentVersion`；可见文件名、物理 basename、`ManagedFile`、`FileObject` 与下载入口统一为 `.xlsx`。
- 源 `.xls` 移入数据集隐藏 `.source-versions` 路径，只保留一个普通可见 `.xlsx` 文件；后续版本继续进入 `.versions`。
- 异步等待、完成和失败会同步更新 `AgentRun`、`ToolInvocation`、`ChangeSet/ChangeItem`、任务结果及 `WorkingCopyPathRecord`；确定性校验失败不重试，服务端异常最多按任务上限重试。
- 自动化回归已覆盖 MCP 参数透传、202 异步返回、任务轮询结果、幂等复用、同一稳定 ID、父子版本、WorkBuddy schema 核对以及单一可见文件。

尚需在真实部署环境使用安装好的 LibreOffice 与真实 `.xls` 做一次 WorkBuddy 验收；这不影响代码层实现状态。

日期：2026-09-22

状态：服务端格式提升、MCP 参数与异步 job 已实现并通过自动化回归；待真实 LibreOffice/WorkBuddy 环境验收。

## 1. 问题与结论

当前 WorkBuddy 可以通过真实电子表格能力读取 `.xls`，File Agent 也已经具备受控的
`LibreOffice Headless -> CONVERTED_XLSX -> openpyxl` 转换链路，但累计表格
`structured_dataset_create_from_excel` 只允许当前工作副本为 `.xlsx`。因此，用户把一个新上传的 `.xls`
选为持续追加目标时，会在目标注册阶段得到 `TARGET_EXCEL_TYPE_UNSUPPORTED`，后续 append/export 无法继续。

推荐方案不是让累计表格继续写旧版 `.xls`，而是增加“旧版目标表格式提升”流程：

```text
WorkBuddy 真实读取上传的 .xls
-> 展示工作表、标题、已有行及转换说明
-> 用户确认，或本轮已明确 DIRECT_SAVE
-> File Agent 使用既有受控 LibreOffice 链路生成并校验 .xlsx
-> 把转换结果发布为同一个 Document / WorkingCopy 的新版本
-> 用户可见文件名由 原名.xls 变为 原名.xlsx
-> 注册 StructuredDataset
-> 追加本轮来源材料数据
-> export 发布后续 DocumentVersion
```

转换后只保留一个用户可见的当前目标文件；原 `.xls` 字节不被覆盖，作为该 Document 的历史来源版本保存。
后续用户必须使用转换后的 `.xlsx` 文件名继续追加。

## 2. 为什么采用格式提升

### 2.1 不直接写 XLS

- 当前累计表格 writer 使用 `openpyxl`，只能安全写 `.xlsx`，不能写 BIFF `.xls`。
- `.xls` 行列、公式、编码和兼容性边界复杂，不适合作为持续版本化写入格式。
- 强行引入 `xlwt/xlrd` 会形成第二套表格写入和校验链路，也违反项目现有“旧版 `.xls` 统一通过
  LibreOffice 转换”的规则。

### 2.2 不让 WorkBuddy 在客户端另存文件

- WorkBuddy 的职责是读取宿主附件、生成可验证正文/单元格证据，不应直接写服务器工作副本。
- 客户端另存无法可靠绑定服务器 `Document ID`、`WorkingCopy ID`、`DocumentVersion ID` 和数据集 ID。
- 服务端无法保证同名冲突、单一工作副本、版本链、ChangeSet 和审计一致性。

### 2.3 复用现有转换链路

`apps/api/app/modules/files/office_conversion.py` 已经提供：

- `XLS_TO_XLSX` 转换规格；
- LibreOffice 可执行文件发现、并发限制和超时；
- 隔离输入、输出和用户 profile；
- OOXML ZIP 与 openpyxl 结果校验；
- `CONVERTED_XLSX` 持久派生件、源哈希和转换配置哈希；
- 幂等复用与结构化错误。

新功能只应增加“累计目标提升与发布”，不能再实现一套 XLS 解析器，也不能用模型、临时脚本或文件名猜测
代替转换。

## 3. 用户行为

### 3.1 目标选择

目标优先级调整为：

```text
本轮唯一新上传的 .xls 或 .xlsx
-> 提示词指定的已有 .xlsx 文件名
-> 根据提示词创建新的 .xlsx
```

- 本轮只有一个 `.xls`，且还有 PDF、Word、图片等材料时，该 `.xls` 作为新目标表。
- 同时上传多个 `.xls/.xlsx` 时必须让用户选择，不能静默优先 `.xlsx`，也不能按上传顺序选择。
- `.xls` 只是普通来源文件、用户没有要求把它作为目标表时，继续由 WorkBuddy 读取，不触发目标格式提升。
- 已经转换并注册后，后续只能按实际可见的 `.xlsx` 文件名定位目标。

### 3.2 用户可见提示

复核路径必须在确认前展示：

```text
目标来源：人才材料登记表.xls
保存后的目标文件：人才材料登记表.xlsx
工作表：登记表
字段：姓名、单位、审批项目……
已有数据：12 行
说明：原 XLS 不会被覆盖；将作为历史来源版本保留，服务器文件列表只显示当前 XLSX。
```

`DIRECT_SAVE` 路径不追加一次复核，但最终回执必须说明发生了格式提升、最终文件名、当前版本和稳定 ID。
“直接保存”不能绕过转换失败、同名冲突、工作表歧义、版本变化或 schema 校验。

### 3.3 文件名

- `人才材料登记表.xls` 的目标名确定性变为 `人才材料登记表.xlsx`。
- 不添加 `(2)`、时间戳、UUID 或 `converted` 后缀。
- 如果工作区已经存在活动的 `人才材料登记表.xlsx`，返回 `TARGET_EXCEL_FILENAME_CONFLICT`，由用户选择
  使用已有文件、修改新目标名称或取消；不能覆盖或自动改名。
- 转换完成后，文件列表名、当前物理 basename、下载名和后续引用名都必须是同一个 `.xlsx` 名称。

## 4. WorkBuddy 与服务端职责

### 4.1 WorkBuddy

WorkBuddy 继续负责：

1. 对 `.xls` 使用已安装、已授权的真实电子表格能力读取全部工作表。
2. 返回真实 `SHEET` unit、`TABLE_CELL` block、sheet name、行列坐标、标题和已有值。
3. 在 `REVIEW_REQUIRED` 中展示目标 schema、已有行和格式提升说明。
4. 用户确认后提交固定的 extraction result；`DIRECT_SAVE` 按已有规则直接提交。
5. 调用受控 MCP 工具注册目标，不自行运行 LibreOffice、Shell 或文件写入。

WorkBuddy 的读取结果用于用户预览和转换后一致性校验。File Agent 的 LibreOffice 只负责把旧文件变成可持续
写入的 `.xlsx`，不能替代 WorkBuddy 的本轮内容预览。

### 4.2 File Agent

File Agent 负责：

1. 固定并校验源 `document_id + document_version_id + source_sha256`。
2. 锁定当前 WorkingCopy，防止转换期间文件被改名或更新。
3. 复用或生成 `CONVERTED_XLSX` 派生件。
4. 用 openpyxl 读取转换结果，并与 WorkBuddy 已提交的工作表、标题和使用区结构交叉校验。
5. 保存原 `.xls` 历史版本，创建同一 Document 的新 `.xlsx` DocumentVersion。
6. 更新同一 WorkingCopy 和 ManagedFile 的当前名称、路径、扩展名、大小与哈希。
7. 创建 StructuredDataset 并绑定新的当前 `.xlsx` 版本。
8. 写 ToolInvocation、ChangeSet、ChangeItem 和 WorkingCopyPathRecord。

## 5. API 与 MCP 契约

### 5.1 保留现有工具名

继续使用 `structured_dataset_create_from_excel`，将其职责从“只接受 `.xlsx`”扩展为：

> 把本轮已入库的 `.xls` 或 `.xlsx` 工作副本注册为新的累计目标；`.xls` 先经过受控格式提升。

建议输入扩展为：

```json
{
  "document_id": "uuid",
  "document_version_id": "uuid",
  "extraction_run_id": "uuid",
  "sheet_name": "登记表",
  "legacy_target_policy": "CONVERT_TO_XLSX",
  "idempotency_key": "stable-key"
}
```

规则：

- `.xlsx` 忽略 `legacy_target_policy`，沿用现有注册流程。
- `.xls` 必须显式为 `CONVERT_TO_XLSX`，防止模型在用户未选择目标时触发转换。
- `extraction_run_id` 必须属于同一源版本、来自 `WORKBUDDY`、状态已完成且覆盖目标工作表。
- 同一幂等键和相同固定版本重复调用只复用原任务或结果；载荷不同返回 `IDEMPOTENCY_CONFLICT`。

### 5.2 异步转换

`.xlsx` 注册仍可同步返回。`.xls` 转换建议进入现有 `filesystem_jobs`，避免 LibreOffice 耗时导致 MCP/HTTP 超时。
第一次调用返回：

```json
{
  "status": "PREPARING_TARGET",
  "kind": "filesystem_job",
  "filesystem_job_id": "uuid",
  "source_filename": "人才材料登记表.xls",
  "target_filename": "人才材料登记表.xlsx"
}
```

WorkBuddy 使用现有 `job_get` 查询该固定任务：

- `PENDING/RUNNING`：说明仍在转换，不能报告 Excel 已生成。
- `FAILED`：展示稳定错误，不得继续 append/export。
- `COMPLETED`：使用原 `idempotency_key` 重试 `structured_dataset_create_from_excel`，取得最终目标绑定。

成功结果至少包含：

```json
{
  "dataset_id": "uuid",
  "working_copy_id": "uuid",
  "document_id": "uuid",
  "document_version_id": "xlsx-version-uuid",
  "source_document_version_id": "xls-version-uuid",
  "filename": "人才材料登记表.xlsx",
  "sheet_name": "登记表",
  "converted_from_legacy": true,
  "conversion_artifact_id": "uuid",
  "version_number": 2,
  "fields": []
}
```

只有取得最终绑定后才能调用 `structured_dataset_append`；只有 append/query/export 均成功后才能报告任务完成。

## 6. 数据与文件版本

推荐保持同一个逻辑文件身份：

```text
Document A
├─ Version 1：人才材料登记表.xls（上传/工作副本来源版本）
├─ Version 2：人才材料登记表.xlsx（格式提升后的初始目标版本）
├─ Version 3：人才材料登记表.xlsx（第一次追加并 export）
└─ Version N：人才材料登记表.xlsx（后续追加）

WorkingCopy A
└─ current_version_id -> Version N

StructuredDataset A
├─ working_copy_id -> WorkingCopy A
└─ target_source_version_id -> Version 1
```

具体规则：

- `Document.original_filename` 保留最初的 `.xls` 名称，作为来源审计信息。
- `WorkingCopy.filename/relative_path/extension` 更新为用户可见的 `.xlsx`。
- 转换后的 `DocumentVersion.parent_version_id` 指向源 `.xls` 版本。
- 转换版本 `source_type` 使用 `STRUCTURED_DATASET_XLS_CONVERSION`。
- `StructuredDataset.target_source_version_id` 指向源 `.xls`，`working_copy_id` 指向同一工作副本。
- 如果源 `.xls` 版本原本引用用户可见工作副本路径，先原子复制到内部历史路径并校验哈希，再切换当前版本；
  历史路径不出现在普通文件列表。
- `.xlsx` 内部版本继续使用
  `structured-datasets/{dataset_id}/.versions/{version_number}-{export_id}.xlsx`。
- 用户可见目录始终只有当前 `.xlsx`；不能同时留下一个普通 `.xls` 和一个普通 `.xlsx`。

该设计不需要新增数据库列；现有 `DocumentArtifact`、`DocumentVersion.parent_version_id`、
`StructuredDataset.target_source_version_id` 和 `StructuredDataset.working_copy_id` 已能表达完整关系。若实现中发现
旧版本物理路径无法安全迁移，才增加专用版本存储元数据，不应先复制第二个普通 WorkingCopy。

## 7. 原子性与幂等

格式提升必须按以下顺序执行：

1. 锁定 Dataset 创建键、Document、源 DocumentVersion 和 WorkingCopy。
2. 验证源版本仍是当前版本，源文件哈希与数据库一致。
3. 在隔离目录转换；校验 OOXML、openpyxl、大小、SHA-256 和工作表结构。
4. 把源 `.xls` 和目标 `.xlsx` 写入各自的内部不可变路径，暂不改变用户可见文件。
5. 检查目标 `.xlsx` 文件名在工作区和数据集范围内没有冲突。
6. 创建/更新 DocumentVersion、Dataset、Export revision 0、FileObject、ManagedFile、WorkingCopy 和审计记录。
7. 原子发布 `.xlsx` 当前文件；确认新当前文件存在且哈希正确后，移除旧的用户可见 `.xls` 副本。
8. 提交数据库事务。中断重试必须根据幂等键、版本 ID 和哈希收敛，不能产生第二个版本或第二个工作副本。

文件系统提交和数据库事务无法形成单一 ACID 事务，因此必须提供补偿：

- 数据库未提交但 `.xlsx` 已发布：重试识别同哈希暂存/当前文件并完成同一记录。
- 数据库已提交但当前文件缺失：再次调用注册或 export 从内部不可变版本重新发布。
- 转换失败或 schema 不一致：保持原 `.xls` 当前文件和所有数据库绑定不变。
- 旧 `.xls` 只有在新 `.xlsx` 已经发布并完成哈希校验后才从用户可见位置移除。

## 8. Schema 与内容校验

转换结果必须满足现有累计表格约束：

- 工作表选择与 WorkBuddy 预览一致；多个非空工作表且用户未选择时返回歧义。
- 标题行非空、无重复字段、字段数不超过 20。
- 已有数据只保留当前累计表格支持的标量值；公式不执行为指令，不保留宏。
- WorkBuddy 返回的 sheet name、标题顺序和使用区必须与转换后的 `.xlsx` 一致。
- 如果 LibreOffice 改变了工作表、标题或关键单元格结构，返回
  `TARGET_EXCEL_CONVERSION_SCHEMA_MISMATCH`，不得注册目标。
- 加密、密码保护、损坏、宏风险无法安全消除或外链结构不明确时停止，不尝试破解或静默删除关键内容。

累计表格当前本身只维护标量标题和数据行，不保证保留原 `.xls` 的宏、公式表达式、图表、数据透视表、按钮、
复杂样式或外部链接。预览时发现这些特性必须明确提示；无法确认转换安全时要求用户先提供 `.xlsx` 模板。

## 9. 错误码

| 错误码 | 行为 |
| --- | --- |
| `TARGET_EXCEL_LEGACY_POLICY_REQUIRED` | `.xls` 未显式授权转换，停止 |
| `TARGET_EXCEL_CONVERSION_PENDING` | 返回固定 job ID，不报告完成 |
| `LIBREOFFICE_NOT_AVAILABLE` | 要求服务器安装/配置 LibreOffice 或用户另传 `.xlsx` |
| `XLS_CONVERSION_TIMEOUT` | 任务失败，可用原幂等键重试，不改动当前文件 |
| `XLS_CONVERSION_OUTPUT_INVALID` | 转换产物不是有效 `.xlsx`，停止 |
| `TARGET_EXCEL_CONVERSION_SCHEMA_MISMATCH` | WorkBuddy 预览与转换结果不一致，停止 |
| `TARGET_EXCEL_FILENAME_CONFLICT` | `.xlsx` 目标同名冲突，要求用户处理 |
| `TARGET_EXCEL_SHEET_AMBIGUOUS` | 多个业务工作表，要求用户选择 |
| `TARGET_EXCEL_VERSION_CONFLICT` | 转换期间源版本变化，重新读取、预览和确认 |
| `TARGET_EXCEL_FEATURE_UNSUPPORTED` | 宏、加密或复杂结构不适合作为累计目标 |

任何失败都不能回退为：模型重建表格、内部 OCR、`xlrd` 直读、Shell 临时转换、自动改名或创建第二个目标文件。

## 10. 代码改造范围

### 服务端

- `apps/api/app/modules/structured_datasets/schemas.py`
  - 增加 `extraction_run_id` 和 `legacy_target_policy`。
- `apps/api/app/modules/structured_datasets/service.py`
  - `create_from_excel` 支持 `.xls/.xlsx` 分支；新增转换结果校验、格式提升、文件名冲突和版本发布逻辑。
- `apps/api/app/modules/structured_datasets/runtime.py`
  - 正确记录异步接受、等待、完成和失败状态；不能把 `PREPARING_TARGET` 写成业务完成。
- `apps/api/app/modules/file_lifecycle/storage.py`
  - 增加受控旧目标源版本路径、当前文件扩展名切换和幂等补偿接口。
- `apps/api/app/modules/managed_files/worker.py`
  - 增加 `PREPARE_STRUCTURED_DATASET_TARGET` job handler，调用既有 `OfficeDerivativeService`。
- `apps/api/app/modules/files/office_conversion.py`
  - 原则上只复用；如需增加面向目标提升的公开适配方法，不改变现有解析行为。

### MCP

- `apps/mcp/file_agent_mcp/dataset_tools.py`
  - 扩展 `structured_dataset_create_from_excel` 输入和说明，透传异步 job/result。
- `apps/mcp/tests/test_dataset_tools.py`
  - 覆盖 `.xls` 参数、202 job、完成重试和 JSON 序列化。

### WorkBuddy Skill

- `integrations/workbuddy/file-agent-plugin/skills/file-agent-structured-dataset/SKILL.md`
  - 把“唯一新上传 `.xlsx`”扩展为“唯一新上传 `.xls/.xlsx`”；明确预览转换、job_get、最终 `.xlsx` 文件名和
    export 完成条件。
- 插件 README、版本和市场包同步更新。

## 11. 自动测试

### 11.1 API/服务层

1. 简单 `.xls` 成功转换并注册；工作表、标题、已有行与 WorkBuddy extraction 一致。
2. 转换后 Document ID、WorkingCopy ID 不变，DocumentVersion 从 `.xls` 父版本增加 `.xlsx` 子版本。
3. 文件列表只存在一个 `.xlsx` 当前文件；旧 `.xls` 只作为历史来源版本存在。
4. 文件列表名、当前物理 basename、下载名和返回 filename 完全一致。
5. 同一幂等键重复调用不重复运行 LibreOffice、不新增 Dataset/Version/WorkingCopy。
6. append/export 后仍使用同一 Document/WorkingCopy，并新增后续 `.xlsx` 版本。
7. LibreOffice 缺失、超时、损坏输出、schema 不一致、版本冲突和同名冲突均无可见副作用。
8. 多工作表、重复标题、空标题、加密和复杂特性分别得到稳定失败。
9. 当前 `.xlsx` 丢失时，幂等注册或 export 能从内部版本重新发布。
10. 普通 `.xls` 来源文件而非目标时不触发转换提升。

### 11.2 MCP/Skill

1. `.xls` 创建目标返回 `PREPARING_TARGET + filesystem_job_id`，不能返回伪完成。
2. job 完成后原幂等键重试返回最终 `.xlsx` 稳定绑定。
3. job 失败后不调用 append/query/export。
4. 复核预览明确显示源 `.xls` 和最终 `.xlsx` 名称。
5. `DIRECT_SAVE` 可跳过内容确认，但最终仍必须报告转换和 export 结果。
6. UUID、派生件 ID 和嵌套结果全部 JSON 可序列化。

## 12. 真实 WorkBuddy 验收

准备一个无敏感信息的 `.xls` 模板、两个 PDF/图片来源材料：

1. 上传 `.xls` 和来源材料，要求先预览确认；核对 WorkBuddy 展示真实 sheet、字段、已有行及最终 `.xlsx` 名称。
2. 确认后观察转换 job，完成后核对 append/query/export 均发生。
3. 在服务器文件列表确认只有 `.xlsx` 当前文件，下载后核对标题、已有行和新增行。
4. 用新的 `.xlsx` 文件名继续追加第二批材料，核对字段无需重新描述、WorkingCopy/Document ID 不变、版本递增。
5. 测试 `DIRECT_SAVE` 路径，确认无 preview/confirm，但转换失败时仍停止。
6. 制造同名 `.xlsx`、多工作表、LibreOffice 不可用和源版本变化，核对不会自动选择、改名或写错文件。

## 13. 实施顺序与完成标准

推荐分三步实现：

1. 服务端格式提升、异步 job、单一工作副本和版本审计。
2. MCP 参数/结果与 WorkBuddy Skill 编排。
3. 自动测试、真实 WorkBuddy 验收、插件打包发布。

只有以下条件全部成立才算解决：

- `.xls` 可被选为新目标，不再得到 `.xlsx only` 阻塞；
- 内容仍由 WorkBuddy 真实读取，File Agent 只做受控格式转换与持久化；
- 最终目标固定为 `.xlsx`，用户清楚知道实际文件名；
- 服务器文件列表只显示一个当前 `.xlsx`；
- 原 `.xls` 字节和来源版本可追溯；
- 后续按 `.xlsx` 文件名持续追加，稳定 ID 不变且版本递增；
- 任何转换、冲突或校验失败均无错误写入，也不被误报为完成。

## 14. 2026-09-22 导出保真与旧数据修复补充

本轮在不改变 MCP 入参、Dataset/Document/WorkingCopy 稳定 ID 和异步转换契约的前提下完成以下闭环：

1. 上传目标和系统新建目标的唯一普通文件都发布到
   `WORKING_COPY_STORAGE_ROOT/structured-datasets/{dataset_id}/{filename}`；旧入口只在数据库提交及最终文件哈希
   校验成功后移除。
2. 修正“迁移后又生成新版本”场景的清理校验：旧入口使用 parent version 哈希，新入口使用本轮最终导出哈希，
   防止错误保留第二份普通文件。
3. 已上传 XLSX 的追加不再通过 openpyxl 重存整个工作簿，而是直接修改 OOXML 包中的目标 worksheet；关联表格和
   autoFilter 仅扩展行范围，其余 ZIP part 原字节复制。
4. 新 DocumentVersion 写入 `structured_dataset_export_mode=package-append-v3`。缺少该标记或仍为旧
   `template-append-v2` 的历史导出在同一 revision 再次导出时，从 revision 0 原始模板和全部已确认成员重建，
   不重复追加，并保留旧错误版本供审计。
5. 自动测试覆盖普通连续追加、幂等重发、旧目录迁移、旧扁平导出自修复、样式/列宽/冻结窗格、多工作表、公式、
   合并区域、数据校验、Excel Table、autoFilter、图表和 drawing 保留。
6. 导出前由服务端校验 revision 与成员链连续性；成员缺失或没有任何已确认记录时关闭式失败，防止客户端绕过
   Skill 的 query 检查后得到只有标题行的“成功”文件。
