# 自适应 Planner 与 LangGraph 规划执行循环实施方案

- 文档状态：开发中（阶段 0～6 主体已落地，阶段 7 保持 Shadow 观察）
- 编写日期：2026-07-30
- 代码审计范围：当前工作树，包含尚未提交的 LangGraph 最小条件路由改动
- 上位产品方案：`docs/automatic-organization-conversational-access-implementation-plan.md`
- 关联问题记录：`docs/langgraph-runtime-issues.md`

## 1. 目标

本方案用于完成以下能力：

```text
用户自然语言请求
-> LLM 根据受控 Catalog 生成 PlannerDecision
-> 后端校验文件范围、Tool、Skill、输入 schema、权限、风险和确认要求
-> LangGraph 按步骤执行 Tool
-> 后续步骤通过受控绑定读取前序 Tool 输出
-> Agent 观察结果后继续执行、重规划、澄清或生成回复
-> 现有 Catalog 无法满足时生成受控 CapabilitySuggestion
-> 高风险操作仍只生成或消费 OperationPlan
-> Shadow 模式比较新旧 Planner
-> 达到灰度门槛后逐步启用新 Planner
```

本次改造解决的是“自然语言请求无法穷举具体 intent”的问题，但不允许 LLM 获得文件系统、数据库或
外部服务的直接执行权。LLM 只能选择已经启用的 Skill 和白名单 Tool，并生成声明式计划。

普通用户界面只展示任务结果、文件、证据、风险、待确认项和下一步，不展示 Planner、Skill、Tool、
Catalog、绑定表达式或 Shadow 对比详情。

## 2. 统一名词

后续代码、数据库、日志、测试和文档统一使用以下名词：

| 名词 | 含义 |
|---|---|
| `PlannerDecision` | Planner 对本轮请求或本轮观察作出的受控决策 |
| `decision_type` | `TOOL_PLAN`、`DIRECT_RESPONSE` 或 `CLARIFY` |
| `ToolPlan` | `TOOL_PLAN` 决策携带的声明式执行计划 |
| `ToolStep` | ToolPlan 中的一步白名单 Tool 调用 |
| `ToolDefinition` | Tool 的名称、输入/输出 schema、权限、风险、副作用和 handler 定义 |
| `ToolResultEnvelope` | Tool 调用的统一结构化结果外壳 |
| `ToolResultBinding` | 后续 ToolStep 从已完成步骤输出中取值的受控绑定 |
| `ToolCatalog` | 当前请求实际可用的 ToolDefinition 投影 |
| `SkillManifest` | Skill 的机器可读声明，包括允许调用的 Tool 和触发提示 |
| `SkillCatalog` | 当前启用且通过校验的 SkillManifest 集合 |
| `CatalogSnapshot` | 本次 AgentRun 使用的 ToolCatalog、SkillCatalog 版本和指纹 |
| `ExecutionObservation` | 交给 Planner 的脱敏 Tool 执行摘要 |
| `CapabilitySuggestion` | 现有 Catalog 无法满足明确用户目标时生成的待管理员评审能力建议 |
| `Legacy Planner` | 当前 `DeterministicPlanner + build_plan_from_user_intent` 兼容链路 |
| `Adaptive Planner` | 使用动态 Catalog、PlannerDecision 和执行观察的新链路 |
| `Planner Shadow` | Legacy Planner 产生用户可见结果，Adaptive Planner 只做只读决策对比 |

不得把现有 `GRAPH_CLASSIFICATION_MODE=shadow` 称为 Planner Shadow。前者只比较图谱增强前后的分类候选，
与本方案的 Planner 双轨对比是两套独立机制。

## 3. 当前完成度审计

### 3.1 总结

截至 2026-07-30 当前工作树，10 项均已开始实现：

- 已完成：4 项。
- 部分完成：6 项。
- 未开始：0 项。

“存在字段、占位接口或静态清单”不等于能力完成。完成状态以运行时强制校验、持久化审计和自动化测试
是否同时存在为准。

### 3.2 逐项结论

| 序号 | 项目 | 状态 | 当前证据 | 主要缺口 |
|---:|---|---|---|---|
| 1 | 修复 LLM 空响应和确定性降级 | 已完成 | 空正文、HTML、非法 JSON 均转为 `LLMResponseError`；Adaptive 失败先回退 Legacy，再回退确定性 Planner | 保留生产降级率监控 |
| 2 | 定义 PlannerDecision 和 Tool 输出 schema | 部分完成 | 已新增独立 `PlannerDecision`、`ToolPlan`、`ToolStep`、`ToolResultEnvelope`、`ToolError`，Registry 强制校验 input/output model | 部分旧 Tool 仍使用迁移期 `GenericToolOutput`，需按核心 Tool 清单继续收敛严格业务 schema |
| 3 | 实现动态 Tool/Skill Catalog | 已完成 | 请求级 Registry 动态投影 Tool；13 个 Skill 已有 `manifest.json`；启动与请求时交叉校验；AgentRun 保存版本和指纹 | 后续如增加后台启停，只能通过受审计发布流程 |
| 4 | 实现 Tool 结果绑定解析器 | 已完成 | 已实现安全点分字段绑定、成功来源校验、受信任字段保护、数组上限和绑定后 Tool 输入二次 schema 校验 | 后续可按具体 Tool 补充更细类型提示 |
| 5 | 重构 LangGraph 为规划执行循环 | 部分完成 | Dispatcher 每次只执行一个步骤；支持前序结果绑定、继续原计划、最多 3 轮规划、5 次调用、重复拒绝和确认暂停 | 异步恢复仍是同步占位；尚未把选择、绑定、记录拆成独立图节点，也未接数据库 checkpointer |
| 6 | 接入 DIRECT_RESPONSE 和 CLARIFY | 已完成 | 三分支已进入独立 PlannerDecision；文件事实不能通过 DIRECT_RESPONSE 绕过 Tool；缺少唯一范围进入 CLARIFY | 继续用回放集监测不必要澄清 |
| 7 | 实现分类证据读取能力 | 部分完成 | 已新增当前版本 EvidenceReader；优先活动工作副本当前版本，只取最新成功运行；EvidenceAnswer 复用该服务 | 仍需补齐失败运行、回收站、同名文件和无 quote 的完整测试矩阵，并收敛严格 output model |
| 8 | 缩减确定性关键词路由 | 部分完成 | 正常 LLM 主路径只保留重命名、确认、分类等安全 preflight；普通搜索、总结、解释、能力咨询和表格语义交给 Catalog Planner | Legacy/故障降级仍保留 `_has_*`，需在 Shadow 回放达标后再删除重叠规则 |
| 9 | 调整后台 LexRank 摘要 Provider | 已完成 | 后台双摘要默认 CPU-only `Jieba + LexRank`；全局 LLM 开关不隐式外发；已有配置测试 | 继续保护最终回答必须回到 Evidence |
| 10 | Shadow 模式对比新旧 Planner 后再默认启用 | 部分完成 | 已实现 `legacy/shadow/enabled`、只读双决策、对比表、稳定分桶、灰度开关和失败回退；Shadow 不执行第二次 Tool | 尚未完成生产观察期、离线回放指标报表和 5%→100% 灰度，因此不得默认启用 Adaptive |

