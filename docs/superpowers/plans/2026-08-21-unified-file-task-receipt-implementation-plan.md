# 统一文件任务回执实施方案

**目标：** 所有涉及文件的对话任务统一采用“确认任务理解 → 展示业务进度 → 先给结果摘要 → 展示逐文件明细和证据 → 说明文件变化 → 提供下一步”的回答结构，同时保留搜索、分类、证据回答、重命名和 OperationPlan 等专用交互。

**范围：** 上传与导入、读取、摘要、正文问答、分类、检索、目录列举、表格分析、重命名建议、文件选择、冲突处理、OperationPlan、确认后的文件动作、失败和待复核回执。本文只调整回答和展示架构，不新增压缩包识别或解压能力。

## 1. 当前实现与主要缺口

当前后端已经有三层基础：

- `graph.py::_deterministic_response` 根据 `result_summary` 生成确定性最终文本。
- `user_receipt.py::UserTaskReceipt` 把内部 AgentRun 事实投影为普通用户可见结果。
- `AgentRunReceipt.tsx` 根据 `response_type` 选择搜索、证据、分类、重命名或 OperationPlan 等专用组件。

主要缺口不是缺少专用卡片，而是缺少所有文件任务共享的回执外壳：

- 正在处理时通常只显示“正在处理你的请求”，没有业务阶段。
- 不同分支对任务范围、结果数量、失败数量和原件状态的表达不一致。
- 一部分结果主要依靠 `final_response`，另一部分只展示卡片，容易重复或遗漏。
- 下一步动作目前主要是字符串提示，前端无法稳定区分“填入对话”“打开文件”和“确认计划”。
- 新增文件能力时容易继续在 `AgentRunReceipt` 增加早返回分支，回答体验会越来越分散。

## 2. 统一回答结构

所有文件任务统一使用以下六段结构；没有业务内容的段落可以隐藏，但顺序不能打乱：

```text
1. 任务理解：对象、范围、动作、条件关系、限制
2. 关键进度：只展示用户可理解的业务阶段
3. 结果摘要：数量、完成度、主要结论、待处理数量
4. 专用明细：逐文件状态、证据、分类、before/after 或表格结果
5. 文件变化：原件、工作副本、派生件、待确认计划分别说明
6. 下一步：查看、筛选、总结、纠正或确认计划
```

技术 Tool 名、队列名、SQL、物理路径、内部评分和重试过程不进入普通用户回执。

## 3. 后端统一数据契约

在现有 `UserTaskReceipt` 中新增可选的 `presentation`，保持历史回执和现有专用 payload 兼容：

```python
class FileTaskPresentation(BaseModel):
    """所有文件任务共享的用户展示外壳，只包含经过后端验证的业务事实。"""

    schema_version: Literal["file-task-receipt.v1"]
    task_kind: Literal[
        "INGEST", "READ", "SUMMARIZE", "ANSWER", "CLASSIFY", "SEARCH",
        "LIST", "SPREADSHEET", "RENAME_SUGGESTION", "OPERATION_PLAN",
        "FILE_OPERATION", "CLARIFICATION", "FAILURE"
    ]
    title: str
    phase: FileTaskPhase
    request: FileTaskRequestPresentation
    outcome: FileTaskOutcomePresentation
    change_impact: FileChangeImpactPresentation
    notices: list[FileTaskNotice]
    next_actions: list[FileTaskNextAction]
```

建议子结构：

```text
FileTaskPhase
- code: RECEIVED | UNDERSTANDING | PROCESSING | ORGANIZING | WAITING_CONFIRMATION | COMPLETED | NEEDS_ATTENTION | FAILED
- label: 用户可见业务阶段

FileTaskRequestPresentation
- target_label: 当前附件 / 指定文件 / 受管目录 / 学校级文件等
- scope_label: 用户可理解的范围；不使用“当前 workspace”替代业务范围
- action_label: 读取、分类、检索、生成改名建议等
- conditions[]: 范围词、主题组、AND/OR 关系、年份、单位、文种等已确认条件

FileTaskOutcomePresentation
- headline: 一句话结果
- total_count
- completed_count
- failed_count
- needs_review_count
- skipped_count
- completeness: COMPLETE | PROCESSING | PARTIAL | UNVERIFIABLE

FileChangeImpactPresentation
- originals_changed: true | false | null
- working_copies_changed: true | false | null
- derivatives_created: int
- operation_executed: true | false
- message: 确定性说明，例如“受管原件未改变；本次只生成了分类建议”

FileTaskNextAction
- id: 稳定动作标识
- label: 用户可见文字
- action_kind: FILL_PROMPT | OPEN_FILE | RESOLVE_CLARIFICATION | CONFIRM_OPERATION | LOAD_MORE
- prompt: 仅 FILL_PROMPT 使用的建议输入
- target_ref: 安全的 document_id、managed_file_id 或 plan_id
- requires_confirmation: 是否仍需后端确认
```

