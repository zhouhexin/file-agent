# LangGraph 底层框架选型决策

## 1. 决策状态

- 状态：已采纳
- 适用范围：File Agent 的 Agent Runtime 和任务编排层
- 不适用范围：文件解析、OCR、分类算法、数据库、对象存储和异步 Worker 的具体实现
- 最近确认日期：2026-07-20

## 2. 背景

File Agent 不是单次调用大模型的聊天应用。一个完整文件任务通常包含：

```text
理解用户意图
-> 确定附件或受管文件范围
-> 生成声明式 ToolPlan
-> 解析、OCR、分类或生成重命名建议
-> 持久化结果和审计信息
-> 必要时等待异步任务
-> 高风险操作等待用户确认
-> 恢复执行并生成 ChangeSet 和逐文件回执
```

这些任务具有多阶段、有状态、可分支、可失败、可等待确认和可恢复的特征。使用单个 Service 方法或自由 Agent 循环会让流程、权限和审计边界逐渐混在一起。

## 3. 决策

File Agent 使用 LangGraph 作为 Agent Runtime 的底层编排框架。

LangGraph 只负责：

- 节点和 Edge 编排。
- 本次 AgentRun 的状态传递。
- 条件分支、暂停、确认和恢复。
- Tool 调用前后的流程控制。
- 节点级日志、耗时和错误收集。

LangGraph 不负责：

- 直接读取或修改文件。
- 实现 PDF、Word、Excel 解析或 OCR。
- 保存数据库长期事实。
- 替代 PostgreSQL、Neo4j、文件存储或任务队列。
- 让 LLM 绕过 Tool 白名单执行操作。

## 4. 选择原因

### 4.1 显式表达多阶段任务

读取、分类、重命名和批量处理可以拆成职责明确的节点：

```text
collect_context
-> planning
-> tool_dispatch
-> async_job
-> evidence_or_change
-> response
```

流程结构可以直接检查和测试，避免业务逻辑退化为大量嵌套 `if/else`。

### 4.2 支持持久化状态和恢复

AgentRun 需要保存意图、文件引用、ToolPlan、逐文件结果、异步任务 ID、OperationPlan ID、ChangeSet ID 和错误。LangGraph checkpoint 可以保存这些轻量业务状态，并在用户确认或异步任务完成后继续执行。

恢复执行不能重新猜测文件范围，也不能重复执行已经成功的解析、OCR 或文件操作。

### 4.3 支持 Human-in-the-loop

文件改名、移动、删除和覆盖属于高风险操作，必须执行：

```text
生成 OperationPlan
-> WAITING_CONFIRMATION
-> 用户确认
-> 校验计划和文件版本
-> 执行 Tool
```

LangGraph 可以把等待确认建模为正式运行状态，而不是依赖前端临时变量或重新发起一条不相关任务。

### 4.4 约束 LLM 权限

LLM 只负责理解和编排：

```text
用户消息
-> LLM 输出结构化意图和候选参数
-> 后端确定性解析真实文件 ID 和目录范围
-> Planner 校验意图和 Tool 白名单
-> ToolDispatcher 校验输入 schema
-> Tool 执行
```

LLM 不直接访问文件系统、数据库写接口或 Shell，也不能跳过 OperationPlan。

### 4.5 支持分支、降级和批量失败隔离

图结构可以明确表达：

- 解析成功后进入分类或总结。
- OCR 失败后进入 `NEEDS_REVIEW`。
- 大批量任务进入异步 Worker。
- Neo4j 或 Embedding 不可用时回退规则分类。
- 单个文件失败时保留其他文件的成功结果。
- 高风险操作进入确认节点，普通读取直接生成回执。

### 4.6 便于审计和观测

每个节点统一记录进入、退出、耗时、状态和错误，并关联：

- `request_id`
- `agent_run_id`
- `user_id`
- `conversation_id`
- `tool_name`
- `document_id`