### 3.3 现有实现不能算完整规划执行循环的原因

改造前最小图为：

```text
planning
-> tool_dispatch：遍历并执行本轮全部 steps
-> observe_tool_result：整批结果完成后观察
-> 可选 replan，总规划最多 3 轮
```

目标循环必须是：

```text
planning
-> validate decision and plan
-> select next step
-> resolve bindings
-> dispatch one step
-> validate and record result
-> observe
-> continue current plan / replan / clarify / wait confirmation / finalize
```

只有后者才能安全支持“先检索文件，再把检索结果交给证据回答”“先读取分类证据，再解释为什么分类”
等不能预先写死全部 Tool 输入的自然语言任务。

## 4. 不变安全边界

本方案不得削弱以下规则：

1. LLM 不直接访问文件系统、Shell、数据库写接口或外部服务。
2. LLM 只能引用 CatalogSnapshot 中存在并已启用的 Tool 和 Skill。
3. Tool 输入经过绑定后必须再次通过该 Tool 的 Pydantic 输入 schema。
4. Tool 输出必须通过该 Tool 的 Pydantic 输出 schema，验证失败按 Tool 失败记录。
5. 文件范围由后端解析；LLM 不得猜测“刚上传”“上一个文件”或自然语言目录的真实 ID。
6. Tool Registry 的风险等级和确认要求高于 Planner 声明，Planner 不能降低风险。
7. 重命名、移动、复制、覆盖、删除、大批量导出和外发仍必须通过已确认的 OperationPlan。
8. 有副作用 Tool 失败后不得由 Planner 盲目重试。
9. 同一 AgentRun 内相同 `tool_name + schema 规范化 input` 不得重复执行。
10. Planner 观察中不得包含正文、OCR 全文、绝对路径、密钥、JWT、数据库会话或模型客户端。
11. 后台摘要默认保持本地 CPU-only `Jieba + LexRank`，不能因 Planner 或全局 LLM 开关而隐式外发。
12. 普通用户回复不暴露内部 Skill、Tool、调用预算或 Shadow 对比信息。
13. CapabilitySuggestion 只是待评审建议，不能自动注册 Tool、启用 Skill、生成代码或扩大权限。

## 5. 目标数据契约

### 5.1 PlannerDecision

建议在 `apps/api/app/modules/agent/planner_contracts.py` 定义独立模型：

```json
{
  "decision_type": "TOOL_PLAN",
  "intent": "EVIDENCE_ANSWER",
  "user_goal": "解释这个文件为什么被分到学生工作",
  "selected_skill_ids": ["evidence-answer", "document-classification"],
  "scope": {
    "document_ids": ["document-uuid"],
    "source": "current_message",
    "requires_backend_resolution": false
  },
  "tool_plan": {
    "plan_id": "plan-uuid",
    "steps": []
  },
  "capability_suggestions": [],
  "direct_response": null,
  "clarification": null,
  "confidence": 0.92
}
```

强制校验：

- `TOOL_PLAN` 必须有非空 `tool_plan`，不得有 `direct_response`。
- `DIRECT_RESPONSE` 必须有非空 `direct_response`，不得包含 ToolStep。
- `CLARIFY` 必须有一个最关键的 `clarification.question`，不得包含会产生副作用的 ToolStep。
- `selected_skill_ids` 必须存在于当前 SkillCatalog。
- `scope.document_ids` 必须由后端授权范围校验；模型自报 ID 不能直接采用。
- `intent` 用于审计和统计，不再作为执行能力的唯一枚举开关。
- `capability_suggestions` 只能描述未满足的用户目标，不能把建议名称当成可调用 Tool 或 Skill。

第一阶段保留 `UserIntentPlan` 作为 LLM 适配层，进入 LangGraph 前统一转换为 `PlannerDecision`。完成 Shadow
并默认启用 Adaptive Planner 后，再评估删除兼容字段。

### 5.2 ToolStep