`presentation` 只负责公共外壳，不复制专用结果的事实来源：

- 搜索文件仍来自 `file_search_result`。
- 分类和解析明细仍来自 `document_results`。
- 正文回答和引用仍来自 `evidence_answer_result`。
- 重命名建议仍来自 `rename_plan_result`。
- 高风险操作仍只由持久化 `OperationPlan` 驱动。

## 4. 后端实现

### 4.1 新增统一编排器

新增：

```text
apps/api/app/modules/agent/file_task_receipt.py
```

核心公开方法：

```python
def compose_file_task_presentation(result: AgentRunResult) -> FileTaskPresentation | None:
    """从已验证的 AgentRun 结果构造文件任务公共回执，不调用 Tool 或写数据库。"""
```

职责边界：

- 只读取 `intent`、`status`、`search_context`、`document_results`、OperationPlan ID 和已投影的安全业务结果。
- `document_results` 只是逐文件结果容器，不能单独作为 `READ` 的判定依据；读取回执必须来自明确的读取意图。
- `SYSTEM_FILE_LIFECYCLE` 必须由后端根据已审计的生命周期 Tool 类型和结构化结果映射为归档、分类、命名建议、文件操作或待确认事项；未声明映射的事件保持原有文本/专用回执，不得回退成“文件读取结果”。
- 不读取文件正文，不重新计算分类或搜索相关性。
- 不允许 LLM 填写数量、路径、页码、before/after 或文件变化状态。
- 无文件对象、文件范围或文件结果的普通聊天返回 `None`，继续使用文本回执。

### 4.2 接入 `UserTaskReceipt`

修改 `apps/api/app/modules/agent/user_receipt.py`：

- 新增 `presentation: FileTaskPresentation | None`。
- 在所有安全专用 payload 生成后调用统一编排器。
- `suggested_next_actions` 暂时保留用于历史兼容，新前端优先读取 `presentation.next_actions`。
- 确保投影不包含绝对路径、Tool 输入、原文全文和内部错误。

### 4.3 业务进度映射

第一阶段不新增数据库表，使用 AgentRun 状态和任务类型确定性映射：

| Agent 状态 | 用户可见阶段 |
|---|---|
| `RECEIVED` / `PLANNING` | 正在确认处理对象和任务条件 |
| `RUNNING_TOOL` | 正在读取或处理文件内容 |
| `WAITING_FOR_ASYNC_JOB` | 文件仍在处理中，完成后会继续整理结果 |
| `SUMMARIZING` | 正在整理逐文件结果和依据 |
| `WAITING_FOR_CONFIRMATION` | 计划尚未执行，等待确认 |
| `COMPLETED` | 处理完成 |
| `NEEDS_REVIEW` | 已完成可执行部分，仍有事项需要确认 |
| `FAILED` | 处理未完成 |

后续如需展示多阶段历史，再复用持久化 processing/agent events 增加 `business_progress_events`；不得把 ToolInvocation 直接投影给用户。

### 4.4 最终文本职责收敛

`graph.py::_deterministic_response` 继续负责证据回答正文和无法卡片化的业务文字。文件任务公共事实由 `presentation` 展示，避免同时在 `final_response` 和卡片中重复数量、路径和状态。

现有 `receipt_summary_service` 只能润色不含事实的简短引导语；服务失败时必须保持确定性回执，且不能改变专用结果。

## 5. 前端实现

### 5.1 新增公共外壳

新增组件：

