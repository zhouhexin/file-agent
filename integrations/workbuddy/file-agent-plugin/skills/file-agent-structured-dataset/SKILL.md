---
name: file-agent-structured-dataset-demo
description: 用户要求从图片、Word、PDF 或电子表格提取信息并生成或追加服务器 Excel 时使用；新上传模板必建新目标，仅无新模板时按文件名续写，默认直接保存。
---

# 服务器累计 Excel

本任务必须使用 File Agent 的结构化表格 MCP 工具完成服务器发布。WorkBuddy 负责真实内容读取；
本地 Office/edsdk 生成文件、仅确认正文或只返回下载缓存，都不是服务器 Excel 已保存。

## 入口与授权

- 纯上传归档继续主 Skill 的 DIRECT_ARCHIVE，不增加内容复核。明确提取成表才使用本 Skill。
- 来源按 WORKBUDDY_DOCUMENT 领取固定 document_extraction_task_id，读取工具与 provider_name 使用宿主实际
  已安装授权能力，不固定腾讯或 xparse。不得调用程序内部 OCR 替代。
- 用户要求“只取盖章属实内容”时同时使用 file-agent-sealed-item-extraction；无明确项号仍可按整栏审批。
- Excel 新建与续写任务导入前冻结 review_mode：默认 DIRECT_SAVE、
  extraction_save_policy=DIRECT_ARCHIVE，读取并校验后直接提交和发布；仅用户本轮明确要求
  “先看/复核/确认后保存”时使用 REVIEW_REQUIRED、CONFIRM_BEFORE_SAVE。重复文件仍必须按附件桥接
  Skill 等待用户选择。冲突先澄清，不得因耗时或失败改路由。
- 本流程需要 structured_dataset_preview/confirm/commit/task_get。工具缺失时停止，提示更新 MCP 和插件。
  旧 structured_dataset_create/create_from_excel/source/append/query/export 仍兼容独立工具调用，
  但本任务不要让模型拆开这条保存链，更不能使用 Shell、SQL、私有 API 或本地编辑补救。

## 先确定目标与完整字段（只读）

1. 本轮新上传 Excel 且作为目标模板时，**必须以它创建独立新目标**，即使用户同时提到旧文件名也不追加旧表；
   目标使用 `uploaded_task_id` 或 `uploaded`，绝不改填旧 `dataset_id`。若服务器已有唯一同名已发布目标，
   仍提交本轮上传目标；服务端在新表发布成功后归档旧目标，只保留新表参与文件名匹配，不续写旧表。
   同名对象不是已发布累计目标或存在多个活动同名目标时停止请用户解决。多个新 Excel 或多个非空工作表先让用户
   选择。本轮 Excel 明确仅作普通来源时，尊重用户说明。不得静默选第一个。
2. **只有本轮没有新上传的目标 Excel**，且用户以完整文件名指定已有 Excel 时，才用
   structured_dataset_resolve 精确解析完整文件名，读取其当前工作表、标题与字段，并在该文件末尾追加；
   使用其稳定 dataset_id、document_version_id、filename、sheet_name、layout 和字段顺序。
   未找到或多个同名文件必须询问，不按时间、相似度猜测。
3. 没有模板、也未指定续写目标时按提示词设计字段，形成 new 目标计划。用户指定名称原样使用；
   未指定时生成可读 .xlsx 名称并告知。“新建名为……”不能被当成续写。
4. 从真实 Excel 读取全部完整多层标题、工作表和现有行。字段范围以当前 Excel 为准，不要只挑容易提取的列。
   每个人一行的表，把该人员多条业绩归入对应列，不得拆成多个人员行。
