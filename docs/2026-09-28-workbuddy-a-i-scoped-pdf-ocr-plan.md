# WorkBuddy 只填 A–I 列的定向 PDF OCR 方案

日期：2026-09-28

状态：已实现 WorkBuddy 0.1.50 Skill、可信附件拆分、定向 PDF 区域 OCR API/MCP、B–I 证据验收及 A–I 限列 Excel 回传；首次上传表取代唯一同名已发布旧目标，后续可只上传一份 PDF 并按完整 `.xlsx` 文件名向同一目标追加。B–I 八值行绕过宿主 `null` 占位传输。自动 API/MCP 回归通过；真实 WorkBuddy 宿主 OCR 效果需安装本版套件验收。服务端 PDF 完成条件只覆盖 B–I 八列。

## 2026-09-29 后续 PDF 单件追加

首次继续使用“一份 PDF + 一份 XLS/XLSX”的可信提交单。目标发布后，后续本轮任务须明确写出完整 `.xlsx` 文件名并只附加一份 PDF；`scoped_excel_pdf_append_ingest` 先经现有鉴权接口唯一解析活动、已发布且具有真实布局的目标，再只把 PDF 导入 `SCOPED_EXCEL_SOURCE`。目标零命中、歧义、未发布或任务文字未包含该全名时不导入来源。后续 `excel_writeback_checkout` 只传文件名与本轮请求键，冻结当前版本、revision、schema 和布局；同一来源版本不得重复追加。

PDF 仍只裁剪第 1、3 页 B–I 所需区域，得到新来源的八列正式证据。`excel_writeback_apply.rows` 每人只传 B–I 八值；A 若有模板序号则保留，否则从上一数值序号生成，J–AA 均不写值。已预留数据行先使用；数据区满时，MCP 在说明文字之前插入一行并移动其合并区域，服务器独立重建预期行移动并检查完整工作簿差异。发布成功后原目标工作副本创建新版本，数据区下界同步扩展；下一次必须重新 checkout 当前版本和布局。包含无法安全移动的公式、表格对象或区域引用时明确失败，不冒险改写。无需数据库迁移，沿用现有数据集布局 JSON、DocumentVersion、成员与发布审计。

自动验收覆盖三份不同 PDF 依次发布到同一个 `.xlsx`、序号 1/2/3、数据区扩展、说明文字与合并区域下移、J–AA 空白、重复来源拒绝以及未授权目标拒绝。真实 WorkBuddy 验收还需核对宿主提交单只含本轮 PDF、实际 OCR 只对两张冻结 PNG、发布回执 `excel_published=true` 和最终下载文件的新版本。

## 1. 目标和样本结论

用户明确要求对 `申报人员业绩汇总-教学科研型教授.xls` 只填写物理 A–I 列，并从 `2026职称评审表-教授-石程.pdf` 只识别与这些列有关的内容。此任务不读取、分析、判定或填写 J–AA 列，也不为这些列寻找印章、业绩或资格依据。

已检查的 XLS 是 A–AA 共 27 列的空白模板，数据区从第 5 行开始，A 列有预置序号。PDF 为 25 页扫描件，不能直接提取文本。该样本的 B–I 证据集中在 PDF 物理第 1 页封面和第 3 页“基本情况”：

| 物理列 | 目标信息 | 本样本候选来源 | 处理方式 |
| --- | --- | --- | --- |
| A | 序号 | Excel 原有序号/行规则 | 保留或按目标表规则确定，不做 PDF OCR |
| B–C | 学院、姓名 | PDF 第 1 页、第 3 页 | 从定向 OCR 的真实原文取值 |
| D–I | 出生年月、工作时间、职称及取得时间、学历及取得时间 | PDF 第 3 页 | 从定向 OCR 的真实原文取值 |

上表页号只适用于这份已核对样本及经验证的同版式文件，不可从文件名推断其他 PDF 的页码。OCR 负责读出具体值；服务端只校验来源、位置、字段映射和证据，不预设任何争议字段的取值。