```json
{
  "step_id": "step-2",
  "skill_id": "evidence-answer",
  "tool_name": "evidence-answer",
  "literal_input": {
    "question": "为什么被分到学生工作"
  },
  "bindings": [
    {
      "target_field": "document_ids",
      "source_step_id": "step-1",
      "source_field": "documents.document_ids"
    }
  ],
  "requires_confirmation": false,
  "expected_output_kind": "evidence_answer"
}
```

`ToolStep` 不再允许一个自由 `input` 同时混合模型字面量和前序结果模板。字面量放在 `literal_input`，
动态值只能通过 `bindings` 表达。

### 5.3 ToolDefinition

现有 ToolDefinition 至少扩展为：

```text
name
version
description
input_model
output_model
side_effects
risk_level
requires_confirmation
allowed_roles
allowed_skill_ids
writes
failure_strategy
retry_policy
handler
enabled
```

关键规则：

- `output_model` 必填，不再统一返回 `{"type": "object"}`。
- `requires_confirmation` 和 `risk_level` 由 Registry 强制执行。
- `allowed_skill_ids` 防止某个 Skill 越权调用与其无关的 Tool。
- `retry_policy` 的默认值为 `never`；只读、幂等 Tool 才能显式声明有限重试。
- `handler` 仍由代码注册，不允许从 Skill 文档、LLM 输出或数据库动态加载 Python 代码。

### 5.4 ToolResultEnvelope

所有 Tool 统一返回：

```json
{
  "tool_name": "read-document-classifications",
  "tool_version": "1",
  "invocation_id": "invocation-uuid",
  "step_id": "step-1",
  "status": "COMPLETED",
  "ok": true,
  "output": {},
  "error": null,
  "changeset_id": null,
  "operation_plan_id": null,
  "async_job_id": null,
  "replan_signal": null
}
```

状态统一为：

```text
COMPLETED
FAILED
WAITING_FOR_ASYNC_JOB
WAITING_FOR_CONFIRMATION
NEEDS_REVIEW
```

业务 handler 可以先返回具体 output model，Registry 负责封装 ToolResultEnvelope、写 ToolInvocation 并投影
兼容字段。`ok=false` 或 `status=FAILED` 必须使 ToolInvocation.status 为 `FAILED`。

### 5.5 ToolResultBinding

绑定只支持结构化字段引用，不支持字符串模板执行、Python `eval`、Shell、SQL、任意 JMESPath 或带通配符
JSONPath。

第一版字段规则：

- `source_step_id` 必须指向本 AgentRun 已完成的步骤。
- `source_field` 只能由字母、数字、下划线和点号组成。
- 每一段必须能在 source Tool 的 `output_model` schema 中解析。
- `target_field` 必须存在于 target Tool 的 `input_model` schema。
- 禁止绑定覆盖 `user_id`、`conversation_id`、`agent_run_id`、确认令牌、角色、工作区和本地路径。
- 解析完成后对合并输入执行完整 `input_model.model_validate()`。
- 缺字段、类型不匹配、来源步骤失败或数组超限时进入结构化 `BINDING_VALIDATION_FAILED`，不得调用 Tool。

建议新增 `ToolResultBindingResolver`，输入只包含 CatalogSnapshot、已完成步骤结果和目标 ToolStep，不读取
文件系统或数据库。

## 6. 动态 Tool/Skill Catalog

### 6.1 “动态”的准确含义

本项目的动态 Catalog 是“从当前已注册、已启用、已授权并通过 schema 校验的能力中，为每次 AgentRun
生成可审计快照”，不是让 LLM 动态创建或加载代码。

允许动态变化：

- Tool 版本、启停状态和当前角色可见范围。
- Skill 版本、启停状态和允许 Tool 集合。
- 可选 Provider 是否可用。
- 当前部署支持的文件类型和能力。

不允许动态变化：

- LLM 写入 Tool handler。
- 从上传文件、网页正文或 SKILL.md 中执行代码。
- 未经发布流程自动启用候选 Skill。
- 绕过代码白名单调用 Shell、SQL、文件系统或外部接口。

### 6.2 ToolCatalog

ToolCatalog 由请求级 ToolRegistry 的 ToolDefinition 生成，不再维护一份与真实 Registry 可能漂移的
平行 Tool 名称列表。Catalog 对 Planner 只投影：

```text
name
version
description
input_schema
output_schema
risk_level
side_effects
requires_confirmation
allowed_skill_ids
failure_strategy
```

handler、数据库会话、用户 ID、本地路径和密钥不得进入 CatalogSnapshot 或 LLM 输入。

### 6.3 SkillCatalog

继续保留每个 `skills/<skill-name>/SKILL.md` 作为人类规则文档，并新增机器可读
`skills/<skill-name>/manifest.json`。不应依赖解析 Markdown 标题来决定运行时权限。

SkillManifest 至少包含：

```json
{
  "id": "evidence-answer",
  "version": "1",
  "status": "ACTIVE",
  "description": "基于已验证证据回答文件问题",
  "trigger_hints": ["总结", "解释", "比较", "为什么"],
  "allowed_tools": ["evidence-answer", "read-document-classifications"],
  "required_capabilities": ["document_read"],
  "risk_ceiling": "medium"
}
```

启动时执行交叉校验：

1. manifest 对应的 `SKILL.md` 必须存在。
2. `allowed_tools` 必须全部存在于 ToolRegistry。
3. ToolDefinition 的 `allowed_skill_ids` 与 SkillManifest 必须互相一致。
4. ACTIVE Skill 不得引用禁用 Tool。
5. Skill 风险上限不得低于其 Tool 的真实风险。
6. Catalog 校验失败时服务启动失败或禁用有问题的 Skill，不能静默交给 LLM。

### 6.4 CatalogSnapshot

每次 AgentRun 保存：

```text
catalog_version
catalog_fingerprint
enabled_skill_ids
enabled_tool_names
created_at
```