```text
apps/web/src/features/chat/FileTaskReceiptShell.tsx
apps/web/src/features/chat/FileTaskRequestSummary.tsx
apps/web/src/features/chat/FileTaskOutcomeSummary.tsx
apps/web/src/features/chat/FileTaskChangeImpact.tsx
apps/web/src/features/chat/FileTaskNextActions.tsx
```

统一渲染顺序：

```tsx
<FileTaskReceiptShell presentation={taskResult.presentation}>
  <SpecializedReceipt />
</FileTaskReceiptShell>
```

其中 `SpecializedReceipt` 继续复用现有组件：

- `DocumentResultCard`
- `SearchResultsReceipt`
- `EvidenceAnswerReceipt`
- `RenameSuggestionReceipt`
- `OperationPlanCard`
- `FileSelectionReceipt`
- 分类和文件名冲突卡

### 5.2 收敛 `AgentRunReceipt`

`AgentRunReceipt.tsx` 不再让每个文件分支直接返回整张回执，而是：

1. 根据 `response_type` 选择中间的专用明细组件。
2. 所有文件任务统一交给 `FileTaskReceiptShell` 包裹。
3. 普通聊天、能力帮助等非文件结果继续使用当前文本展示。
4. 历史消息没有 `presentation` 时继续走旧组件，保证兼容。

### 5.3 下一步动作安全边界

- `FILL_PROMPT` 只把建议文字放入输入框，不自动发送。
- `OPEN_FILE` 必须继续调用现有鉴权预览接口。
- `CONFIRM_OPERATION` 只能引用已持久化且属于当前用户的 OperationPlan。
- 删除、覆盖、移动、重命名等动作不得因点击普通建议按钮而直接执行。

## 6. 各类文件任务的展示映射

| 任务 | 结果摘要 | 专用明细 | 文件变化说明 |
|---|---|---|---|
| 上传、解析、OCR | 成功、失败、待复核数量 | 逐文件处理状态、派生件、风险 | 原件未覆盖；列出派生件数量 |
| 读取、摘要 | 已读取文件数和摘要范围 | 文件卡、摘要正文、限制 | 原件未改变 |
| 正文问答 | 回答状态和证据文件数 | 回答、引用、页码/单元格 | 原件未改变 |
| 分类 | 已生成建议数、低置信度数 | 多标签、置信度、原文证据 | 只生成建议，未正式移动或改名 |
| 搜索、目录列举 | 明确相关、可能相关、完整性 | 逻辑路径、命中原因、证据 | 只读，原文件未改变 |
| 表格分析 | 工作表、统计项、失败项 | 确定性计算结果、单元格依据 | 原件未改变；导出件单列 |
| 改名建议 | 可执行、待复核数量 | 原名、建议名、依据 | 只是建议，尚未执行 |
| OperationPlan | 计划对象和风险 | before/after、选择框、确认按钮 | 明确“当前尚未执行” |
| 确认后的动作 | 成功、失败、跳过数量 | 每个工作副本执行状态 | 区分受管原件与工作副本变化 |
| 选择、冲突、澄清 | 需要用户决定的对象数 | 候选文件和安全逻辑位置 | 决定前不执行变更 |

## 7. 测试方案

### 后端

新增 `apps/api/app/tests/test_file_task_receipt_presentation.py`，至少覆盖：

- 每一种文件任务都生成六段公共结构所需字段。
- 普通聊天不生成 `presentation`。
- 分类、搜索、证据问答和改名建议都明确原件状态。
- OperationPlan 确认前 `operation_executed=false`。
- 确认后的文件动作区分受管原件和工作副本。
- 部分失败、全部失败、异步处理中和待确认状态的数量与文案一致。
- 同名不同路径文件保留，逻辑身份相同的重复结果只展示一次。
- 不投影绝对路径、Tool 名、内部评分、正文全文和异常堆栈。
- 历史 AgentRun 缺少新字段时仍能构造旧回执。

扩展现有测试：

- `test_user_receipt_file_search.py`
- 分类、证据回答、OperationPlan 和文档处理对应的 user receipt 测试。

### 前端

为公共外壳和 `AgentRunReceipt` 增加组件测试：