5. 复杂模板必须固定实际 layout，如 header_range=A2:AA4、data_range=A5:AA6（仅示例），
   保留合并表头、尾注、格式、预置序号和其他工作表。区域不确定先询问，不能在尾注下重建普通表。
   重复叶标题用完整层级和真实列号区分，与服务端 schema 一致。
   读取结果必须包含该工作表表头范围内非空格的真实 TABLE_CELL（unit 上的 sheet_name、从零开始的行列索引、原文）；
   等价无损网格到 SHEET unit 的转换与提交前自检按 `file-agent-excel-grid-archive` 执行。
   普通 SHEET 正文、OCR 文本或模型推断的列号不能代替单元格网格。缺失时先停止预览/保存，换用能提供
   结构化网格的宿主工具重新读取；无法获取时提示上传可读取的 .xlsx，不能继续耗时转换。
6. .xls 只确定性提升扩展名为 .xlsx，回执中明确展示原名、最终名及转换说明。不能加时间戳、UUID、
   (2) 或 converted 后缀。转换须在冻结的直写授权或显式复核确认后由服务器完成，宿主不能自行转换或重建模板。

## 形成完整逐列候选

调用前阅读 [任务参数与示例](references/task-payload.md)，并以当前工具 schema 为准。

- 来源固定 extraction_task_id + source_sha256，或已有工作副本 document_id/version/extraction_run_id + 哈希；
  不猜测归档后的 ID。新上传模板用 uploaded_task_id；已正式入库模板用 uploaded；已有数据集用 dataset_id；
  新表用 new。这四种目标只能选一个；新上传目标不得使用旧数据集 ID。
- REVIEW_REQUIRED：逐文件 document_extraction_preview 缓存完整宿主读取结果，在 manifest.extraction_confirmations
  记录其 task_id、confirmation_id、result_digest。此时不确认/submit，不写服务器正文。
- DIRECT_SAVE：按主 Skill 直接 submit 真实来源（包括模板）并检查成功，不增加文档确认。
- records 每行必须覆盖 labels 的所有列且顺序一致。每列提供原文及 evidence_refs，或 blank_reason；
  多段非连续原文使用 fragments，由工具逐字校验并用换行合并，不能拼成不存在于原文的单一引文。
- 有原文但不满足资格条件不等于没有内容：盖章范围允许时仍填写事实，不加勾。未审批/无法确定内容留空说明。
- 盖章任务 manifest.sealed_only=true，业务有值列必须提供 approval。仅当同时启用
  file-agent-sealed-item-extraction 且该专用 Skill 核实模板物理 B–I 为身份列时，才设置
  identity_column_range="B:I"：身份格仍需原文证据，但无需 approval/assessment，也不加勾。
  普通表格任务不得设置此豁免。业务列 scope 覆盖全部字段片段及 assessment 证据，
  不得拿同页无关印章证明另一栏；签字不必需，无编号不构成拒绝填写理由。
- 整列资格使用 assessment 的完整标题、条件分组和来源证据。不改表头，不在各小项加勾；
  只有整列条件满足，服务端才在整个人员数据格具体文字末尾增加一次 √。
- 引文保持原文，不新增 √；复杂条件无法可靠表达时给出 unresolved_reasons，禁止删减条件强行判通过。

## 预览、确认、发布

1. 调用 structured_dataset_preview(payload, request_key, review_mode)。完整候选只缓存在宿主；
   服务器保存指纹和目标元数据，确认前不创建数据集、不写正文/记录/Excel。
2. REVIEW_REQUIRED 必须按 preview_result 展示逐列具体值、证据、审批范围、条件判定和留空原因，
   包括全部排除项。不能只报数量，不能用 Markdown 文件/旧会话记忆替代结果。
   询问是否确认保存并结束本轮。用户修改则用新 request_key 生成新预览，正文变化还需新文档 preview。
3. 用户明确确认本轮完整预览后调用 structured_dataset_confirm(task_id,result_digest)，工具自动确认
   固定文档草稿、提交缓存正文并推进目标注册、append、校验和 export；无需模型重新传长正文。
4. DIRECT_SAVE 也登记 structured_dataset_preview 的本地候选，但 review_mode=DIRECT_SAVE，
   不等待用户、不调用 confirm，立即 structured_dataset_commit。同一证据与发布规则仍适用。
