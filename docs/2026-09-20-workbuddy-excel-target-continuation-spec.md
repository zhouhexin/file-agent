# WorkBuddy Excel 目标表与按文件名持续追加开发规范

> 2026-09-28 修订：本文件中“显式提取成表默认人工确认”的旧策略由
> [Excel 直写与目标选择修订](2026-09-28-workbuddy-excel-direct-save-and-target-selection.md) 取代；
> 新上传目标必须新建独立文件，只有本轮无新目标上传且按文件名指定已有表时才续写。

日期：2026-09-20

状态：仓库实现与自动测试完成；真实 WorkBuddy 验收待执行。本文是本轮开发和验收的直接依据。

## 1. 目标

用户通过 WorkBuddy 从图片、Word、PDF 等来源文件提取结构化信息时，可以上传一个 Excel 作为新的目标表，
也可以不上传 Excel、仅通过提示词定义字段并创建目标表。提取结果必须先向用户展示并取得明确确认，之后才
允许写入服务器中的目标 Excel。

目标 Excel 是服务器文件列表中的普通文件，也是可持续修改的唯一当前工作副本。用户后续只需按文件名指定
这个 Excel，系统就读取其当前工作表和标题字段，从新的来源文件中提取相同字段并追加数据。历史内容通过
`Document + DocumentVersion` 保存，不通过创建多个带版本后缀的可见 Excel 文件实现。

本文与以下既有规范共同生效：

- `docs/2026-09-20-workbuddy-explicit-extraction-confirmation.md`
- `docs/2026-09-20-structured-dataset-working-copy-publication.md`
- `docs/2026-09-20-workbuddy-document-to-excel-development-plan.md`

冲突时，本轮用户明确要求和本文中的目标表选择、文件名及版本规则优先。

## 2. 必须实现的用户行为

### 2.1 本轮上传 Excel

1. 本轮附件中存在一个新的 Excel 时，该 Excel 优先成为新的目标表。
2. 系统读取其工作表、标题行和字段结构，以这些字段作为本轮来源文件的提取范围。
3. Excel 本身不得作为待抽取业务记录的普通来源文件重复处理。
4. 本轮即使同时提到一个历史 Excel 文件名，也不得静默写入历史表；新上传 Excel 优先。
5. 新上传 Excel 与已有数据集独立绑定，后续使用它的文件名继续追加。
6. 第一阶段支持 `.xlsx`；旧 `.xls` 必须先走项目既有的受控持久化转换链路，不能由 LLM 或 MCP 直接改写。

### 2.2 本轮没有上传 Excel

1. 如果提示词指定了历史 Excel 文件名，系统按文件名解析唯一目标表，并读取其当前字段结构。
2. 如果提示词没有指定历史 Excel，则根据提示词生成字段 schema 并创建新的目标表。
3. 用户明确给出新文件名时使用该文件名；未指定时系统生成可读的 `.xlsx` 文件名，并在回执中明确告知用户。
4. 文件名必须经过受控规范化，最终扩展名固定为 `.xlsx`；不得使用 UUID 或临时名称作为用户可见文件名。

### 2.3 保存前确认

1. 明确提取正文、字段或表格内容的请求必须使用 `CONFIRM_BEFORE_SAVE`。
2. WorkBuddy 必须展示本轮实际提取值、来源文件、字段证据、缺失项和警告。
3. 用户确认前不得提交正文、追加数据集或导出 Excel。
4. 用户修正字段或值后必须生成新的预览指纹并重新确认。
5. 纯归档内部解析继续使用 `DIRECT_ARCHIVE`，不得与本流程混用。

### 2.4 按文件名持续追加

用户可以发送：

```text
将这批材料的信息追加到“2026年人才材料登记表.xlsx”
```

系统必须：

