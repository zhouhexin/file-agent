# 统一 Excel 任务参数

先看实际 MCP schema。本页说明参数间的关系；示例 ID 必须替换为真实工具返回值。

## 目标与来源

`payload={manifest,records}`；`manifest={sources,target,sealed_only,extraction_confirmations}`。
仅盖章项目专用 Skill 可按其模板规则额外设置 `identity_column_range="B:I"`；普通任务省略。

- `sources[]`：待归档来源用 extraction_task_id、source_sha256；已有来源用 document_id、
  document_version_id、extraction_run_id、source_sha256。两种不能混用。
- `target` 固定 filename、完整 labels 顺序、sheet_name、layout，另从以下四种选一种：
  - uploaded_task_id：本轮新上传模板的提取任务；服务端定位完成归档后的真实副本。
  - uploaded：已入库模板的 document_id、document_version_id、extraction_run_id、sheet_name、
    layout、idempotency_key；旧 XLS 还传 legacy_target_policy=CONVERT_TO_XLSX。
  - dataset_id + document_version_id：resolve 返回的旧目标绑定。
  - new：name、filename、fields、idempotency_key；fields 包含 key、完整 label、field_type、required。
- `target.layout` 与 uploaded.layout 必须一致；新表无复杂模板时均省略。上传 XLS 最终 filename 为 .xlsx。
- `extraction_confirmations[]`：REVIEW_REQUIRED 的每个来源及模板的 document_extraction_preview 返回的
  task_id、confirmation_id、result_digest。DIRECT_SAVE 已提交来源时为空。
- 不把目标 Excel 作为 sources 再抽取一遍。sources 只列有拟写入记录的材料；无有效内容的其他材料在回执列为排除项。

## 逐列记录

每个 records 元素含 source_index（sources 的零基索引）和 fields（与 labels 等长、同顺序）。
一名人员的多个业绩放同一 fields，不按业绩拆人员行。每条记录目前绑定一个来源；多文件共同证明同一人员且
不能在一个来源中形成完整记录时停止并说明该限制，不得拆成重复人员行冒充合并。

每列非空用 raw_text + evidence_refs 或 fragments；引文为：

```json
{"source_unit_id":"本轮真实 unit_id 或正式 UUID","quote":"逐字原文","start":0,"end":4}
```

start/end 是左闭右开字符偏移，可同时省略；重复引文必须提供真实偏移。
fragments 可跨区段，自动逐段用换行合并为 raw_text，不额外改写文字。空列用 blank_reason。
confidence 仅传真实来源评分；没有则 null，不为了避免低分校验伪造 1.0。统一任务将无评分与低评分区分，
仍要求精确证据、类型合法和本轮确认/直接保存授权；显式低评分仍可能阻塞保存，应展示并处理不确定项。

盖章字段还必须有 approval：binding_method、declaration（审批原文引文）、seal（真实识别印章信号引文）、
scope（被批准原文范围引文数组）、explanation。绑定方式支持 EXPLICIT_RANGE、WHOLE_ITEM、WHOLE_SECTION、
CHECKED、ROW_ORDER、LAST_ITEM_FALLBACK。宿主仍负责根据真实印章位置判定，服务器文字引用校验不替代印章检测。
seal 必须来自保存的真实识别结果，不能为了满足字段而自行在原文中插入“圆章”。
唯一例外是 file-agent-sealed-item-extraction 的物理 B–I 身份列：仍需逐格原文引用，
不需要 approval 或 assessment，也不能在值后加勾；其余业务列保持原审批证据链。

## 整列条件

例如完整标题“教学工作量不少于80学时”，原文“教学工作量：120学时”且本栏审批通过，可提供：

```json
{
  "title": "教学工作量不少于80学时",
  "mode": "ALL",
  "groups": [{"checks": [{
    "criterion": "教学工作量不少于80学时", "operator": "GTE",
    "expected": "80", "observed": "120",
    "evidence": [{"source_unit_id": "本轮真实unit_id", "quote": "教学工作量：120学时"}]
  }]}]
}
```

仅将真实完整标题及相应证据映射到上述结构；示例不是通用数据。
组内 checks 为 AND；mode=ALL 要求所有组；ANY 至少一组；AT_LEAST 用 minimum 指定数量。
required_groups 是必须满足的组零基索引。保留全部分支，不能只提交通过分支。
白名单比较为 CONTAINS、EQUALS、GTE、LTE；数字观测值必须来自对应证据，阈值来自标题，不混用单位和年限。
不可唯一表达的条件放 unresolved_reasons，禁止伪造 through=true 或 checkmark=true。
服务端返回的判定与 display_value 才是最终加勾依据；OCR 原文和 raw_text 不新增勾。

## 状态

preview 返回 task_id、result_digest、preview_result，后两次调用只传 task_id 和原指纹。
用户改了来源、目标或任何列就必须新预览；不能把新载荷绑定旧指纹。
task_get 是只读；commit 才推进。COMPLETED 且 excel_published=true 之前均不能说 Excel 已保存。
