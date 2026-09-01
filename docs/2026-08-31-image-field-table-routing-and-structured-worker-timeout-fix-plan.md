# 图片字段表格展示与结构化 Worker 超时修复方案

## 1. 问题结论

当前系统把“以表格形式返回”同时解释成两件不同的事：

1. 展示要求：将已经识别出的字段和值用前端表格展示。
2. 结构恢复要求：从图片中恢复原始行、列、单元格、合并关系和多条记录。

`is_structured_image_extraction_request()` 目前只要同时检测到“图片/识别/表格”就返回 true，导致普通的
“识别图片中的申请人、资助金额和使用情况登记，以表格形式返回”也进入
`extract-image-structured-data -> STRUCTURED_EXTRACTION` 异步队列。

这不是单纯的前端样式切换。当前重型链路会调用腾讯云表格 OCR 或 PP-Structure，恢复版面结构、映射字段、
校验证据并持久化结构化运行，所以才被放入专用 Worker。但是，“表格”作为展示格式本身不应成为启动该
重型链路的充分条件。

本次实际卡住的任务为：

- AgentRun：`11bebe47-a700-490a-911f-8983e84dbc41`
- FilesystemJob：`2ed98d89-5865-4a54-84cf-7ad597156dd3`
- 19:46:49 入队，19:46:51 被 Worker 领取。
- 后续没有完成或失败事件，前端一直保留 `WAITING_FOR_ASYNC_JOB`。
- Worker 的租约心跳会持续续租，但结构化处理没有任务级硬截止时间，卡住后不会自然进入失败回执。

## 2. 修复目标

1. 将“识别内容”和“如何展示”拆成两个独立维度。
2. 普通图片字段识别即使要求表格展示，也不依赖 `STRUCTURED_EXTRACTION` Worker。
3. 只有明确要求恢复原始表格结构、多条明细行或导出结构化表格时才进入专用 Worker。
4. 重型结构化任务必须在可配置期限内完成或失败，不能无限显示“处理中”。
5. Worker 超时、崩溃、租约耗尽后，`FilesystemJob`、`StructuredExtractionRun`、`ToolInvocation` 和
   `AgentRun` 必须一致进入终态。
6. 原始文件始终不修改；失败和超时也必须生成可审计回执。

## 3. 意图与展示格式解耦

### 3.1 新的受控语义

Planner 不再只输出 `presentation=TABLE`，而是分别表达：

```json
{
  "extraction_mode": "FIELD_VALUES",
  "record_shape": "SINGLE_FORM",
  "presentation": "TABLE"
}
```

枚举建议：

```text
extraction_mode:
- OCR_TEXT：只读取图片文字
- FIELD_VALUES：从 OCR 正文中提取用户点名的字段值
- TABLE_STRUCTURE：恢复原始行列、单元格或多条明细记录

record_shape:
- SINGLE_FORM：一张表单的一组字段和值
- REPEATED_RECORDS：多条同构记录
- SOURCE_TABLE：忠实恢复原图表格结构

presentation:
- TEXT
- TABLE
- JSON
- CSV
- XLSX
```

其中 `presentation` 只控制返回格式，不能单独决定使用哪个 OCR Provider 或是否创建异步任务。

### 3.2 确定性路由规则

| 用户表达 | extraction_mode | 执行链路 |
|---|---|---|
| “读取这张图片” | OCR_TEXT | 普通 OCR |
| “识别申请人、金额和使用情况” | FIELD_VALUES | OCR + 证据字段回答 |
| “识别申请人、金额和使用情况，以表格形式返回” | FIELD_VALUES | OCR + 证据字段回答 + 前端表格 |
| “把识别结果整理成两列表格” | FIELD_VALUES | OCR + 字段/值表格 |
| “恢复图片中的原始表格结构” | TABLE_STRUCTURE | 结构化 Worker |
| “逐行识别表格中的所有记录” | TABLE_STRUCTURE | 结构化 Worker |
| “保留行列、单元格和合并关系” | TABLE_STRUCTURE | 结构化 Worker |
| “按原表导出 Excel/CSV” | TABLE_STRUCTURE | 结构化 Worker |

重型结构恢复只允许由下列强信号触发：