服务端的 PDF 提取完成条件是 **B、C、D、E、F、G、H、I 八个物理列全部取得可验证的来源结果**。A 列由目标 Excel 的序号规则处理；J–AA 不在本次 PDF 提取范围。OCR 输入从 25 页压到上述页面中的相关区域。按页面数量，本样本最多只需处理 2/25 页；实际耗时和费用还包含上传、目标 Excel 读取、`.xls` 格式提升、审计与发布，必须以实测确认，不能把页面比例直接说成总耗时比例。

## 2. 当前阻碍

1. `workbuddy-document-v1` 的 PDF `submit` 在 `document_service.py::_validate_unit_scope` 中要求页号覆盖 `1..page_count`。只提交第 1、3 页会得到 `EXTRACTION_PAGE_SCOPE_MISMATCH`；`PARTIAL` 又不能推进现有归档。因此，单改 Skill 或提示词不能真正减少 OCR 页数。
2. 当前普通上传的 `WORKBUDDY_DOCUMENT` 任务把 `COMPLETE` 解释为全文完整，可供后续归档、检索和分类复用。第 1、3 页的内容不能伪装成全文 `COMPLETE`，也不能把未读页面填为 `BLANK`。
3. 现有结构化表格 Skill 要求读取目标 Excel 的完整真实网格，并对完整 `labels` 构造逐列候选。需要把“模板 schema 的完整性”和“本次 PDF 必须覆盖的字段范围”分开：完整读取 A–AA 表头以保护模板，服务端只要求 B–I 的 PDF 证据，最终只允许 A–I 数据格变化。
4. 新上传 `.xls` 仍须经既有受控 LibreOffice 路线提升为 `.xlsx`，并保留原件、版本、布局和原有单元格。缩小 PDF OCR 范围不会自动消除这段固定耗时。

## 3. 冻结的任务范围

用户可见的显式任务为 `SCOPED_EXCEL_FILL`；实现上由 PDF 批次配置 `SCOPED_EXCEL_SOURCE` 和 Excel 回传载荷的 `write_scope=A:I` 共同承载。仅在用户明确指定目标列范围和两份来源材料时启用。它不能由普通上传、自动归档或全文问答隐式触发。

服务端分两次冻结：PDF 领取前生成不可变 `scope_digest`，Excel checkout 冻结目标版本并由发布载荷把两者绑定：

- 可信提交单固定本轮一份 PDF 和一份 Excel；PDF `scope_digest` 包含其 `document_version_id`、SHA-256、25 页数量、区域矩形及 B–I 范围。Excel 的稳定 ID、当前版本、SHA-256、sheet、布局及完整标题在 checkout 的任务快照中单独冻结；发布时同时校验来源 ID/运行与目标 CAS。
- `write_columns=[A,B,C,D,E,F,G,H,I]`，`required_pdf_columns=[B,C,D,E,F,G,H,I]`，`excluded_columns=[J..AA]`；列号以目标表真实物理坐标为准，不靠标题相似度推断。服务端的成功判定只枚举 `required_pdf_columns`，不枚举 PDF 全部页或 J–AA。
- `ocr_regions`：本样本为物理第 1 页的封面身份栏、第 3 页的“基本情况”中对应身份、工作、职称和学历的区域。区域配置使用版本化模板键和归一化页面坐标，实施时用真实样本校准并留边距；不得直接照搬未经验证的估计坐标。
- OCR Provider 名称、版本由真实 WorkBuddy OCR 结果回传；服务端限制区域数、文本长度、字段数、租约和幂等键。宿主侧 OCR 重试次数无法由服务端直接观测，本版依赖 Skill 限定只对这两张图像调用 Provider；需在真实宿主验收中记录实际调用次数。

页数先用 PDF 容器元信息校验。模板页号或栏目位置不符时，停止并返回 `SCOPE_LOCATION_UNRESOLVED`；由用户提供正确页号/区域，或者通过单独明确授权创建新的范围快照。不得自动从第 4 页扫到第 25 页寻找字段，也不得静默切换全文 OCR。

## 4. 定向读取与 OCR

### 4.1 PDF 输入范围