LangGraph 的运行轨迹与 AgentRun、ToolInvocation、ChangeSet 和 OperationPlan 共同构成审计链，日志不能替代这些业务记录。

## 5. 三层状态边界

LangGraph 接入必须严格区分以下三层：

### AgentGraphState

保存本次任务可持久化的轻量业务状态，例如：

- 用户消息和附件引用。
- 后端已经解析完成的 `document_ids`。
- `planner_mode`、结构化计划和 Tool 结果摘要。
- `document_results`、`result_summary` 和错误。
- AgentRun、OperationPlan、ChangeSet 和异步任务 ID。

不得保存文件全文、数据库 Session、服务对象、API key 或 LLM Client。

### AgentRuntimeContext

保存本次运行所需的依赖：

- Planner。
- Tool Registry。
- Context Loader。
- LLM Intent Service。
- 文档总结、分类、Storage、Queue 和数据库工厂。

这些对象不得进入 checkpoint 或 `graph_state_json`，请求级依赖必须通过 factory 构造。

### Persistent Stores

保存长期事实：

- PostgreSQL 中的文件、解析结果、分类建议、AgentRun、ChangeSet 和 OperationPlan。
- 文件存储中的原件与派生件。
- Neo4j 中可重建的图谱投影和向量。

Graph State 不能替代长期事实存储。

## 6. 替代方案评估

### 普通 Python Service

适合单次、同步、无确认任务。随着异步、恢复、批量失败隔离和条件路由增加，会形成难以维护的嵌套状态判断，因此不作为 Agent Runtime 主框架。

### 自由 Agent 循环

实现简单，但 Tool 选择、停止条件、权限和高风险确认过度依赖模型，不符合文件操作的安全要求。

### Celery、RQ 等任务队列

适合执行耗时任务和重试，但不负责对话意图、ToolPlan、Human-in-the-loop 和响应编排。它们是 LangGraph 的异步执行补充，不是替代品。

### 传统 BPM 或工作流引擎

适合固定企业流程，但当前文件任务包含 LLM 意图理解和动态 ToolPlan，接入成本较高。后续若出现跨天审批和复杂组织流程，可以在外层增加 BPM，不替换当前 Agent Runtime。

## 7. 代价与约束

使用 LangGraph 会增加以下成本：

- 必须维护明确的 State schema。
- 必须处理 checkpoint 版本兼容。
- 必须避免节点重复执行产生副作用。
- 必须编写节点、Edge、恢复和 Runtime Context 专项测试。
- 简单聊天请求也会经过 Agent Runtime，但可以使用轻量直接路由减少开销。

这些成本对于纯聊天应用可能过重，但对于包含批量文件处理、异步任务、OperationPlan、ChangeSet 和审计的 File Agent 是合理的。

## 8. 实施要求

1. Planner 只能生成声明式计划，不能执行副作用。
2. 所有 Tool 必须经过 ToolDispatcher、白名单和 schema 校验。
3. 文件范围由后端确定性解析，LLM 不得猜测 `document_id` 或真实目录。
4. 有副作用的节点必须具备幂等或重复执行保护。
5. `evidence_or_change` 负责统一聚合 Tool 结果，`response` 只能消费聚合结果。
6. 高风险操作必须生成 OperationPlan 并等待确认。
7. 长期事实必须写入 Persistent Stores，不能只保存在 checkpoint。
8. 图谱、模型或外部服务不可用时，主文件处理链路必须安全降级。

## 9. 重新评估条件

出现以下情况时重新评估编排方案：

- LangGraph 无法满足长期任务恢复或运行版本迁移要求。
- 业务演进为跨组织、跨天审批为主的固定 BPM 流程。
- 任务规模要求独立分布式调度平台统一接管状态机。
- LangGraph 的运行和维护成本明显高于其提供的确认、恢复和审计价值。

在重新评估前，业务 Tool、OperationPlan、ChangeSet 和 Persistent Stores 必须保持框架无关，避免替换编排框架时重写核心文件能力。