- 原始表格、原表、恢复表格结构。
- 行列、单元格、合并单元格、保持版式。
- 每行每列、逐行、全部明细、多条记录。
- 按原表导出 Excel/CSV/XLSX。

“表格展示”“以表格形式返回”“整理成表格”只能设置 `presentation=TABLE`，不能设置
`extraction_mode=TABLE_STRUCTURE`。

### 3.3 字段拆分修复

当前请求还出现了“资助金额”和“使用情况登记”被合并为一个字段的现象。字段解析器需要：

1. 先删除“以表格形式返回”等展示后缀。
2. 再按 `、`、中文逗号、英文逗号、`和`、`以及` 分隔字段。
3. 保留业务短语内部的固定词组，避免把“使用情况登记”继续拆散。
4. 对用户点名字段生成稳定 key；未知字段使用安全序号 key。
5. Planner 生成的字段必须再次经过后端确定性解析结果校验，LLM 不得合并、删除或新增字段。

本例最终必须得到三个字段：

```text
申请人
资助金额
使用情况登记
```

## 4. 轻量字段表格链路

### 4.1 后端执行链路

单张图片或表单的字段表格请求改为：

```text
后端确定附件 document_id
-> extract-document-text（需要时执行或复用 OCR）
-> document_pages 完整 OCR 正文
-> evidence-answer 的受控字段模式
-> 校验每个字段值与 page/quote 证据
-> field_table_result
-> 前端表格展示
```

建议扩展 `evidence-answer` 输入，而不是再建立一个重复的文件读取体系：

```json
{
  "question": "识别图片中的申请人、资助金额和使用情况登记",
  "document_ids": ["document-id"],
  "answer_mode": "FOCUSED",
  "response_format": "FIELD_TABLE",
  "fields": [
    {"key": "applicant", "label": "申请人", "field_type": "person_name"},
    {"key": "funding_amount", "label": "资助金额", "field_type": "money"},
    {"key": "field_3", "label": "使用情况登记", "field_type": "string"}
  ]
}
```

输出增加可选的受控结果：

```json
{
  "field_table": {
    "field_schema": [],
    "records": [],
    "review_items": [],
    "original_unchanged": true
  }
}
```

要求：

- 字段值必须来自完整 `document_pages`，不能来自短 `text_preview`。
- LLM 只做字段映射，字段列表由后端锁定。
- 金额、日期等类型继续使用确定性规范化和原文一致性校验。
- 无证据字段返回 `MISSING` 或 `NEEDS_REVIEW`，不能编造。
- 引用继续在数据库和 Tool 输出中保留；按既有产品要求，普通 OCR 字段识别的前端可以不重复展示原文证据卡。
- 该链路不创建 `STRUCTURED_EXTRACTION` 文件系统任务，因此不需要专用 Worker。

### 4.2 前端展示

新增通用的 `FieldTableResult` 投影，或将现有结构化表格组件抽成只依赖
`field_schema + records + review_items` 的展示组件。

前端标题应区分：

- 轻量字段表格：“图片识别结果”。
- 重型结构恢复：“图片表格结构识别结果”。

不能把轻量结果伪装成已经恢复了原始单元格结构。CSV/XLSX 下载按钮只在后端确实生成派生件时显示。

## 5. 重型结构化任务超时与恢复

### 5.1 任务级硬超时

新增配置：

```env
STRUCTURED_EXTRACTION_TASK_TIMEOUT_SECONDS=300
```

默认 300 秒，配置层限制在 30 至 900 秒。Provider 自身的 HTTP 超时仍然保留，任务级超时是覆盖页面
渲染、OCR、字段映射、持久化和 AgentRun 恢复的总预算。

仅使用线程超时不可接受：Python 无法安全终止已经卡住的 SDK 线程，进程退出时非守护线程仍可能继续阻塞。
重型结构化任务应使用可终止的子进程隔离：

```text
STRUCTURED_EXTRACTION worker 主进程领取任务
-> 生成 execution_token
-> 子进程用独立 DB Session 执行一个 job_id
-> 主进程维持租约并等待结果
-> 到期则 terminate，必要时 kill
-> 使用新的 DB Session 写入统一失败终态
-> 主 Worker 继续处理下一任务
```

