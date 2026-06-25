# LangGraph Runtime 问题记录

本文记录当前 File Agent LangGraph Runtime 与目标“对话式文件智能体”之间的差距。

参考来源：`/Users/zhouhexin/Downloads/langgraph_state_node_edge_issues_summary.md`

更新时间：2026-06-25

## 当前已改善项

- 已接入 LLM 意图理解的第一阶段：`LLM_ENABLED=true` 时，消息入口会调用 OpenAI-compatible LLM 生成结构化 `UserIntentPlan`。
- 已保留 deterministic planner：`LLM_ENABLED=false` 时继续走确定性 Planner，保证本地开发和测试稳定。
- 已新增 `collect_context`：对话阶段可以加载附件对应的文件上下文和 `document_insights`。
- 已新增 `read-document-insights` Tool：用户要求总结或查看已上传文件基础信息时，可复用上传阶段 deterministic ingest 的结果。
- AgentRun 快照已记录 `context_documents` 和 `user_intent_plan`，便于审计和排查模型规划结果。

## P0 问题：下一阶段优先处理

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

当前没有统一的 `document_results`。

问题：

- 无法清晰表达多个文件分别处理到了哪一步。
- 无法输出逐文件分类、关键词、证据、错误、警告。
- 无法支持“12 个文件部分成功、部分失败”的回执。

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

- deterministic planner 仍需要改为处理全部附件，而不是偏向第一个文件。
- 批量任务需要 map/reduce 结构：

```text
batch_prepare
-> map_document_processing
-> aggregate_document_results
-> generate_change_report
-> response
```

- 单文件应有独立状态、错误、重试次数和结果。
- 批量处理应支持部分成功、部分失败。
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