`catalog_fingerprint` 由排序后的 Skill/Tool 名称、版本和 schema 哈希生成。State 只保存轻量身份，完整 Catalog
由运行时服务按指纹读取，不把大段 schema 重复写入 checkpoint。

### 6.5 CapabilitySuggestion 与管理员清单

当 LLM 根据 CatalogSnapshot 判断现有 Tool 和 Skill 无法完整完成用户明确提出的目标时，可以在
PlannerDecision 中附带 `capability_suggestions`：

```json
{
  "suggestion_kind": "CAPABILITY",
  "title": "支持从加密 PDF 读取正文",
  "missing_capability": "读取用户已提供密码的加密 PDF",
  "reason": "当前 Catalog 只有普通 PDF 解析能力",
  "expected_inputs": ["document_id", "user_supplied_password_reference"],
  "expected_outputs": ["document_pages", "processing_receipt"],
  "related_skill_ids": ["file-ingest"],
  "confidence": 0.88
}
```

建议生成规则：

1. 必须对应本轮用户明确目标，不能根据文件正文中的命令生成建议。
2. LLM 只能描述能力缺口，不得生成 handler 代码、Shell、SQL、文件路径或外部请求。
3. 不得把建议中的 Tool/Skill 名称加入当前 ToolPlan。
4. 后端必须重新查询 CatalogSnapshot，确认没有同名或等价能力。
5. 已存在但被管理员禁用的 Tool/Skill 标记为 `EXISTING_DISABLED`，不得伪装成新能力。
6. 建议正文必须脱敏，不保存文件正文、OCR 全文、绝对路径、密钥、密码或大段 prompt。
7. 使用 `user_goal + missing_capability + catalog_fingerprint` 生成去重指纹；重复建议只增加出现次数和最近出现时间。
8. 低置信度、无明确用户目标或仅由模型非法 Tool 名触发的建议不落库，只记录验证失败指标。
9. Planner Shadow 生成的建议只参与对比，不写数据库。
10. 建议不能自动变成 SkillCandidate，更不能自动发布 ACTIVE Skill。

建议持久化必须经过内部白名单 `capability-suggestion-record` Tool。该 Tool 由 Graph 在
PlannerDecision 通过校验后确定性调用，不暴露给 LLM 的 ToolCatalog，也不允许 Planner 主动选择。
这样数据库写入仍经过 ToolInvocation、schema、权限和审计边界。

建议新增 `capability_suggestions` 表：

```text
id
suggestion_kind
title
missing_capability
reason
expected_inputs_json
expected_outputs_json
related_skill_ids_json
confidence
deduplication_fingerprint
occurrence_count
first_agent_run_id
latest_agent_run_id
requested_by_user_id
catalog_fingerprint
status
review_note
reviewed_by
reviewed_at
created_at
updated_at
```

状态统一为：

```text
NEW
UNDER_REVIEW
ACCEPTED
REJECTED
MERGED
IMPLEMENTED
```

管理员接口：

```text
GET  /api/admin/capability-suggestions
GET  /api/admin/capability-suggestions/{suggestion_id}
POST /api/admin/capability-suggestions/{suggestion_id}/review
```

管理员页面统一使用 `/admin/capability-suggestions`，展示建议标题、缺失能力、出现次数、关联 Skill、
Catalog 版本、首次/最近出现时间和评审状态。`ops`、`admin` 可以查看和评审；只有 `admin` 可以标记
`ACCEPTED` 或 `IMPLEMENTED`。评审操作只改变建议状态，不自动创建 Tool、修改 SkillManifest 或启用能力。

## 7. LangGraph 目标流程

### 7.1 节点

```text
chat_intake
-> collect_context
-> build_catalog_snapshot
-> planning
-> validate_decision
   -> direct_response
   -> clarification_response
   -> select_next_step
-> resolve_bindings
-> dispatch_step
-> record_step_result
-> observe_step_result
   -> select_next_step
   -> planning
   -> waiting_for_async_job
   -> waiting_for_confirmation
   -> needs_review
   -> evidence_or_change
-> response
```

### 7.2 单步执行

`dispatch_step` 每次只执行一个 ToolStep。这样才能：

- 在执行前验证前序结果绑定。
- 在单步后判断是否继续原计划。
- 精确记录 `step_id`、attempt、结果、失败和幂等键。
- 遇到高风险步骤时暂停，而不是跳过后继续执行后续步骤。
- 确认后从 checkpoint 恢复到同一步。

### 7.3 继续原计划与重规划的区别

- 前序步骤成功且下一个步骤的绑定可解析：继续原 ToolPlan，不消耗规划轮数。
- Tool 明确返回 `replan_signal`，或原计划因可恢复的业务结果不再适用：调用 Planner 重新决策。
- Tool 失败：默认停止当前依赖分支，不自动重规划；只有只读 Tool 声明允许降级且观察策略有明确分支时处理。
- 缺少用户选择、目录歧义、文件同名或低置信度：进入 CLARIFY 或 NEEDS_REVIEW，不让模型猜测。

### 7.4 执行预算

第一版继续采用：

```text
最大规划轮数：3
最大实际 Tool 调用数：5
最大重规划次数：2
相同 Tool + 规范化输入：最多调用 1 次
```

在完整循环稳定后把数值移动到配置：

```text
AGENT_MAX_PLANNING_ROUNDS=3
AGENT_MAX_TOOL_CALLS=5
AGENT_MAX_REPLANS=2
```

预算限制的是一次 AgentRun 的自主执行范围，不限制用户的自然语言表达。批量文件处理应由一个受控异步
Tool 创建 Job，不能靠提高 Planner Tool 调用预算逐文件同步执行。