- 所有文件 `response_type` 都包含任务理解、摘要、变化说明和下一步区域。
- 专用卡片只渲染一次，不与 `final_response` 重复。
- 无 `presentation` 的历史消息继续正常展示。
- 下一步按钮不会自动执行高风险操作。
- 长文件名、深层相对路径、大批量结果和移动端宽度正常换行。
- 屏幕阅读器能识别任务状态、警告和确认按钮。

验证命令：

```bash
cd apps/api
pytest -v

cd apps/web
npm test
npm run build
```

## 8. 分阶段交付

### 阶段一：契约和公共外壳

- [x] 新增 Pydantic/TypeScript `FileTaskPresentation` 契约。
- [x] 新增后端确定性编排器和单元测试。
- [x] 新增前端 `FileTaskReceiptShell`。
- [x] 保持全部旧 payload 和历史消息兼容。

### 阶段二：覆盖只读任务

- [x] 接入搜索、目录列举、读取、摘要、证据问答和表格分析。
- [x] 统一范围、结果完整性、证据和“原文件未改变”说明。
- [x] 去除正文和卡片之间的重复展示。

### 阶段三：覆盖分析与建议任务

- [ ] 接入解析、OCR、分类、洞察和重命名建议。
- [x] 先行完成系统生命周期中的归档、后台分类、命名建议和待确认事项类型映射，并兼容既有历史 AgentRun。
- [ ] 统一派生件、建议、低置信度、失败和待复核表达。

### 阶段四：覆盖文件变更任务

- [ ] 接入 OperationPlan、确认后的动作、回收站恢复和文件名冲突。
- [ ] 逐文件展示 before/after、执行状态、失败和跳过项。
- [ ] 保证所有高风险操作仍必须确认。

### 阶段五：真实业务进度和体验验收

- [ ] 将持久化业务事件投影为可恢复的关键进度。
- [ ] 完成桌面端、窄屏、长路径、大批量和无障碍检查。
- [ ] 执行全量后端测试、前端构建和项目手工烟测。

## 9. 完成标准

- 所有涉及文件的回答都使用统一公共结构，不再只有文件搜索采用新样式。
- 用户能看懂系统处理了哪些对象、做了什么、得到什么结果以及文件是否变化。
- 批量任务继续逐文件展示，不用统计摘要替代明细。
- 文件搜索、分类和回答均展示可定位依据；没有依据时明确说明。
- 搜索和只读分析不声称修改文件；建议不声称已经执行；计划不经确认不能执行。
- 受管原件、工作副本和派生件的变化状态分开表达。
- 普通用户看不到物理路径、内部 Tool、队列、模型提示词或异常堆栈。
- 历史消息可以兼容展示，新旧回执可以渐进迁移。

## 10. 已知暂缓项

- [ ] `search_completeness` 当前仍按完整字典从 Tool 结果投影到普通用户回执，尚未增加字段白名单、类型与长度校验。该项已于 2026-08-21 评审确认；按用户明确要求本轮暂不修改，后续进行 API 安全收敛或 Tool 契约扩展前必须重新评估并补充恶意字段回归测试。

## 11. WorkBuddy 文档流视觉调整（2026-09-05）

根据 `docs/ui-preview/workbuddy-style-preview.html` 和
`docs/ui-preview/workbuddy-gallery.html`，生产聊天页采用“白色对话画布 + 文档式回执”的展示语言：

- 用户消息右对齐并使用浅蓝气泡；助手显示 File Agent 身份、头像和用户可读状态。
- 回执从大面积阴影卡片改为标题、分隔线、列表、表格、证据引用组成的文档流，减少不同功能之间的视觉跳变。
- 保留 File Agent 的状态色语义：蓝色用于操作和链接，绿色表示完成与原件保护，黄色表示需要注意或证据不足，红色表示失败。
- 首次归档继续使用分类树展示；每个文件同时展示“原名 → 现名”或“原名 → 保留原名”。命名依据不足不再显示为重命名失败。
- 无搜索结果时只呈现“请再精确或确认查询条件”的轻量文本，不渲染空结果大卡片或继续筛选按钮。
- 生产页面不再展示通用重命名建议块；明确重命名请求的成功回执仍由既有受控操作链路负责。