新增受控 `scoped_pdf_source_prepare/claim/submit` 等价 MCP/API 能力。`claim` 不把整份 PDF 作为 OCR 输入交给 Provider，而是从固定 SHA-256 的原件只渲染冻结的页和矩形区域，返回带页号、原图尺寸、裁剪坐标、区域 SHA-256 和短期资源引用的图像。服务器只做 PDF 元数据读取、定向渲染和裁剪，不做文本识别；WorkBuddy 调用已安装、已授权的实际 OCR Provider 处理这些区域图像。

宿主侧只对冻结的两个区域执行 OCR。能返回文字位置时保留实际位置；只能返回文字时保留真实页号、区域矩形和字符引文，不伪造单元格坐标。Provider 名称进入任务审计，版本保存在独立提取运行；宿主实际调用次数需在真实 WorkBuddy 测试回执中记录，当前服务端不能独立计数外部 Provider 调用。OCR 输入资源不包含其他 23 页的图像；Skill 禁止对原 PDF 再调用整篇识别工具。

若 B–I 某列的原文跨出裁剪框，本版直接停止并返回定位/证据错误，不自动扩大区域；后续若支持扩大，必须对**同一已授权页面**创建新的受控范围与 `scope_digest`。需要新增页面时先请求用户指定页码，不能靠模型自行扩大到全文。识别失败可在同一图像重试；仍不清楚时不提交猜测值、不发布 Excel，也不触发全文兜底。

### 4.2 结果和证据

新增独立契约 `workbuddy-scoped-pdf-v1`，不修改 `workbuddy-document-v1` 的 `COMPLETE` 语义。成功状态命名为 `SCOPE_COMPLETE`，只表示 **B–I 八列都已从定向 OCR 结果取得可验证的原文和页/区域定位**；不要求处理 PDF 其余页面，也不读取 J–AA 相关内容。结果携带 `required_pdf_columns`、`page_count=25`、实际 `processed_pages`、区域与正式 `field_refs`。未处理页面没有 `DocumentPage`，语义为 `NOT_READ`，不得保存成 `BLANK`、`FAILED` 或全文正文。服务端校验八列覆盖集合、区域图像哈希和逐列引文；不运行 `actual_pages == 1..page_count` 检查。缺列或 OCR 失败返回 422，任务不进入 `SCOPE_COMPLETE`，Excel 无法发布。

正式 OCR 结果保存为与源 `DocumentVersion`、`scope_digest` 和目标列范围关联的定向提取运行。B–I 每列必须各有一条 `RESOLVED` 字段结果，包含真实来源页、区域、原文引文或字符范围、OCR Provider 信息；同一原文可支持多列，但每列均须明确映射。服务端对引文与保存的区域文本逐字核对，并拒绝缺列、重复列、范围外列和伪造定位。A 列的序号标记为 `TARGET_DERIVED`，不冒充 PDF 证据。任一 B–I 列 `UNRESOLVED` 时返回缺失列清单，不补写猜测值，也不把本次任务报告为完成。

定向提取运行不能被全文检索、证据问答、普通分类、近似内容查重或默认 `document_pages` 最新完整结果查询当作全文。复用缓存仅限相同源版本哈希、页/区域、Provider 版本及 scope；今后用户明确要求全文处理时创建新的完整运行，不把定向结果升级为全文。此模式的 PDF 是任务绑定的不可变来源附件；若普通归档/自动分类仍需完整正文，须走独立的完整提取任务，不能让它成为本次 Excel 保存的前置条件。

## 5. 目标 Excel 与写入范围

2026-09-29 宿主参数兼容修订：WorkBuddy 的工具调用可能把显式 `null` 占位变成字符串等非空值，导致 `excel_writeback_apply` 的 A/J–AA 范围守卫拒绝调用。定向任务的 `rows` 改为每行只提交 B–I 八个展示值，MCP 在验证目标真实列数后补入 Python `None`，再交给原 XLSX 写入器。完整列序和留空原因仍由 `payload.records[].fields` 表达；A 和 J–AA 的空字段省略可选 `raw_text`，仅保留 `blank_reason`。完整行中的真实 JSON `null` 仍兼容，字符串 `"null"` 不作为空位接受。服务端发布前的证据、版本和物理差异校验不变。

