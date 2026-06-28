# LangGraph Runtime 问题记录

本文记录当前 File Agent LangGraph Runtime 与目标“对话式文件智能体”之间的差距。

参考来源：`/Users/zhouhexin/Downloads/langgraph_state_node_edge_issues_summary.md`

更新时间：2026-06-25

## 当前已改善项

- 已接入 LLM 意图理解的第一阶段：`LLM_ENABLED=true` 时，消息入口会调用 OpenAI-compatible LLM 生成结构化 `UserIntentPlan`。
- 已保留 deterministic planner：`LLM_ENABLED=false` 时继续走确定性 Planner，保证本地开发和测试稳定。
- 已新增 `collect_context`：对话阶段可以加载附件对应的文件上下文和 `document_insights`。
- 已新增 `read-document-insights` Tool：用户要求总结或查看已上传文件基础信息时，可复用上传阶段 deterministic ingest 的结果。
- 已支持对话触发 `extract-document-text`：用户要求读取正文、解析 PDF/Excel 或 OCR 图片时，可由 LLM intent 映射到文件解析 Tool，并写入 `document_extraction_runs` / `document_pages`。
- 已新增 `document_results` 的第一阶段实现：对话触发正文解析后，会在 AgentRun 快照中记录逐文件解析状态、字符数、分类建议、证据和错误，并用于生成逐文件回执。
- 已预置学校文件归类 JSON 配置：分类目录来自 `apps/api/app/modules/classification/taxonomies/school_file_classification.json`，当前不入库，分类建议带 `taxonomy_key` 和 `taxonomy_version`。
- 已支持多附件正文解析的顺序执行：LLM intent 引用多个 `document_id` 时，Planner 会为每个文件生成独立 `extract-document-text` 步骤；单步 Tool 异常会记录为文件级失败结果，后续文件继续处理。
- deterministic planner 已支持“读取/解析/正文/内容/OCR”类请求的多附件正文解析步骤；“读取并分类/解析并归类/只分类”意图也会优先进入正文解析路径，不再只取第一个附件或进入旧占位分类链路。
- 分类建议已改为基于 `document_pages.text_content` 的完整正文生成，不再使用 Tool 返回的 300 字 `text_preview` 作为分类依据。
- 逐文件回执已支持展示多个分类建议、置信度和证据；分类建议已写入 `document_classification_runs` 和 `document_category_suggestions`，尚未写入用户确认后的正式 `document_categories`。
- 已新增真实 ChangeSet 第一阶段：读取/读取并分类链路会写入 `change_sets` 和 `change_items`，覆盖 `TEXT_EXTRACTED`、`DOCUMENT_PAGES_CREATED`、`CATEGORY_SUGGESTED` 和 `DOCUMENT_PROCESSING_FAILED`。
- 已新增解析复用第一阶段：同一文件已有成功解析页时默认复用，不重复写 `document_extraction_runs` / `document_pages`；用户明确要求重新解析时才强制重处理，并在 ChangeSet 中区分复用和新生成。
- 已修正 ToolInvocation 审计状态：Tool 输出 `ok=false` 或 `status=FAILED` 时，调用记录状态写为 `FAILED`。
- AgentRun 快照已记录 `context_documents` 和 `user_intent_plan`，便于审计和排查模型规划结果。
- 已新增 `AgentRuntimeContext`：`planner`、`registry`、`context_loader`、`llm_intent_service` 已从 `AgentGraphState` 移出，LangGraph 节点通过 `runtime.context` 获取运行依赖。

## P0 问题：下一阶段优先处理

### 0. 运行依赖混入 AgentGraphState（已完成第一阶段修复）

第一阶段已完成：`planner`、`registry`、`context_loader`、`llm_intent_service` 已从 `AgentGraphState` 中移出，并通过 `AgentRuntimeContext` 传入 LangGraph 节点。

仍需长期保持的约束：

- 这些对象不是本次任务的业务事实，不能作为可持久化状态。
- 它们可能包含数据库会话、模型客户端、配置、缓存或函数引用，不适合 checkpoint、恢复和审计快照。
- 后续接入 LangGraph checkpoint、`interrupt()`、确认后恢复、异步任务或批量 map/reduce 时，仍必须保持 Runtime Context 与业务 State 分离。
- `graph_state_json` 仍应使用白名单快照，不得直接持久化完整 State。