1. 从用户提示词中取得完整目标文件名，按当前共享工作目录中的活动 Excel 工作副本精确匹配。
2. 文件名唯一命中时，解析其稳定 `Document ID`、当前 `DocumentVersion ID`、`WorkingCopy ID` 和
   `StructuredDataset ID`。
3. 读取该当前版本真实 Excel 的工作表、标题行和字段结构，不要求用户再次重复字段清单。
4. 使用读取到的字段提取本轮新来源文件内容，确认后追加到同一数据集。
5. 追加和导出必须创建同一 Document 的新版本，并更新同一 WorkingCopy 的当前版本。
6. 文件名不存在时明确要求用户重新指定；多个活动文件同名时返回候选并要求用户选择，禁止按时间、ID、路径
   或相似度自行挑选。

文件名是用户入口，不是内部主键。执行阶段始终使用解析后冻结的稳定 ID 和期望当前版本，防止确认期间文件
被改名或更新后写错对象。

## 3. 文件名、物理路径与版本规则

1. 服务器当前工作副本的实际文件名必须与文件列表展示名、下载名和用户后续引用名完全一致。
2. 例如展示名为 `2026年人才材料登记表.xlsx`，当前物理工作副本也必须使用该名称；不得使用 UUID、
   `dataset.xlsx`、临时名或自动版本后缀代替。
3. 系统新建和本轮上传的目标表都固定发布到受控
   `WORKING_COPY_STORAGE_ROOT/structured-datasets/{dataset_id}/{用户可见文件名}.xlsx`。上传文件注册为目标表时，
   复用原 Document、WorkingCopy 和稳定 ID，把唯一普通文件入口迁移到该目录；数据库提交成功后移除旧位置，
   不复制出第二个普通文件。两类目标的内部不可变历史版本都位于
   `structured-datasets/{dataset_id}/.versions`，但不得作为普通文件显示。
4. 首次发布创建一个 Document、一个 WorkingCopy 和一个当前 DocumentVersion。后续导出只新增
   DocumentVersion，并更新原 WorkingCopy 和当前 FileObject。
5. 用户修改目标文件名时，必须复用受控工作副本重命名、冲突检查、OperationPlan 和审计链路；展示名、下载名
   和当前工作副本物理名称同步变化。
6. 本轮上传新 Excel 与唯一已发布的活动目标同名时，以新上传表建立独立数据集；新表发布成功后归档旧目标，
   按文件名只匹配新表。旧表版本和导出继续保留，不合并旧表数据；发布失败不归档旧表。其他同名歧义必须
   停止并让用户决定。不得静默生成 `(2)`、时间戳或 UUID 后缀，也不得覆盖旧表历史。
7. 内部继续使用稳定 `dataset_id + document_id + working_copy_id + current_version_id` 关联；文件名变化不得
   创建新的数据集或丢失版本历史。
8. 已上传 Excel 的后续导出必须以当前 DocumentVersion 的真实工作簿为模板，只向选定工作表追加当前 revision
   新增且已确认的数据行；不得根据数据库标量重新创建工作簿，也不得通过通用工作簿库重新保存整个 XLSX。
   实现必须在 OOXML ZIP 包级别只修改目标 worksheet 和确需扩展的 table/autoFilter 范围，其余 part 原字节复制，
   以保留未选中工作表、标题位置、列宽、行高、冻结窗格、公式、合并单元格、数据校验、图表、drawing、打印设置
   及扩展对象。同一 revision 重试不得重复追加已有行。
9. 升级前由旧 writer 生成的扁平导出必须通过版本策略标记识别；再次导出时从 revision 0 原始模板重建，合并所有
   已确认追加记录，并在同一 export_id 下发布新的 DocumentVersion。旧错误版本只作为内部历史保留，不再作为当前
   普通文件或下载内容。
10. 目标从旧工作副本目录迁移并在同一请求中生成新内容时，旧入口清理必须分别校验“旧入口的 parent version
    哈希”和“新入口的最终导出哈希”。不得用迁移前哈希校验已经更新后的当前文件；提交后只允许存在一个普通
    可见文件。