仍先真实读取 `.xls` 的工作表、A–AA 多层表头、合并区、预置序号、数据区和已有行；这是目标文件结构校验，不是 PDF 全文 OCR。多个可用工作表、表头错位或无真实 `TABLE_CELL` 时在 OCR 前停止。`.xls` 格式提升、稳定 Document/WorkingCopy ID、版本 CAS、冲突检查和单一可见 `.xlsx` 工作副本沿用现有路线。

给结构化数据集任务增加冻结的 `write_scope`，在现有完整 schema 中区分 `IN_SCOPE_VALUE` 与 `OUT_OF_SCOPE`。本次只接受 A–I 的写入候选，其中 B–I 均须是带 PDF 证据的 `IN_SCOPE_VALUE`；J–AA 标记 `OUT_OF_SCOPE`，不要求其 PDF 来源、不运行审批或资格判断、不生成对勾。`OUT_OF_SCOPE` 不计入“缺失必填字段”，也不能被后续普通字段默认值填上。

服务端对基线和候选工作簿做物理单元格 diff：新行只允许 A–I 改变，J–AA 在已有行保持原字节/值，在新预置行保持模板原值或空白；表头、样式、公式、合并区域及其他工作表按现有发布规则保护。写入前复核实际目标版本和 `scope_digest`；发布后仍生成 DocumentVersion、ToolInvocation、ChangeSet/ChangeItem 和逐列回执。用户要求直接保存时沿用 `DIRECT_SAVE`；明确要求先复核时用 `REVIEW_REQUIRED`。两种模式均不跳过来源证据和文件差异校验。

如果 B–I 任一字段未识别，按字段返回原因并停止本次发布；补读也只允许相应字段的受控区域或经用户指定的新页。不得为了填满 B–I 去 OCR J–AA 的业务页，也不得把未读页面解释为该字段不存在。

## 6. 入口与链路

建议以独立受控入口承载此模式，避免现有 `workbuddy_attachment_ingest` 的自动整理语义在来源 PDF 尚未全文读取时被误用：

```text
明确任务：只填 A–I
→ 固定 PDF 与目标 XLS 附件、哈希和任务范围
→ 先读取并校验目标 XLS 全部真实网格/布局
→ 准备或复用受控 XLS→XLSX 目标版本
→ 服务端只生成 PDF 第 1、3 页指定区域图像
→ WorkBuddy 对这些图像调用实际 OCR Provider
→ 提交 B–I 八列的字段结果和定向区域 OCR 证据
→ 服务端核验 B–I 全覆盖、逐字证据和目标版本，仅写 A–I
→ 发布同名 XLSX 新版本与逐列回执
```

附件接收可复用既有隔离区、源版本、哈希、租约和分片传输能力，但要在触发完整文档提取前冻结 `SCOPED_EXCEL_FILL` 路由。普通 `WORKBUDDY_DOCUMENT` 和既有 25 页完整覆盖规则不变。任务中断后按原 `scope_digest`、租约和幂等键恢复，不重新 OCR 已成功的区域、不重复追加人员行。

## 7. 实施点

1. **契约与存储**：在外部提取模块增加独立 scoped schema、`required_pdf_columns=B..I` 范围快照、逐列覆盖校验、`SCOPE_COMPLETE` 结果和受控区域资源；迁移保存 scope、区域和覆盖元数据。不要把服务对象或 OCR 全文写入 AgentGraphState。
2. **入口与生命周期**：在 WorkBuddy 附件桥接/MCP 增加显式定向任务入口，保证 PDF 不被默认全文提取阻塞；普通归档入口保持现状。来源原件保持不可变，未全文处理的来源状态与搜索索引明确隔离。
3. **目标与验证**：在结构化数据集或 Excel writeback 任务中增加 `write_scope`，让完整模板 schema、B–I PDF 必填覆盖集合与本次 A–I 修改范围同时冻结；服务端强制 J–AA 零改动、B–I 逐字段证据和版本 CAS。
4. **WorkBuddy Skill**：增加“只填 A–I”分支，先检查目标网格，再领取定向区域；只把区域图像交给 OCR Provider，只向模型提供 A–I 的 OCR 结果。不能退回“整份识别一次”路径。若宿主无法对区域图像执行 OCR，则明确报告当前能力不足。
5. **审计与回执**：记录原 PDF 总页数、实际渲染/OCR 页号和区域数、Provider 调用次数、B–I 逐列完成/失败状态、J–AA 未处理、最终版本及实际耗时；不记录文件正文或密钥到普通日志。