改造目标：

```text
AgentGraphState       可持久化业务状态
AgentRuntimeContext   运行时依赖
Persistent Stores     数据库/对象存储/长期事实
```

具体要求：

- `AgentGraphState` 只能保存用户输入、附件引用、上下文摘要、planner_mode、tool_plan、执行结果、错误、业务对象 ID 和最终回复。
- `AgentRuntimeContext` 保存 Planner、Tool Registry、Context Loader、LLM Intent Service，以及后续的 Storage、Queue、DB Factory、Settings。
- PostgreSQL、对象存储、向量库、图数据库保存长期业务事实；State 只保存引用 ID 或轻量摘要。
- 已采用 LangGraph Runtime Context，即 `StateGraph(..., context_schema=AgentRuntimeContext)`，节点通过 `runtime.context` 获取依赖。
- `planner`、`registry`、`context_loader`、`llm_intent_service`、`db session`、LLM client、API key 不得写入 State、checkpoint 或 `graph_state_json`。

### 1. LangGraph 仍是固定线性流程

当前主流程仍接近：

```text
chat_intake
-> collect_context
-> planning
-> tool_dispatch
-> async_job_wait
-> evidence_or_change
-> response
```

问题：

- 没有条件边。
- 没有失败分支。
- 没有人工复核分支。
- 没有确认暂停与恢复分支。
- 没有异步任务轮询与恢复循环。

改造目标：

- 引入 `add_conditional_edges`。
- 增加 `validate_plan`、`failure`、`needs_review`、`waiting_for_confirmation` 等节点。
- 根据 intent、确认状态、错误状态、异步任务状态路由。

### 2. State 缺少步骤级执行状态

当前 State 主要保存全局 `tool_plan`、`tool_results`、`tool_invocations` 和 `status`。

问题：

- 无法支持单步失败重试。
- 无法支持用户确认后从指定步骤恢复。
- 无法支持 OCR、Office 转换、向量化等长任务等待。
- 无法支持单文件失败、其他文件继续成功。

建议新增字段：

```text
current_step_index
pending_steps
completed_steps
failed_steps
step_results
async_job_ids
retry_counts
resume_token
```

### 3. State 缺少文件级结果容器

当前已有第一阶段 `document_results`，覆盖对话触发 `extract-document-text` 后的单文件和多附件解析结果、轻量分类建议、文件级错误。

问题：

- 还没有覆盖所有 Tool 和所有 deterministic ingest 步骤。
- 还没有步骤级状态、实体、年份、关键词、ChangeItem 和证据跨度。
- 仅支持顺序执行的部分成功回执，还没有并发、进度事件、步骤级重试和恢复。
- 分类建议已落入 `document_classification_runs` 和 `document_category_suggestions`，但还没有用户确认、反馈 API 和正式 `document_categories` 写入流程。

建议新增：

```json
{
  "document_id": "doc_001",
  "filename": "示例.pdf",
  "processing_status": "READY",
  "artifacts": [],
  "years": [],
  "keywords": [],
  "entities": [],
  "categories": [],
  "evidence": [],
  "changes": [],
  "warnings": [],
  "errors": []
}
```

### 4. Tool dispatch 缺少确认、错误和幂等治理

当前 `tool_dispatch` 会直接遍历执行不需要确认的步骤；遇到需要确认的步骤，只设置占位 `operation_plan_id`。

问题：

- 没有真正创建 `OperationPlan`。
- 没有检查用户是否已经确认。
- 没有按 `step_id` 保存执行结果。
- 任意 Tool 异常可能中断整次运行。
- 没有幂等键，重试可能重复写入。

改造目标：

```text
prepare_steps
-> dispatch_step
-> record_step_result
-> route_after_step
```

并引入：

```text
step_id
idempotency_key
attempt_no
retry_policy
per_document_result
```

### 5. 高风险操作确认链路未闭环

当前 `PlannerStep.requires_confirmation` 和 `ToolDefinition.requires_confirmation` 都存在，但还没有形成强制执行机制。

问题：

- Planner 和 Tool 的确认属性可能不一致。
- Registry 没有强制校验 confirmed OperationPlan。
- 未来如果某个节点直接调用高风险 Tool，可能绕过确认。

