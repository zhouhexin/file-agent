# WorkBuddy 显式内容提取复核分流方案

## 目标与边界

同样使用 WorkBuddy 读取文件内容时，必须按用户意图区分两条链路：

- `DIRECT_ARCHIVE`：用户只要求上传、归档、分类或整理，或者在显式提取中明确要求无需复核/确认、
  直接生成并保存/写入。前者继续归档流程；后者在直接提交正文后继续结构化数据写入和 Excel 导出。
- `CONFIRM_BEFORE_SAVE`：用户明确要求提取正文、字段或表格内容。WorkBuddy 先读取并向用户展示，
  在用户明确确认前，File Agent 不得接收或持久化正文、页面、元素或结构化记录；用户没有明确要求
  跳过复核时默认使用本路径。

同一请求同时包含归档和显式内容提取时，以本轮提示词中的复核表达为准。明确跳过复核才可直接保存；
表达冲突时先澄清，不能仅用归档意图推断跳过确认。

## 两阶段契约

1. 附件导入时冻结 `extraction_profile=WORKBUDDY_DOCUMENT`、`review_mode` 和对应保存策略：
   `REVIEW_REQUIRED -> CONFIRM_BEFORE_SAVE`，`DIRECT_SAVE -> DIRECT_ARCHIVE`。
2. WorkBuddy 领取固定文件并用真实宿主工具读取。
3. `REVIEW_REQUIRED` 下，本地 MCP 根据将来要提交的完整强类型结果计算规范 SHA-256，只向服务器登记指纹、来源版本、
   Provider、覆盖状态和数量；正文与 blocks 不发送到预览接口。
4. MCP 工具在服务器登记成功后，把同一完整结果作为本地 `preview_result` 返回当前 WorkBuddy 会话；
   `preview_result` 不进入服务器预览 API 或数据库。WorkBuddy 必须从该字段展示逐文件实际值、原文来源、
   缺失项与警告，不得只显示“提取完成”。展示后停止，等待用户明确确认。
5. 用户确认后调用独立确认工具，把确认绑定到任务、来源版本和结果指纹。
6. `document_extraction_submit` 重新计算正文载荷指纹；只有与当前已确认指纹完全一致才允许持久化。
7. 用户要求修改或结果发生变化时，旧确认被废止，必须重新登记指纹、展示并确认。

`DIRECT_SAVE` 不执行第 3–5 步，宿主完成同等证据判定后直接 submit；submit 成功前仍不得创建、追加或导出 Excel。

## 持久化与审计

新增 `document_extraction_confirmations`：

- 每条记录绑定一个 `ExternalExtractionTask`、用户、来源版本和结果指纹。
- 状态为 `WAITING_CONFIRMATION`、`CONFIRMED`、`SUPERSEDED` 或 `SAVED`。
- 只保存摘要和计数，不保存正文、OCR 全文、blocks、租约或本地路径。
- preview、confirm 和最终 submit 均写 ToolInvocation、ChangeSet 和 ChangeItem。

任务读取与 claim 回执返回 `extraction_save_policy`、`confirmation_required` 和当前确认状态，
使 WorkBuddy 重连后仍能恢复正确分支。

## Skill 行为

- 明确“先展示/先复核/确认后保存”或没有复核表达时使用确认策略，在展示后结束当前轮，不得提前
  submit、append 或 export。
- 明确“无需复核/无需确认/直接生成并保存或写入”时使用直接策略，不调用 preview/confirm；证据不足项仍排除。
- 用户的确认必须出现在后续明确消息中；含糊回复继续澄清。
- 纯归档请求继续使用直接策略，不展示或索要额外内容确认。
- `CONFIRM_BEFORE_SAVE` 的正文确认完成后，才能继续 `structured_dataset_append` 和 export。
- 盖章审批 Skill 只在标题下全部必需小项均 verified 时以 `标题√` 写入 Excel；小项具体文字不加勾。新增
  对勾只属于标题展示值，不进入 OCR 原文或证据 quote。

## 验收

- 显式提取在确认前提交返回 `EXTRACTION_CONFIRMATION_REQUIRED`，数据库没有 DocumentPage/Element。
- 确认后修改任一正文、字段、页码、block 或 Provider 信息都会返回
  `EXTRACTION_RESULT_CHANGED_AFTER_CONFIRMATION`。
- 完全相同结果确认后可以保存并继续归档。
- 纯归档模式保持现有直接提交行为。
- 明确直接保存的显式提取不创建 confirmation，submit 成功后才允许 append/export；review 项不写入。
- MCP 预览请求不向服务器发送 `units`、正文或 blocks。
- MCP 工具结果包含本地 `preview_result`，Skill 能从中展示实际内容；用户修改后必须产生并展示新预览。
- `structured_dataset_append` 的嵌套 evidence UUID 在 MCP 边界转换为 JSON 字符串，可携带证据正常追加记录。