## 8. 验收

- 用这两份真实样本，OCR Provider 只收到第 1、3 页的指定区域图像；审计中无其他页的渲染/OCR 调用。服务端只读取 PDF 容器元信息及冻结区域，不执行全文 OCR。
- 服务端仅以 B–I 八列全覆盖判定定向 PDF 提取完成；提交第 1、3 页的允许区域即可通过范围校验，缺任一列或提交白名单外页面均失败。B–I 每值能回到真实 PDF 页和区域文本；A 序号来自目标表规则。J–AA 无字段提取、审批判断、模型输入和数据格变化。
- 最终 XLSX 保持同一目标工作副本、版本链、表头、合并区、样式和尾注；旧 `.xls` 原件未被覆盖。下载文件与当前列表一致。
- 页码/版式不符、OCR 区域读不清、宿主不支持区域 OCR、模板网格缺失、目标版本变化或无法唯一归档的同名冲突均有确定错误；唯一已发布同名旧目标在新表发布成功后归档。任何一种失败都不会暗中转为 25 页全文 OCR。
- 旧 `workbuddy-document-v1` 的完整 PDF 提取、普通上传和全文证据问答回归通过；定向运行不能被选作完整正文。
- 在相同机器、Provider、同一文件版本和缓存状态下分别记录旧链路与新链路的 OCR 页/区域调用、输入输出 token、OCR 费用、p50/p95 总耗时及 Excel 发布耗时。以实测报告节省幅度；若固定的 XLS 转换/发布阶段占主导，再单独优化固定开销，不放宽 OCR 范围。

## 9. 边界

本方案是**针对明确列范围的任务级提取**，不是把 25 页 PDF 宣称为全文已读。未来用户要求 J–AA、全文检索、全文分类或完整证据问答时，需要为相应范围另起受控任务；本次 A–I 保存不能自动触发这些工作。对其他版式的评审表，只有通过页/区域模板校验或得到用户指定位置后才能运行定向 OCR。

## 10. 实施记录与 WorkBuddy 验收命令（2026-09-28）

### 已落地的链路