## 8. DIRECT_RESPONSE 和 CLARIFY 收敛

当前最小分支保留，但迁移到 PlannerDecision：

### 8.1 DIRECT_RESPONSE

只允许：

- 不需要文件事实、业务数据或外部状态的普通交流。
- 系统已有固定公开说明可以直接回答的情况。
- CatalogSnapshot 已证明能力不存在时，诚实说明当前不支持并告知已生成待管理员评审建议。

禁止：

- 回答文件正文、分类、搜索结果、数字、日期、金额或处理状态。
- 声称文件已经上传、分类、重命名、移动或删除。
- 代替失败 Tool 编造结果。

### 8.2 CLARIFY

只询问阻止安全执行的最小信息，例如：

- 多个同名文件中选择哪一个。
- 受管目录候选不唯一时提供完整相对路径。
- 文件范围为空或无法从后端上下文唯一解析。
- 高风险操作的 before/after 信息不足。

当后端已经能唯一解析范围时，LLM 不得用 CLARIFY 拒绝执行一个合法低风险计划。

## 9. 分类证据读取能力

### 9.1 改造现有 Tool

优先增强 `read-document-classifications`，不新增功能重复的平行 Tool。新增严格输入选项：

```text
document_ids
version_scope = CURRENT_WORKING_COPY
include_evidence = true
include_explanation_context = true
```

输出定义独立 Pydantic model，至少包含：

```text
document_id
document_version_id
working_copy_id
filename
classification_run_id
taxonomy_key
taxonomy_version
classifier_version
classification_basis
summary_status
categories[]
categories[].category_id
categories[].category_path
categories[].confidence
categories[].status
categories[].source
categories[].evidence_items[]
```

### 9.2 当前版本解析规则

1. 校验 Document 属于当前用户可访问范围。
2. 优先解析活动工作副本 `WorkingCopy.status=ACTIVE` 的 `current_version_id`。
3. 没有工作副本时，按上传附件链路解析当前 DocumentVersion；不得使用文件名猜版本。
4. 只读取 `DocumentClassificationRun.status=COMPLETED`。
5. 只接受 `DocumentCategorySuggestion.document_version_id` 等于当前版本的建议。
6. 同版本按运行创建时间选择最新成功运行，再按 rank 和 confidence 返回建议。
7. 找不到当前版本建议时返回明确 `NO_CURRENT_CLASSIFICATION_EVIDENCE`，不得回退展示历史版本分类。

### 9.3 分类解释

“为什么被分类到某目录”必须先读取上述结构化分类事实。回复只能使用：

- category path。
- 分类置信度和状态。
- 原文 evidence_items 中真实 quote、页码、Sheet、signals。
- 分类主题摘要只作为候选召回背景，不能作为最终证据。

非“其他”分类没有可定位 quote 时必须显示 `NEEDS_REVIEW`，不能让 LLM补写依据。

`EvidenceAnswerService._public_payload` 的分类标签也必须复用同一当前版本解析服务，禁止直接查询一个 Document
的全部历史 `DocumentCategorySuggestion`。

## 10. 缩减确定性关键词路由

### 10.1 保留内容

确定性逻辑继续负责：

- 空任务文字和附件提交边界。
- 文件 ID、附件批次、工作区、用户权限和活动版本解析。
- 高风险操作识别与 OperationPlan 强制要求。
- 确认回复与既有 OperationPlan 的精确匹配。
- 完整文件名、稳定 ID、目录唯一性等确定性实体提取。
- LLM 不可用时的降级 Planner。

### 10.2 从 LLM 前置路由移除的内容

以下语义不再由 `_deterministic_preflight_plan` 在 LLM 前大范围截获：

- 普通文件搜索、总结、解释和比较。
- 分类查询与“为什么这样分类”。
- 能力咨询。
- 普通表格分析意图。
- 仅依赖“读取、查找、内容、为什么”等词组的路由。

第一阶段不立即删除全部 `_has_*` 函数，而是：

1. 建立明确的 `SAFETY_PREFLIGHT_INTENTS` 白名单。
2. 其余函数只服务 Legacy Planner 和 LLM 失败降级。
3. Shadow 达标后删除无测试价值的重叠关键词规则。
4. 保留实体提取函数，但与“决定调用哪个 Tool”的语义路由分离。

这样既能减少 LLM 正常工作时的关键词覆盖，又不会在模型网关故障时让消息入口完全不可用。

## 11. 后台 LexRank 摘要 Provider

该项当前已经完成，不再重写算法。后续改造只做兼容性保护：

1. 保持 `DOCUMENT_SUMMARY_PROVIDER=extractive`。
2. 保持 `CLASSIFICATION_SUMMARY_PROVIDER=extractive`。
3. 保持固定候选上限和 CPU-only `Jieba + LexRank`。
4. `LLM_ENABLED=true` 不得改变后台 Provider。
5. 只有两个后台 Provider 显式设置为 `llm` 才允许调用模型。
6. `CHAT_DOCUMENT_SUMMARY_PROVIDER` 和 `EVIDENCE_ANSWER_PROVIDER` 继续独立。
7. Planner 只接收摘要 ID、版本、状态和安全短摘要，不接收后台全文。
8. 最终问答和分类解释仍回到当前版本原文 Evidence，不能只基于 LexRank 摘要回答。

本阶段新增的 Catalog 中不把 LexRank 摘要服务暴露为任意用户可调用的文件写 Tool。它继续作为导入、解析和
分类链路内部的受控 Provider。

## 12. Planner Shadow 与默认启用

### 12.1 配置

新增独立配置：