为防止超时后的旧子进程晚到覆盖新状态，`filesystem_jobs` 增加 `execution_token`，完成提交前必须同时校验：

```text
status == RUNNING
lease_owner == 当前 worker
execution_token == 当前执行令牌
attempt_count == 当前尝试次数
```

不满足时拒绝提交结果。

### 5.2 阶段日志

当前日志只有 `filesystem.worker.started`，无法确认卡在腾讯云调用还是字段映射。结构化任务至少增加：

```text
structured_extraction.source_resolved
structured_extraction.page_rendering_started/completed
structured_extraction.ocr_request_started/completed
structured_extraction.field_mapping_started/completed
structured_extraction.persisting_started/completed
structured_extraction.agent_resumed
structured_extraction.timed_out
```

日志只记录 job_id、run_id、页码、Provider、耗时、状态和错误码，不写 OCR 全文、图片内容或腾讯云凭证。

### 5.3 统一失败终态

超时或不可恢复异常必须在一个事务中更新：

```text
FilesystemJob.status = FAILED
StructuredExtractionRun.status = FAILED
StructuredExtractionRun.error_code = STRUCTURED_EXTRACTION_TIMEOUT
ToolInvocation.status = FAILED
AgentRun.status = FAILED
AgentGraphState.async_job_ids 删除当前 job_id
AgentGraphState.final_response 写入用户可理解的失败说明
ChangeSet/ChangeItem 记录 STRUCTURED_EXTRACTION_FAILED
```

用户回执建议：

```text
图片结构识别超时，本次没有生成表格，原始文件未修改。
你可以重新识别，或改为“读取图片文字并按字段表格展示”。
```

### 5.4 既有卡住任务补偿

增加 `reconcile_waiting_structured_extraction_runs()`，在结构化 Worker 启动和队列空闲时执行：

1. 查找 `AgentRun.status=WAITING_FOR_ASYNC_JOB` 的结构化任务。
2. 如果关联 Job 已完成，补做 `_resume_agent_run()`。
3. 如果关联 Job 已失败或已达到尝试次数，调用 `fail_structured_extraction_agent_run()`。
4. 如果 Job 仍为 RUNNING，但已经超过任务级硬截止时间，标记超时失败。
5. 补偿必须幂等，重复运行不能创建第二份 ChangeSet 或覆盖成功结果。

这样可以修复“任务租约耗尽后 Job 已失败，但 AgentRun 仍永久等待”的现有缺口。

对当前任务的上线处理顺序：

1. 停止旧的结构化 Worker，避免旧处理继续占用进程。
2. 部署新版本并启动结构化 Worker。
3. 启动补偿扫描，将任务 `2ed98d89-5865-4a54-84cf-7ad597156dd3` 结束为超时失败。
4. 用户重新发送原请求；新路由应直接走轻量字段表格链路，不再创建结构化 Job。

## 6. 主要代码调整位置

### 后端

- `apps/api/app/modules/agent/planner.py`
  - 拆分 extraction mode、record shape 和 presentation。
  - 收紧 `is_structured_image_extraction_request()`。
  - 修复字段列表拆分和后端锁定。
- `apps/api/app/modules/agent/tool_schemas.py`
  - 为 evidence-answer 增加受控字段表格输入 schema。
- `apps/api/app/modules/evidence_answer/`
  - 生成并验证 `field_table`，复用完整原文和引用校验。
- `apps/api/app/modules/agent/user_receipt.py`
  - 投影 `field_table_result`，继续隐藏内部路径和 OCR 全文。
- `apps/api/app/modules/structured_extraction/worker.py`
  - 增加子进程执行、硬超时、统一失败和既有任务补偿。
- `apps/api/app/modules/managed_files/jobs.py`
  - 增加执行令牌和终态幂等检查。
- `apps/api/app/modules/managed_files/worker.py`
  - 结构化队列调用隔离执行器；空闲时执行结构化等待链补偿。
- `apps/api/app/core/config.py`、`.env.example`
  - 增加结构化任务总超时配置。
- `apps/api/alembic/`
  - 为 `filesystem_jobs.execution_token` 增加迁移。

### 前端

- `apps/web/src/types.ts`
  - 增加 `FieldTableResult`。