- `scoped_excel_submission_ingest` 从本轮可信提交单固定恰好一份 PDF 和一份 Excel，拆成两个稳定幂等批次。Excel 沿用现有完整工作表读取、归档及旧 `.xls` 受控提升；PDF 使用独立 `SCOPED_EXCEL_SOURCE`，不经过全文归档解析。源 PDF 仍有不可变上传 `DocumentVersion` 与 SHA-256，并记录任务、租约和审计。
- `workbuddy-scoped-pdf-v1` 与全文 `workbuddy-document-v1` 分离。当前参考模板要求 25 页且物理第 1、3 页为约 576×823 pt。服务器只裁剪第 1 页 `[0.30,0.40,0.75,0.50]` 的单位/姓名区与第 3 页 `[0.12,0.12,0.88,0.56]` 的基本情况区，返回两张 PNG 和哈希；不会把 PDF 文件或其他 23 页作为 OCR 资源返回。OCR 时间接近租约期限时可调用 `scoped_pdf_source_renew`，不重做已成功的 OCR。版式或页数不符返回 `SCOPE_LOCATION_UNRESOLVED`。这个几何校验只能证明当前裁剪规则适用的外形，具体字段内容仍以实际 OCR 引文为准。
- `scoped_pdf_source_submit` 要求两个区域图像哈希与源快照一致，且 B–I 恰好八列均有真实区域 OCR 原文中的唯一引文。结果保存为 `SCOPE_COMPLETE` 的独立提取运行及两条区域页面；旧全文查询只选择 `COMPLETED` 运行，因此不会把它误当完整正文。`scoped_pdf_source_read` 只对来源用户返回这些区域与正式 `source_unit_id`。
- Excel 回传任务增加 `write_scope=A:I` 与 `required_pdf_columns=B:I`。服务器对 B–I 每列核对本次冻结的引文、字符范围和来源区域；A 必须沿用目标序号，J–AA 必须标记 `OUT_OF_SCOPE`。本地写入器及服务端 OOXML/单元格差异校验共同限制新增值；目标版本仍做 CAS，同名工作副本版本、ChangeSet 和发布审计沿用现有链路。
- 当前 A 列仅支持目标数据行已预置序号（样本 A5=1）；目标行 A 为空时明确返回 `TARGET_EXCEL_SEQUENCE_MISSING`，不会留下空序号并宣称完成。新增自动编号规则需单独冻结并测试。旧 `.xls` 转换后即使 A 标题乱码，也按已确认的物理 A 列保留原序号。
- 目标 Excel 的真实网格到 SHEET/TABLE_CELL 归档载荷由 `file-agent-excel-grid-archive` 执行；本 A–I 任务启用 `--require-a-i`。多行单元格保留原文，由真实行列坐标定位；接口契约与集成边界见 [Excel 网格归档 Skill 集成记录](2026-09-29-workbuddy-excel-grid-archive-integration.md)。
- 2026-09-29 实际 WorkBuddy job `34d2c77d-1d8b-44f2-be25-e587b71c78b5` 的 `.xls -> .xlsx` 派生转换已成功，但 checkout 冻结的 `sheet_name`、`layout` 都为空，后续 schema 读取返回 `TARGET_EXCEL_LAYOUT_REQUIRED`。已生成的派生 XLSX 只读核对确认：真实工作表的 `A2:AA4` 表头与 WorkBuddy 网格非空单元格完全一致，`A5:AA6` 数据区可读取、当前无已有记录。服务端新增入队前网格布局校验，worker 对确定性布局错误返回安全错误码；MCP 对 layout 使用严格契约，Skill 明确本轮上传 XLS checkout 必须传真实工作表与布局。该失败 job 不可改参重放，重试须使用新的 `request_key`；原 XLS、当前可见工作副本和旧同名目标均未被该失败任务改写。
- B 列为“学院”时，若 PDF OCR 引文包含字段标签，例如 `单位 计算机科学与工程学院`，保留完整引文及页/区域证据，但 `scoped_pdf_source_submit.fields[B].value`、候选 B 列 `raw_text` 和 `excel_writeback_apply.rows` 的 B 格都必须是 `计算机科学与工程学院`。服务端在 OCR 提交和写回发布两处拒绝把“单位”或前导空格写入 B 列；已完成的 OCR run 可继续使用其真实引文，不凭空修改来源正文。若已有预览冻结了错误 B 值，应新建写回任务再提交正确候选。

### 自动回归

2026-09-28 附件桥接兼容修复：真实 WorkBuddy 会话将 `user-context` 与包含
`user_references`/`attached_files` 的 `additional-data` 放在相邻的两个宿主
`system-reminder` 块中。旧解析器只检查第一块，导致本轮 PDF+XLS 提交单报“未能在固定会话记录中找到本轮已验证附件”。
现在只接受 `user_query` 前、按顺序出现的这两种宿主块，并继续要求附件路径同时存在于 XML 清单与引用列表；
用户正文中的同名标签、无匹配引用和非唯一候选仍不能取得文件读取授权。真实失败会话的只读检查已识别出
恰好一份 `.pdf` 与一份 `.xls`，新增回归测试覆盖定向拆分与两份文件上传。

在项目根目录、现有 Python 环境运行：

```powershell
$env:PYTHONPATH='apps/api'; & 'D:/anaconda/envs/myenv/python.exe' -m pytest -q apps/api/app/tests/test_scoped_pdf_source.py apps/api/app/tests/test_excel_writeback.py apps/api/app/tests/test_document_extraction_api.py
$env:PYTHONPATH='apps/mcp'; & 'D:/anaconda/envs/myenv/python.exe' -m pytest -q apps/mcp/tests/test_scoped_pdf_tools.py apps/mcp/tests/test_excel_writeback_tools.py apps/mcp/tests/test_workbuddy_bridge_probe.py
```