5. WAITING_DEPENDENCY/PREPARING_TARGET 表示尚未完成。转换任务只用 job_get 查询固定 filesystem_job_id；
   来源归档按原 batch/job 状态核验。依赖完成后继续原 task_id 和 result_digest 的 commit。
   不忙循环、不重做 OCR、不换键重复 append；相同错误最多修正重试一次，再报告错误及待处理阶段。
   FAILED/NEEDS_INPUT 是终态，读取 error.code、filesystem_job_id 和 next_action 后向用户说明；
   不再对该 task_id 循环 commit。缺少表头坐标或逐格核对失败必须重新真实读取并生成新预览指纹。
6. 调用失败、草稿丢失、源/目标版本变化时不得生成替代文件。首次提交前缓存丢失须重新预览确认；
   已提交后服务器可按原任务恢复。源/目标内容变化必须新预览，不能复用旧指纹。
7. 只有 status=COMPLETED、excel_published=true、本轮 task_record_count>0，并有非空 dataset_id、
   working_copy_id、document_id、document_version_id、filename、version_number，才报告完成。
   后端会核验真实版本下载及当前文件。不能把 content_saved=true 或 append 成功当成 Excel 已发布。
8. 最终给出实际文件名、是新建还是追加、版本号、本轮记录数、加勾列、留空/排除原因，以及 ResourceLink。
   下载失败时明确“服务器已保存，宿主下载失败”，不重复追加。认证 API URL 不是公开下载链接。

## 服务器文件与恢复边界

- 最终普通工作副本保存于 WORKING_COPY_STORAGE_ROOT/structured-datasets，实际 basename、列表名和下载名
  完全一致。内部依赖稳定 Document ID、WorkingCopy ID、dataset ID 和当前版本，不能只凭文件名写入。
- 一个目标只有一个当前工作副本，历史用 DocumentVersion，不生成带版本后缀的多个 Excel。
  新上传 Excel 始终建立新目标；只有后续无新目标上传且按文件名指定它时才追加。重命名走原确认/冲突/同步流程。
- .xls 由服务器 STRUCTURED_EXTRACTION 异步转换注册，继续同一 Document/WorkingCopy，
  原始 .xls 保留为历史来源，当前只显示 .xlsx。转换后校验工作表、标题和布局一致，禁止近似重建。
- TARGET_EXCEL_NOT_FOUND/AMBIGUOUS/SHEET_AMBIGUOUS/FILENAME_CONFLICT：用户选择/改名，不能静默覆盖。
- TARGET_EXCEL_VERSION_CONFLICT/SCHEMA_CONFLICT：停止并重新读取目标；复核路径重新预览确认。
- TARGET_EXCEL_LAYOUT_REQUIRED/INVALID/DATA_RANGE_FULL：修正布局或更换模板，不覆盖旧行和尾注。
- TARGET_EXCEL_HEADER_CELLS_MISSING：当前读取没有真实表头坐标；重新使用结构化表格能力读取或上传 .xlsx，
  不得用 OCR/普通正文补造 TABLE_CELL，也不得继续同一失败任务；下一次受控读取按 `file-agent-excel-grid-archive` 构造载荷。
- LIBREOFFICE_NOT_AVAILABLE/XLS_CONVERSION_TIMEOUT/XLS_CONVERSION_OUTPUT_INVALID/CONVERSION_SCHEMA_MISMATCH/
  FEATURE_UNSUPPORTED：报告转换失败，保留原件；请管理员修复或用户另传 .xlsx。
- DATASET_TASK_IN_PROGRESS：同一目标已有未完成任务，先恢复该任务；不能绕过占用直接 append/export。
- DATASET_NO_WRITABLE_RECORDS/RECORDS_NEED_REVIEW：没有可发布的本轮数据，不生成仅表头结果，不称完成。
- 同一原始 idempotency_key 和载荷恢复，不因失败重复累计。任务保留追加阶段进度，发布失败继续 commit。
- 不读取/打印 mcp.json、连接器配置、环境变量、token、API key；claim/preview 结构化结果缺失时安全失败。