- `apps/web/src/features/chat/AgentRunReceipt.tsx`
  - 渲染轻量字段表格结果。
- `apps/web/src/features/chat/FieldTableReceipt.tsx`
  - 单独渲染轻量字段表格，保留重型结构恢复回执及其下载能力不变。

## 7. 测试方案

### Planner 与 schema

1. “识别图片中的申请人、资助金额和使用情况登记，以表格形式返回”必须生成三个字段。
2. 上述请求不得调用 `extract-image-structured-data`，不得创建异步 Job。
3. “恢复图片中的原始表格行列”必须继续进入 `STRUCTURED_EXTRACTION`。
4. “保持合并单元格并导出 XLSX”必须继续进入重型链路。
5. LLM 合并、删除或新增字段时，后端必须恢复为确定性字段集合。

### 轻量字段表格

1. OCR 页面已存在时复用，不重新外发图片。
2. 用户明确说“重新识别”时强制重新 OCR，然后生成字段表格。
3. 无证据字段显示待复核，不生成虚假字段值。
4. ToolInvocation 和引用审计仍保留，前端不重复展示原文证据卡。
5. 不启动结构化 Worker时，轻量请求也能正常完成。

### Worker 超时与恢复

1. Fake Provider 永久阻塞时，任务在测试超时内进入 FAILED。
2. 子进程超时后被终止，Worker 能继续处理下一条任务。
3. 旧 execution token 返回时不能覆盖超时失败状态。
4. Job 已失败但 AgentRun 仍等待时，补偿扫描能结束 AgentRun。
5. Worker 在成功提交和 AgentRun 恢复之间崩溃时，补偿扫描能完成恢复。
6. 超时回执不泄露异常堆栈、绝对路径、OCR 全文或密钥。

### 前端

1. `field_table_result` 按动态字段数展示全部列。
2. 轻量结果标题不声称已经恢复原始表格结构。
3. FAILED 后停止“处理中”动画并显示可重试说明。
4. 历史结构化结果和下载按钮保持兼容。

## 8. 验收标准

1. 普通“字段识别 + 表格展示”不产生 `STRUCTURED_EXTRACTION` Job。
2. 关闭或不启动结构化 Worker时，普通字段表格请求仍能完成。
3. 只有明确恢复行列结构的请求才进入专用 Worker。
4. 任一结构化任务不会无限保持 `WAITING_FOR_ASYNC_JOB`。
5. 超时后 Job、结构化运行、ToolInvocation 和 AgentRun 状态一致。
6. 当前已卡住任务可由补偿流程自动结束，不需要直接修改数据库。
7. 后端完整 pytest、前端测试和生产构建全部通过。

## 9. 推荐实施顺序

1. 先修 Planner 路由和字段拆分，让普通请求不再进入重型队列。
2. 扩展 evidence-answer 和前端通用字段表格回执，打通轻量链路。
3. 增加结构化 Worker 子进程硬超时和统一失败终态。
4. 增加既有等待任务补偿，并处理当前卡住任务。
5. 补齐全链路测试后再提交 Git。

## 10. 实施结果

本方案已按以下边界落地：

1. “以表格形式返回”不再单独触发重型结构化抽取；本例固定解析为“申请人、资助金额、使用情况登记”三个字段。
2. 轻量请求通过 `evidence-answer` 的 `FIELD_TABLE` 模式读取完整 OCR 证据，字段由后端锁定，值必须回到引用原文；前端不展示原文证据卡。
3. 明确包含“原始表格、逐行、所有记录、行列结构、合并单元格、按原表导出”等强信号的请求仍进入专用 Worker。
4. 结构化任务在生产数据库 Worker 中使用可终止子进程执行，默认总时限 300 秒；领取时生成 `execution_token`，提交前同时校验任务状态、租约持有者、执行令牌和尝试次数。
5. 队列空闲时会补偿仍处于 `WAITING_FOR_ASYNC_JOB` 的结构化运行：已完成任务恢复原 AgentRun，失败或超时任务统一写回失败终态。
6. 新增迁移 `20260831_0001_add_filesystem_job_execution_token.py`；部署时必须先执行 Alembic 升级，再重启 API 与结构化 Worker。