改造目标：

- 高风险 Tool 的确认属性以 Tool Registry 为准。
- Planner 只能申请确认，不能降低风险。
- Dispatcher 调用高风险 Tool 前必须校验 OperationPlan。
- OperationPlan 必须已确认、未过期、目标匹配、用户匹配、操作匹配。

### 6. Response 仍不是产品级回执

当前 response 主要返回 Tool 调用摘要；LLM 文件洞察分支只返回简单文件洞察读取结果。

目标回复结构：

```text
1. 处理汇总
2. 逐文件处理与分类明细
3. 证据、关键词、实体、年份
4. 生成的派生件和 ChangeItem
5. 原始文件是否变更
6. 失败、低置信度和待确认项
7. 用户可继续执行的下一步操作
```

## P1 问题：图路由与恢复

- 增加 `validate_plan` 节点，校验 Tool 白名单、输入 schema、风险等级、确认策略、附件归属。
- 增加 `waiting_for_confirmation` 节点，支持确认暂停。
- 增加 `confirmed_dispatch` 节点，支持确认后恢复执行。
- 增加 `failure` 节点，生成安全失败回执。
- 增加 `needs_review` 节点，处理低置信度、加密文件、OCR 质量不足等场景。
- 实现 `async_job_wait` 的真实轮询、超时、失败和恢复。

## P2 问题：批量文件处理

- deterministic planner 的正文读取路径已支持全部附件；旧的 deterministic 分类占位流程仍未替换成完整真实分类链路。
- 当前对话触发 `extract-document-text` 已支持多附件顺序执行，但还没有 map/reduce 和并发控制。
- 批量任务需要 map/reduce 结构：

```text
batch_prepare
-> map_document_processing
-> aggregate_document_results
-> generate_change_report
-> response
```

- 单文件应有独立状态、错误、重试次数和结果。
- 批量处理已支持基础部分成功、部分失败回执，但还没有步骤级状态、重试次数和进度事件。
- 未来需要并发限制和进度反馈。

## P3 问题：真实 Tool 能力

当前多数 Tool handler 仍是占位实现。

后续替换方向：

```text
document-convert      -> 文件解析、OCR、Office 转换
metadata-extract      -> 元数据、关键词、实体、年份提取
multi-label-classify  -> 多标签分类和证据校验
hybrid-search         -> 全文检索 + 向量检索 + 元数据检索
evidence-answer       -> 基于证据生成回答
change-report         -> ChangeSet / ChangeItem 聚合
```

## 结构化错误模型

当前 `errors` 仍偏字符串列表，后续应统一为结构化错误：

```json
{
  "code": "OCR_QUALITY_LOW",
  "scope": "document",
  "document_id": "doc_001",
  "step_id": "ocr-001",
  "retryable": false,
  "user_action_required": true,
  "message": "扫描质量不足，无法可靠识别。"
}
```

## 建议的下一阶段开发顺序

1. 扩展 `AgentGraphState`：增加 `document_results`、步骤状态、结构化 errors。
2. 新增 `validate_plan` 节点。
3. 拆分 `tool_dispatch`，引入步骤级执行和结果记录。
4. 增加条件边：成功、失败、需要确认、需要复核。
5. 优先实现 `read-document-insights` 的多附件逐文件回执。
6. 实现 OperationPlan 确认闭环。
7. 再开始替换真实文件解析、OCR、分类和检索 Tool。

## 验收标准

### 对话式批处理

用户上传多个文件并说“帮我读取并分类这批文件”时：

- 系统处理全部附件。
- 每个文件都有独立处理状态。
- 每个文件都有关键词、分类、证据或失败原因。
- 单文件失败不影响其他文件。
- 回复明确说明原件是否被修改。

### 高风险文件操作

用户要求改名、移动、删除文件时：

- 未确认前只生成 OperationPlan。
- OperationPlan 展示 before/after、风险和目标文件。
- 用户确认后才能执行。
- 执行后生成逐文件 ChangeSet。

### 异步任务

OCR、Office 转换、批量嵌入等任务应支持：

- 任务创建。
- 状态轮询或恢复。
- 超时和失败记录。
- 完成后自动聚合结果并生成回执。
