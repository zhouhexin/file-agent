---
name: file-agent-excel-writeback
description: 用户明确要求由 WorkBuddy 写入完整 Excel 并回传 File Agent 保存时使用；新上传模板或按文件名追加均可。教授汇总表 B–I/L 定向填表与手工补全同名文件回传分别使用其专用 Skill。
---

# WorkBuddy 写完整 Excel，服务器验证发布

此 Skill 使用独立 `excel_writeback_*` MCP 工具。不要把它和旧
`structured_dataset_preview/confirm/commit` 混用；同一任务只能选一个写入者。
WorkBuddy 负责读取 PDF、Word、图片及目标 Excel 字段，决定最终各格展示值；
File Agent 只核对正式来源、审批证据、当前目标版本与文件差异，然后发布同名工作副本。
工具缺失或功能开关未启用时说明能力未就绪；不得用 Shell、SQL、临时重建 Excel 或旧工具假装完成新链路。

## 重复上传必须由用户决定

本轮新上传的 Excel 模板和 PDF、Word、图片等来源文件，均先按附件桥接 Skill 导入并检查批次状态。
重复候选只能在文件接收、查重后确定；不要求在上传前凭文件名猜测是否重复。
只要任一条目进入 `WAITING_DUPLICATE_CONFIRMATION`，或 `batch_get` 返回非空
`pending_duplicate_reviews`，就必须先执行 `file-agent-attachment-bridge` 的“重复文件对比强制流程”：
读取最新 `duplicate_review_get`，对可对比候选调用 `duplicate_comparison_get`，向用户展示本轮返回的
对比链接和“继续上传／使用已有文件／取消上传”选项，然后结束本轮并等待用户明确选择。
用户选择前不得调用 `duplicate_decide`，也不得替用户选择、跳过待确认条目或继续将其作为 Excel
目标或提取来源。用户明确选择后，按最新候选修订提交决定，再重新核对实际归档的目标和来源 ID、版本；
若选择取消，不能把该文件用于本轮写入。没有重复候选时无需额外询问。

`DIRECT_SAVE` 仅表示提取结果通过校验后无需人工复核即可写入 Excel，**不包含重复上传的处理授权**；
即使提示词写“直接保存”“无需复核”，也不能代替用户对重复文件的明确选择。

## 目标与来源

1. 本轮有新上传 Excel 作为目标时，必须提供已归档 `uploaded_document_id`，以该上传件注册**新的独立目标**；
   不得仅凭文件名 checkout 旧数据集，也不得因用户同时提到旧文件名而追加旧表。必要时提供版本、工作表和布局。
   若服务器已有唯一同名已发布目标，仍以本轮上传件 checkout；新表发布成功后服务端归档旧目标，
   后续按文件名只命中新表。旧表不被复用、续写或自动改名。其他同名歧义、多工作表或表头不清楚时先请用户选定。
   **只有本轮没有新上传的目标 Excel** 时，才按用户给出的完整文件名 checkout 已有目标，读取该表当前字段
   并追加；找不到或重名必须让用户选定，不能猜测。
2. 调用 `excel_writeback_checkout`，保留返回的 `task_id`、稳定目标 ID、当前版本、SHA-256、
   `sheet_name`、完整 `labels` 和 `layout`。`.xls` 必须先由服务器受控异步提升为 `.xlsx`；
   等任务实际完成后重试，不让 WorkBuddy 自行转换或重建原模板。
3. 只从 checkout 的当前 Excel 读字段和已有内容，不能用上次本地缓存。读取来源材料沿用主 Skill 的
   WorkBuddy 文档提取任务和真实可用 Provider，不固定 OCR 提供商、不回退到程序内部 OCR。
   需要按印章判断属实范围、资格或业务列时，报告当前插件不支持并停止该填表任务；
   不得用本通用写回流程代替盖章核验，也不得自动缩小为 B–I/L。来源已有正式
   提取结果时，先用 `structured_dataset_source(mode="INDEX")` 取得区段目录与固定运行 ID，
   将各列所需区段合并后用 `mode="FULL", unit_orders=[...]` 分批读取真实原文；同一来源、同一
   运行的已读取区段在本轮复用，不逐列重取完整 PDF。若索引不足以定位，读取必要续页或整份
   已保存正文，不得因提速略过条件、印章或证据。索引本身不能作为字段引用或印章依据。