11. `dataset.revision` 必须与 `StructuredDatasetMember.appended_revision=1..revision` 连续一致；成员链缺失时返回
    `DATASET_REVISION_INCONSISTENT`，不得继续生成或复用导出。revision 大于 0 且没有任何已确认记录时返回
    `DATASET_EXPORT_NO_CONFIRMED_RECORDS`，不得发布只有标题行的 Excel。
12. `target_filename` 字段引入前已经发布的累计表，数据库迁移应优先从其稳定 `WorkingCopy.filename` 回填；
    运行时再次迁移或发布时也必须自修复该字段。不得只依赖读取时回退而长期保留空的内部文件名绑定。

## 4. Excel schema 读取规则

### 4.1 上传 Excel

- 只读取受控附件提交中明确上传的 Excel。
- 至少返回工作表名、标题行位置、按顺序排列的字段名和现有数据行数。
- 默认使用唯一非空工作表；存在多个有业务内容的工作表且用户未指定时，必须要求用户选择。
- 标题为空、重复、公式错误、合并单元格导致字段不确定或文件损坏时，不得猜测 schema。
- 读取公式单元格时不得执行宏、外链或任意代码。

### 4.2 已有目标 Excel

- 必须读取当前 DocumentVersion 对应的真实 `.xlsx` 内容，不能只相信旧请求中的字段或模型记忆。
- 当前 Excel 的字段与数据集 schema 不一致时停止追加，并返回结构化冲突；不得静默增删列。
- 读取成功后将 `target_document_id`、`target_document_version_id`、`target_working_copy_id`、
  `target_dataset_id`、`sheet_name` 和字段 schema 固定到本次受控调用参数。

## 5. 服务端与 MCP 边界

### 5.1 服务端

服务端负责：

- 按精确文件名解析唯一活动 Excel 工作副本；
- 校验用户可见范围、扩展名、当前版本及数据集绑定；
- 从受控当前版本读取 Excel schema；
- 把上传 Excel 注册为新的目标表与数据集；
- 追加结构化记录并创建新文件版本；
- 维持工作副本实际文件名与用户可见文件名一致；
- 写入 ToolInvocation、ChangeSet、版本关系和下载回执。

LLM 和 WorkBuddy Skill 不得直接访问服务器文件系统或数据库，也不得只凭文件名猜测稳定 ID。

### 5.2 MCP Tool

MCP 至少需要提供等价的受控能力：

- 由本轮上传 Excel 建立/解析新目标表；
- 按完整文件名解析已有目标表并返回字段 schema；
- 新建提示词 schema 对应的数据集并显式指定最终文件名；
- 在 append/export 时携带已解析的稳定目标 ID 与期望版本；
- 返回用户可见文件名、当前版本和下载信息。

所有 UUID、证据引用和嵌套返回值在 MCP 边界必须使用 JSON 可序列化字符串。

### 5.3 WorkBuddy Skill

Skill 的确定性优先级：

```text
本轮上传的新 Excel
-> 提示词明确指定的已有 Excel 文件名
-> 提示词定义字段并创建新 Excel
```

Skill 必须在预览结果展示后停止当前轮，等待确认；确认后才按已冻结目标继续 append/export。最终回复必须包含
实际文件名、是否新建或追加、追加行数、当前版本和下载方式。

## 6. 错误与冲突

至少覆盖以下结构化结果：