```text
ADAPTIVE_PLANNER_MODE=shadow
ADAPTIVE_PLANNER_ROLLOUT_PERCENT=0
ADAPTIVE_PLANNER_SHADOW_SAMPLE_PERCENT=100
ADAPTIVE_PLANNER_SCHEMA_VERSION=planner-decision-v1
```

模式语义：

| 模式 | 用户可见执行 | 对比行为 |
|---|---|---|
| `legacy` | Legacy Planner | 不运行 Adaptive Planner |
| `shadow` | Legacy Planner | Adaptive Planner 只生成并校验决策，不执行 Tool |
| `enabled` | 按稳定分桶使用 Adaptive Planner | 未命中分桶仍走 Legacy；Adaptive 决策校验失败时安全降级 |

### 12.2 Shadow 禁止事项

- 不得因为双 Planner 产生两次 Tool 调用。
- 不得创建重复 ChangeSet、OperationPlan、异步 Job 或外部请求。
- 不得把 Shadow 的 direct response 展示给用户。
- 不得向 Shadow Planner 提供超过正常 Planner 权限的正文或文件路径。

### 12.3 对比记录

建议新增 `planner_shadow_comparisons` 表：

```text
id
agent_run_id
legacy_decision_type
adaptive_decision_type
legacy_intent
adaptive_intent
legacy_skill_ids_json
adaptive_skill_ids_json
legacy_tool_names_json
adaptive_tool_names_json
scope_match
risk_match
confirmation_match
adaptive_validation_status
adaptive_error_code
catalog_fingerprint
schema_version
created_at
```

不得保存正文、完整 prompt、API key、JWT、绝对路径或未经授权的 Tool 输入。

### 12.4 指标

至少统计：

- PlannerDecision schema 通过率。
- 未知 Skill、未知 Tool、输出 schema 不匹配次数。
- 文件范围一致率。
- 高风险识别和确认要求一致率。
- DIRECT_RESPONSE 文件事实越权拦截次数。
- CLARIFY 率和不必要澄清率。
- Tool 集合一致率和步骤依赖有效率。
- LLM 空响应、超时、非法 JSON 的降级率。
- Adaptive Planner 平均延迟和 token 使用量。

### 12.5 默认启用门槛

所有门槛同时满足才进入 `enabled`：

1. 未知或禁用 Tool 调用为 0。
2. 未授权文件范围执行为 0。
3. 高风险确认降级为 0。
4. Tool 输入和输出 schema 未拦截的越权为 0。
5. 回放基准集覆盖搜索、总结、分类解释、表格、重命名建议、删除确认、歧义澄清和普通对话。
6. Shadow 连续观察期内 schema 通过率不低于 99%。
7. 关键安全意图的 scope/risk/confirmation 一致率为 100%。
8. 自动化测试、后端全量测试和前端构建通过。
9. 管理员可以一键回退 `legacy`，回退不要求数据库回滚。

灰度顺序：

```text
legacy
-> shadow 100%
-> enabled 5%
-> enabled 25%
-> enabled 50%
-> enabled 100%
-> Adaptive Planner 成为默认值
```

分桶使用稳定的用户或会话哈希，不按单次随机数切换，避免同一会话前后行为漂移。

## 13. 分阶段开发顺序

### 阶段 0：固化现有最小修复

目标：把已完成的空响应降级、DIRECT_RESPONSE、CLARIFY 和最多两次重规划作为兼容基线。

任务：

1. 保留并复核当前尚未提交的最小 LangGraph 改动。
2. 固化空、HTML、非法 JSON、超时和连接失败的降级测试。
3. 固化直接回复不得回答文件事实的安全测试。
4. 固化最多 3 轮规划、Tool 调用预算、重复调用拒绝和有副作用 Tool 不盲目重试测试。
5. 将当前常量限制记录到运行日志，暂不新增管理页面。

完成门槛：当前最小链路全量测试通过，行为不再因底层 JSON 异常返回 ASGI 500。

### 阶段 1：定义契约与 Catalog

目标：先建立稳定边界，再改图。

任务：

1. 新增 `PlannerDecision`、`ToolPlan`、`ToolStep`、`ToolResultBinding`。
2. 新增 `ToolResultEnvelope` 和结构化 ToolError。
3. 为每个 Tool 增加 output model；允许分批迁移，但未迁移 Tool 不得进入 Adaptive Planner Catalog。
4. 新增 ToolCatalogService、SkillCatalogService 和 CatalogSnapshot。
5. 为 13 个现有 Skill 创建并校验 `manifest.json`。
6. 启动时执行 Skill/Tool 交叉校验。
7. AgentRun 持久化 Catalog 指纹和 Planner schema 版本。
8. 新增 CapabilitySuggestion 契约、去重服务、内部记录 Tool 和数据库迁移。
9. 新增管理员建议清单 API、`/admin/capability-suggestions` 页面和角色校验。

完成门槛：Catalog 中每个 Tool 都有真实输入和输出 schema；任一未知 Skill/Tool 在执行前被拒绝。

### 阶段 2：实现 Tool 结果绑定解析器

目标：让后续步骤安全消费前序结果。

任务：

1. 实现无副作用 `ToolResultBindingResolver`。
2. 增加来源步骤、来源字段、目标字段和类型校验。
3. 增加受信任字段覆盖保护。
4. 增加数组长度、空值策略和失败来源处理。
5. 增加 `BINDING_VALIDATION_FAILED` 结构化错误。
6. 建立“搜索结果 document_ids -> 证据回答输入”的端到端测试。

完成门槛：不存在任何 `eval` 或自由模板替换；错误绑定不会调用目标 Tool。

### 阶段 3：重构 LangGraph 为步骤级规划执行循环

目标：将整批 dispatch 改为单步循环。

任务：