首组使用真实 25 页测试 PDF 经批次上传、领取两张裁剪图、拒绝第 4 页和缺 I 列、保存正式定向来源，再把八列写入 27 列 XLSX 模板并检查 A 序号及 J–AA 空白。另有现有完整文档与写表回归。实际 OCR 字值由 WorkBuddy 宿主 Provider 在真实测试时给出；自动测试只验证契约与发布，不替代宿主 OCR 准确率验收。

另以项目内真实样本的未填 `.xls` 提升版（A2:AA4 表头、A5:AA6 数据区）做了一次只读基线的临时写入烟雾检查：候选文件 A5 保持 `1`、B5:I5 可写、J5:AA5 保持空白；临时文件随后删除，未触及样本原件或当前工作副本。

### 真实 WorkBuddy 测试

1. 部署本次 API/MCP 代码，确认 API 与 MCP 均启用 `WORKBUDDY_STRUCTURED_DATASET_DEMO_ENABLED=true`、`WORKBUDDY_DOCUMENT_EXTRACTION_ENABLED=true` 和 `WORKBUDDY_EXCEL_WRITEBACK_ENABLED=true`，重启 API、文件队列 worker 与 WorkBuddy。安装 `integrations/workbuddy/file-agent-marketplace-0.1.50.zip` 并新建会话。MCP 必须列出 `scoped_excel_submission_ingest`、`scoped_excel_pdf_append_ingest`、`scoped_pdf_source_claim/submit/read`、`excel_writeback_checkout/apply/publish`。
2. 在同一条 WorkBuddy 消息附加原始 `申报人员业绩汇总-教学科研型教授.xls` 和 `2026职称评审表-教授-石程.pdf`，发送：

   > 请用文件助手按 `file-agent-a-i-scoped-excel` 处理这两份附件。只填写汇总表的物理 A–I 列：A 保留模板原有序号，PDF OCR 只读取 B–I 所需的第 1、3 页定向区域，不读取其他页面或 J–AA 相关内容。`excel_writeback_apply.rows` 每行只传 B–I 八个值，不传空占位。核对八列原文证据后直接保存到同名 Excel 工作副本；如遇重复上传，继续使用本次上传附件。完成后给出逐列值、来源页/区域、实际 OCR 页数、最终文件版本和下载链接。

3. 检查工具回执中的 `processed_pages=[1,3]`、`region_count=2`、`resolved_columns=[B,C,D,E,F,G,H,I]`、`status=SCOPE_COMPLETE` 和 `excel_published=true`。下载最终 XLSX，核对 A5 原序号、B5:I5 八值及 J5:AA5 与基线一致。记录 WorkBuddy 实际 OCR Provider、调用次数、耗时与错误；若 B–I 任一值不清楚或源布局不同，应停止并报告列名，不得扩到第 4–25 页。
4. 新建后续会话，只上传另一位人员的 PDF，并发送：

   > 请用文件助手把这份 PDF 的 B–I 信息追加到 `申报人员业绩汇总-教学科研型教授.xlsx`。只 OCR 第 1、3 页的冻结区域，A 列按上一序号生成，J–AA 留空；直接保存并给出新版本和下载链接。

   检查调用 `scoped_excel_pdf_append_ingest` 且本轮未上传 Excel，最终仍是同一 `dataset_id` / 工作副本且 revision、DocumentVersion 各递增一次。第二人应在 A6:I6；再用第三份不同 PDF 重复时，说明行及其合并区域应从第 7 行移到第 8 行，第三人进入 A7:I7。不能重用同一 PDF 来源版本冒充新人员。

## 11. 多份 PDF 与 Word 同轮追加（2026-09-29）