| 场景 | 行为 |
| --- | --- |
| 文件名未命中 | 返回 `TARGET_EXCEL_NOT_FOUND`，要求重新指定 |
| 多个同名活动文件 | 返回 `TARGET_EXCEL_AMBIGUOUS` 和候选，不自动选择 |
| 非 Excel 文件 | 返回 `TARGET_EXCEL_TYPE_UNSUPPORTED` |
| 多工作表歧义 | 返回 `TARGET_EXCEL_SHEET_AMBIGUOUS` |
| 标题无效或重复 | 返回 `TARGET_EXCEL_SCHEMA_INVALID` |
| Excel 与数据集 schema 不一致 | 返回 `TARGET_EXCEL_SCHEMA_CONFLICT` |
| 当前版本已变化 | 返回 `TARGET_EXCEL_VERSION_CONFLICT`，重新解析和预览 |
| 已发布目标未携带稳定 ID | 返回 `TARGET_EXCEL_BINDING_REQUIRED`，先按文件名重新解析 |
| 同名新文件冲突 | 返回 `TARGET_EXCEL_FILENAME_CONFLICT`，等待用户决定 |
| 文件名含路径或非法字符 | 返回 `TARGET_EXCEL_FILENAME_INVALID`，禁止静默清理后继续 |
| 未确认即写入 | 沿用 `EXTRACTION_CONFIRMATION_REQUIRED` |

## 7. 自动测试验收矩阵

### 7.1 API/服务层

- 上传一个 `.xlsx` 和多个来源文件：上传 Excel 被选为新目标，字段顺序来自真实标题行。
- 不上传 Excel但提示词给字段：创建指定文件名；未给文件名时生成并返回可读文件名。
- 按唯一文件名解析已有目标：返回稳定 ID、当前版本、工作表和字段。
- 文件名不存在、重复命中、非 Excel、多工作表歧义和无效标题分别失败且无写入。
- append 前后当前 WorkingCopy ID 与 Document ID 不变，DocumentVersion 增加。
- 当前工作副本物理 basename、文件列表 filename 和下载 filename 完全一致。
- 同名冲突不生成后缀、不覆盖、不创建第二个数据集。
- 目标版本在预览后变化时拒绝追加。
- 旧 structured dataset 和显式确认测试继续通过。

### 7.2 MCP

- 新工具/参数序列化稳定，嵌套 UUID 不泄漏为 Python UUID 对象。
- 上传 Excel、新建 Excel、按文件名续写三条调用路径都返回实际目标文件名和稳定 ID。
- append/export 始终绑定解析后的目标及期望版本。
- 未确认时 Skill 不调用 append/export。

### 7.3 WorkBuddy Skill 静态验收

- 明确记录三层目标优先级。
- 明确要求展示字段和值并等待后续确认。
- 明确要求最终回复给出文件名。
- 明确禁止 UUID/临时名称作为可见当前工作副本文件名。
- 明确文件名歧义必须询问，不能自行选择。

## 8. 真实功能测试

代码自动测试通过后，在真实 WorkBuddy 中按顺序执行：

1. 上传一个带标题的模板 Excel 和两份来源材料，核对字段预览；确认后下载并核对两行数据。
2. 不上传 Excel，提示字段和文件名，确认后核对新文件名、文件列表和下载名一致。
3. 新一轮只上传来源材料，通过上一步文件名继续追加，核对字段自动从当前 Excel 读取且版本增加。
4. 同时上传新 Excel 并提及旧文件名，验证新 Excel 优先且旧文件不变化。
5. 制造同名文件、改名和预览后版本变化，分别验证歧义、冲突和重新确认。

真实测试记录不得包含附件正文、身份证号、Token、API key 或服务器绝对路径；只记录无隐私样本、稳定对象
ID、行数、版本号、文件名和结果状态。

## 9. 完成标准

只有以下条件全部满足才算完成：

- 本文定义的 API/服务、MCP 和 Skill 行为已经实现；
- 目标选择优先级、保存前确认和按文件名续写均有自动测试；
- 当前物理工作副本名称与展示/下载名称一致；
- 单一工作副本持续产生 DocumentVersion，文件列表不增加重复文件；
- 相关 API、MCP 回归测试和 Skill 校验通过；
- 真实 WorkBuddy 测试步骤和仍需人工完成的部分被如实记录。