1. 增加 `validate_decision`、`select_next_step`、`resolve_bindings`。
2. 将 `tool_dispatch` 拆为 `dispatch_step` 和 `record_step_result`。
3. 增加 `current_step_id`、`step_states`、`completed_step_ids`、`failed_step_ids`。
4. 区分继续当前计划和重新规划。
5. 增加 waiting for async、waiting for confirmation、needs review 分支。
6. 保留全局预算和重复调用签名。
7. 给有副作用调用增加 idempotency key。
8. 为确认暂停和恢复预留 checkpoint 接口；本阶段可先保持同步最小实现。

完成门槛：至少支持“两步有绑定的只读任务”、中间澄清、单步失败停止依赖分支和高风险确认暂停。

### 阶段 4：接入分类证据读取

目标：可靠回答“为什么这样分类”。

任务：

1. 为 `read-document-classifications` 增加真实 output model。
2. 建立当前版本最新成功分类解析服务。
3. 返回可定位 evidence_items。
4. EvidenceAnswerService 复用同一解析服务。
5. 为旧版本、失败运行、无证据、回收站和同名文件增加测试。
6. 将“分类解释”作为 Tool 绑定用例接入 Adaptive Planner。

完成门槛：任何分类标签和解释都来自当前版本；历史建议不能污染当前文件卡。

### 阶段 5：缩减确定性关键词路由

目标：LLM 正常可用时由 Adaptive Planner 根据 Catalog 选择能力，确定性逻辑回归安全和降级职责。

任务：

1. 建立安全 preflight 白名单。
2. 从正常 LLM 主路径移除大部分业务关键词抢占。
3. 保留 Legacy Planner 作为降级，不立即删除规则。
4. 为每次 legacy preflight/fallback 记录结构化原因。
5. 建立自然语言变体回放集，覆盖当前 `_has_*` 规则保护的业务。

完成门槛：普通业务语义不再依赖必须命中固定词组；LLM 不可用时核心文件任务仍有可解释降级。

### 阶段 6：LexRank 兼容回归

目标：确保 Planner 改造不改变后台摘要隐私边界。

任务：

1. 验证上传、导入、首次命中文件仍默认使用本地 LexRank。
2. 验证两个后台 Provider 必须显式设为 `llm` 才外发。
3. 验证摘要缓存键、模型身份和候选上限不变。
4. 验证 Planner 观察不包含摘要全文或 document_pages。
5. 验证最终问答仍引用 EvidenceSpan 或分类 evidence_items。

完成门槛：`LLM_ENABLED=true` 单独开启时，后台摘要模型调用次数仍为 0。

### 阶段 7：Planner Shadow、灰度与默认启用

目标：用真实请求只读比较新旧 Planner，再逐步切换。

任务：

1. 实现 `legacy/shadow/enabled` 三种模式。
2. 新增 comparison 持久化和结构化日志。
3. 建立离线回放和在线 Shadow 指标。
4. Shadow 只生成决策，不调用 Tool。
5. 按稳定哈希执行 5%、25%、50%、100% 灰度。
6. 达到门槛后将 Adaptive Planner 设为默认值。
7. 保留即时回退 Legacy 的配置能力。

完成门槛：安全指标全部满足，自动化和手工烟测通过，且回退开关经过演练。

## 14. 预计代码与数据库改动

建议新增：

```text
apps/api/app/modules/agent/planner_contracts.py
apps/api/app/modules/agent/tool_contracts.py
apps/api/app/modules/agent/catalog.py
apps/api/app/modules/agent/binding_resolver.py
apps/api/app/modules/agent/shadow_service.py
apps/api/app/modules/agent/capability_suggestion_service.py
apps/api/app/modules/classification/evidence_reader.py
skills/*/manifest.json
apps/api/alembic/versions/*_add_capability_suggestions.py
apps/api/alembic/versions/*_add_planner_shadow_comparisons.py
apps/web/src/features/admin/CapabilitySuggestionsPage.tsx
```

建议重点修改：

```text
apps/api/app/modules/agent/graph.py
apps/api/app/modules/agent/state.py
apps/api/app/modules/agent/service.py
apps/api/app/modules/agent/repository.py
apps/api/app/modules/agent/planner.py
apps/api/app/modules/agent/tool_registry.py
apps/api/app/modules/llm/schemas.py
apps/api/app/modules/llm/service.py
apps/api/app/modules/evidence_answer/service.py
apps/api/app/core/config.py
.env.example
README.md
docs/runbook.md
```

迁移时不要一次性重写所有 handler。先给进入 Adaptive Catalog 的核心只读 Tool 增加 output model：

```text
hybrid-search
read-document-classifications
evidence-answer
read-document-insights
managed-file-list
managed-file-search
profile-spreadsheet
analyze-spreadsheet
```

再迁移会产生 ChangeSet、OperationPlan 或异步 Job 的 Tool。

## 15. 测试矩阵

### 15.1 契约

- 三种 PlannerDecision 的互斥字段。
- 未知 Skill、未知 Tool、禁用 Tool。
- Tool 输入、输出 schema 失败。
- Planner 试图降低 Tool 风险或确认要求。
- CatalogSnapshot 指纹稳定且配置变化后更新。
- 不存在的能力生成脱敏 CapabilitySuggestion，不能进入当前 ToolPlan。
- 已有等价能力、禁用能力、低置信度建议和重复建议的校验与合并。
- 普通用户不能访问建议清单，ops/admin 评审权限符合约束。

### 15.2 绑定

- 标量、列表和嵌套字段的合法绑定。
- 来源步骤不存在、失败或未完成。
- 来源字段不存在或类型不匹配。
- 绑定覆盖受信任字段。
- 超长数组和重复文档 ID。
- 绑定失败时目标 Tool 调用次数为 0。