WorkBuddy 新入口 `scoped_excel_sources_ingest` 接受本轮 1–20 份 `.pdf`、`.doc`、`.docx`。首次另需恰好一份上传 `.xls/.xlsx`；续写不得附 Excel，任务文字须写明唯一已发布目标的完整 `.xlsx` 文件名。工具从可信附件清单冻结 `source_order`，将 PDF 批次导入 `SCOPED_EXCEL_SOURCE`、Word 批次导入 `WORKBUDDY_DOCUMENT`，保留各来源原始附件索引；分批导入不改变最终行顺序。旧单件入口保留兼容。

每份 PDF 独立领取并提交原有两块冻结区域，仍只覆盖 B–I。每份 Word 由宿主读取真实正文及表格，提交完整文档提取并归档，再从正式 `TEXT_SECTION` 等 unit 中为 B–I 各取一条可定位引文。Word 的正文提取与 PDF 的定向 OCR 是两种不同来源契约；Word 图片内容若未形成可核验文字则该文件不具备本次写入条件。J–AA 不做字段提取。用户本轮若有多份来源，只要其中一份缺 B–I 证据，整轮 Excel 不发布。

`TaskPayload.manifest.sources` 与 `records` 均按 `source_order` 排列，`records.source_index` 必须恰好为 `0..N−1`，每份来源一行。服务端逐份核验 PDF 冻结 `field_refs` 或 Word 正式正文引文、B 列标签剥离、A 序号与 J–AA 空值；本地 `excel_writeback_apply.rows` 同顺序每行仅传 B–I 八个值。完成所有来源后对同一 checkout 只执行一次 apply 和一次 publish，目标修订及文件版本各前进一步；失败不报告部分成功。发布回执逐文件列出目标行、字段证据与最终下载链接。无需数据库迁移。

自动验证覆盖混合附件分批但保留原始顺序、两份 Word 同轮写入两行并仅发布一次，以及原 PDF 定向 OCR 仍不读其他页面。真实 WorkBuddy 验收需部署最新 API/MCP 与 0.1.51 插件包后新建会话，可在同一条消息上传两份不同 PDF 和一份 DOCX，并写明目标完整 `.xlsx` 文件名及“只填写 A–I”；应看到三份来源各一行、一次发布、PDF 每份仅 OCR 第 1 与第 3 页。扫描 Word 图片的 OCR 能力不属于本次正文来源路径。

## 12. 22 页 A4 来源与归档等待态修复（2026-09-29）

本轮真实上传的 PDF 为 22 页 A4（第 1、3 页约 595×842 pt），旧定向契约只接受 25 页、约 576×823 pt，后台在查重阶段抛出 `SCOPE_LOCATION_UNRESOLVED`。旧 worker 把确定性的 422 当成可重试文件系统异常，归档记录留在 `RETRY_WAIT`；再次领取原任务时又将其标记为幂等完成，因此没有签发 OCR 任务且条目持续显示归档中。原 PDF 未覆盖。

已核对新 PDF 第 1 页包含单位和姓名，第 3 页基本情况包含 B–I 所需字段行。服务端新增 `a4_22_page` 冻结版式，与原 `professor_25_page` 版式共用第 1 页封面和第 3 页基本情况两块裁剪区域；新任务在 `scope_digest` 快照中记录 `layout_variant`。两种版式均不读取其他页面。字段值仍须由 WorkBuddy 实际 OCR 提交并通过 B–I 逐列引文校验，区域读不清或字段缺失即停止。

后台文件任务遇到 HTTP 4xx 版式校验错误时直接进入 `FAILED`，把安全错误码和说明写回归档回执，不再重复排队。针对旧 worker 遗留的 `RETRY_WAIT + 已完成查重任务 + 无 OCR 任务`，归档补偿器只对 `SCOPED_EXCEL_SOURCE` 创建一次新的受控查重任务；新 worker 完成后签发 `SCOPED_OCR`。补偿不复制、覆盖或全文解析 PDF，也不发布 Excel。无需数据库迁移。已用真实 PDF 在回滚事务中验证新范围可签发，用自动测试验证两种版式、只暴露两块区域、旧等待态补偿和 422 立即失败。更新 API 与 worker 并触发一次既有归档补偿任务后，再由 WorkBuddy 从同一批次领取 OCR 任务；不需要重新上传。