4. 用与旧任务一致的 `TaskPayload`：`manifest.target` 只设 `dataset_id`、当前
   `document_version_id`、实际 `filename/sheet_name/layout/labels`；每条来源固定正式 ID 和 SHA-256；
   每个记录逐列覆盖全部字段，有内容给原文引用及需要的审批/条件证据，无内容给 `blank_reason`。
   多来源记录按 `source_index` 升序，每个人一行。不能编造引文、印章、勾选或缺失值。

## 本机写入与预览

1. WorkBuddy 根据已核实的每列结果形成 `rows`，顺序和 `payload.records`、`labels` 完全相同。
   `rows` 是 Excel 中实际展示值，符合整列条件才在整个数据格文字末尾加一次 ` √`；
   不在标题或每个小项后加勾。预置序号格可传 `null` 保留模板序号。
2. 调用 `excel_writeback_apply(task_id,payload,rows)`。它从冻结基线重新写候选文件，
   只修改目标工作表新数据格，原工作簿其他 ZIP 部件原字节保留，同时登记完整文件和候选共同摘要。
   不要使用普通编辑器另存、`openpyxl.save` 或自行复制整张表代替；这些方式可能重写公式、样式、
   图表和其他工作表，服务器将拒绝。如果专用本地写入工具不能处理模板，停止并说明原因。
3. `REVIEW_REQUIRED`：必须向用户展示 `preview_result` 中所有逐列具体值、证据、审批状态、
   留空/排除原因及最终 Excel 文件名，询问确认并结束本轮。用户修正后重新 checkout 新任务和新指纹。
   用户明确确认同一预览后才调用 `excel_writeback_confirm(task_id,result_digest)`。
4. `DIRECT_SAVE`：Excel 新建和续写默认使用；仍必须完成真实读取、逐列证据和 apply 预览登记，
   但无需调用 confirm，直接 publish。只有用户本轮明确要求先复核才选 `REVIEW_REQUIRED`；
   不得因为耗时、失败或缺证据擅自切换模式。重复文件仍需用户选择。

## 发布与回执

调用 `excel_writeback_publish(task_id,result_digest,payload)`。工具只回传固定任务目录内的完整
`.xlsx`，服务端再次逐字校验证据、条件和 `√`、旧版本 CAS、表头、原有行、样式、公式、
非目标工作表和 OOXML 部件。若模板部件被重写或目标版本已变化，停止并重新 checkout，
不能覆盖当前文件。失败时不得宣称“已保存”。

仅当返回 `status=COMPLETED`、`excel_published=true`、`task_record_count>0`，
且含 `dataset_id/working_copy_id/document_id/document_version_id/version_number/filename/export_id`
及下载 ResourceLink 时报告成功。向用户报实际同名文件、版本号、本轮增加的记录数、填入和留空的列。
服务器只维护一个可见的同名当前工作副本；旧版本由 DocumentVersion 管理，后续仍按文件名继续追加。
本地可编辑缓存或单纯 `preview` 成功不代表服务器文件已生成。

## 修正已发布数据行

已有人员行写错时不要用上述新增行 apply/publish 重新追加。按同名文件与稳定
`record_id`、真实行号调用独立的 `excel_writeback_correction_checkout`，取得当前版本、
目标行旧值、字段键与原来源版本；优先复用该来源版本已保存的 WorkBuddy 结果，不重提整本 Excel
或整份 PDF。只对有差异的列和缺失的印章页/区域补读；已缓存文字不等于已识别印章。
补读形成新的正式提取运行时，带新的 `source_extraction_run_id` 与新请求键重新 checkout，
不得在已冻结的更正任务中偷换来源。

以 `excel_writeback_correction_apply` 提交每格 `field_key/before/after/field` 及固定来源引用。
候选只改该行被列出的单元格，服务端重新核验字段证据、整列末尾单次 `√`、其他部件不变和当前版本。
更正既有内容也默认 `DIRECT_SAVE`：逐格证据、旧值、新值及差异校验通过后直接调用
`excel_writeback_correction_publish`，在最终回执报告每格变化；不跳过服务器证据、行绑定、版本 CAS
或工作簿差异校验。仅用户本轮明确要求先复核时，checkout 传 `REVIEW_REQUIRED`，展示完整逐格差异，
用户明确同意后再调用 `excel_writeback_correction_confirm` 和 `excel_writeback_correction_publish`。
最终仍是原同名文件的新版本，不新增人员行；失败或版本冲突时
重新 checkout，不绕过服务端保护，也不把本地文件当成已发布结果。