### 15.3 LangGraph

- DIRECT_RESPONSE 不调用 Tool。
- CLARIFY 不产生副作用。
- 两步 ToolPlan 按步骤执行。
- 前序输出绑定到后序输入。
- 继续原计划不增加 planning_round。
- 最多 3 轮规划，即最多 2 次 replan。
- 最多 5 次 Tool 调用。
- 相同 Tool 和输入不重复执行。
- 有副作用 Tool 失败不自动重试。
- 高风险 Tool 在未确认状态下不能调用。
- 异步 Tool 进入等待状态而不是阻塞请求。

### 15.4 分类证据

- 只读取活动工作副本当前版本。
- 只读取最新成功分类运行。
- 历史版本建议不进入结果。
- evidence quote、页码、Sheet 和 signals 保持真实。
- 无证据的非“其他”建议进入 NEEDS_REVIEW。
- “为什么分到科研/教学/学生工作”返回对应当前证据或明确无依据。

### 15.5 Provider 与 Shadow

- 全局 LLM 开启不触发后台摘要 LLM。
- Planner Shadow 不调用 Tool。
- Shadow 不写 ChangeSet、OperationPlan 或异步 Job。
- Shadow 生成的 CapabilitySuggestion 不落库。
- 新旧 Planner 对比记录不含正文和绝对路径。
- 稳定分桶结果可重复。
- Adaptive 失败可回退 Legacy。
- `legacy` 回退开关即时生效。

### 15.6 验证命令

每个阶段先运行相关局部测试，完成阶段后执行：

```bash
cd apps/api
pytest -v
```

```bash
cd apps/web
npm run build
```

并按 `docs/file-agent-manual-smoke-test.md` 增加以下手工场景：

```text
普通对话直接回复
完整文件名自然语言问答
搜索文件后继续回答正文问题
解释当前文件为什么得到某个分类
目录或同名文件歧义澄清
重命名、移动和删除只生成待确认计划
Planner Shadow 开启后用户结果与 Legacy 一致
Adaptive Planner 灰度失败后回退 Legacy
```

## 16. 完成标准

本方案只有在以下条件全部满足时才算完成：

1. Planner 可以对未预先枚举的自然语言请求选择受控 Skill 和 Tool。
2. PlannerDecision、Tool 输入和 Tool 输出都有明确 schema。
3. 当前 AgentRun 使用的 Tool/Skill Catalog 有版本和指纹，可审计。
4. Tool 结果绑定不执行自由表达式，绑定后输入再次通过 schema。
5. LangGraph 每次只执行一个步骤，并能按结果继续、重规划、澄清、等待或结束。
6. DIRECT_RESPONSE 不回答文件事实，CLARIFY 不被滥用来绕过可执行计划。
7. 分类解释只读取当前版本最新成功建议和可定位证据。
8. 正常 LLM 主路径不再依赖大量确定性业务关键词抢占。
9. 后台摘要继续默认使用本地 CPU-only Jieba + LexRank。
10. Planner Shadow 不产生任何 Tool 副作用。
11. Adaptive Planner 经过 Shadow 和分阶段灰度后才成为默认值。
12. OperationPlan、ChangeSet、ToolInvocation、原件保护和权限边界没有被削弱。
13. 不存在的能力可以生成去重、脱敏、可审计的 CapabilitySuggestion，并在管理员页面评审。
14. CapabilitySuggestion 不会自动注册、启用或执行 Tool/Skill。
15. 后端全量测试通过，前端构建成功，手工烟测通过。

## 17. 实施后隐藏问题审计收口

本轮实现完成后还必须满足以下事务与安全细节；这些要求属于完成标准的一部分：

1. `AgentRun` 的 `RECEIVED` 状态在图执行前独立持久化。图异常时先回滚未提交的部分业务写入，再单独保存
   `FAILED`，不能让失败运行随请求事务一起消失。
2. `update_run_from_state` 是本轮 `ChangeSet` 聚合的唯一入口，同一次 AgentRun 不得重复删除并重建
   ChangeItem。
3. Planner scope 和每个 Tool 字面量中的 `document_id/document_ids` 都必须属于后端已解析的附件或会话
   上下文；任一层出现模型编造 ID 都拒绝整份计划。
4. `ToolPlan` 的 `step_id` 必须唯一，结果绑定只能引用之前步骤，同一步不能重复写入同一目标字段；重规划
   时必须清空步骤级绑定命名空间，但继续保留全局 ToolInvocation 和重复调用签名。
5. Tool 输入/输出 schema 错误和未知 Tool 必须生成结构化 `FAILED` ToolInvocation，不能冒泡为普通消息
   接口的 ASGI 500。
6. `hybrid-search` 只从真实检索结果投影去重且有上限的 `document_ids`，供后续 `evidence-answer` 绑定；
   澄清、异步等待和未命中结果不得生成虚假文件范围。
7. CapabilitySuggestion 按“建议类型 + 归一化能力缺口 + Catalog 指纹”去重，不因用户换一种说法重复建项。
   ops 不能接受、实现或覆盖已经由 admin 接受/实现的结论。
8. Shadow 的生成失败和校验失败都必须落入比较表并计入失败率。灰度指标默认只聚合同一最新
   `catalog_fingerprint + schema_version` 的样本，不能混入旧 Catalog 抬高或稀释成功率。
9. `CAPABILITY_UNAVAILABLE`、`UNSUPPORTED_REQUEST` 可以在不依赖文件事实、没有 Tool 计划时使用
   `DIRECT_RESPONSE`，并独立记录管理员能力建议，不执行用户不可见的占位 Tool。
