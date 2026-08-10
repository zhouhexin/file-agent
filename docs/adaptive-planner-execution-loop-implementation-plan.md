# 自适应 Planner 与 LangGraph 规划执行循环实施方案

- 文档状态：开发中（现有循环骨架已落地，阶段 3 搜索观察闭环待完善，阶段 7 保持 Shadow 观察）
- 编写日期：2026-07-30
- 代码审计范围：当前工作树，包含现有 LangGraph 步骤级循环、Adaptive Planner 和 Shadow 链路
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

- 已完成：5 项。
- 部分完成：5 项。
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
| 5 | 完善 LangGraph 规划执行循环 | 已完成 | 已存在 `planning -> tool_dispatch -> observe_tool_result` 循环；Dispatcher 每次只执行一个步骤；`hybrid-search` 使用 `PLANNER_AFTER_EXECUTION`，安全观察包含命中数量、受控文件 ID、实际条件、索引状态和允许动作；支持结束、改查、继续读证据、最多 3 轮规划、5 次调用、重复拒绝和确认暂停 | 继续在真实生产模型上观察不必要改查与预算耗尽率 |
| 6 | 接入 DIRECT_RESPONSE 和 CLARIFY | 已完成 | 三分支已进入独立 PlannerDecision；文件事实不能通过 DIRECT_RESPONSE 绕过 Tool；缺少唯一范围进入 CLARIFY | 继续用回放集监测不必要澄清 |
| 7 | 实现分类证据读取能力 | 部分完成 | 已新增当前版本 EvidenceReader；优先共享活动工作副本当前版本，只取最新成功运行；EvidenceAnswer 复用该服务；检索命中的跨导入者共享文件可继续读取分类，普通回复展示置信度与页码/Sheet 原文依据 | 仍需补齐失败运行、回收站、同名文件和无 quote 的完整测试矩阵，并收敛严格 output model |
| 8 | 缩减确定性关键词路由 | 部分完成 | 正常 LLM 主路径只保留重命名、确认、分类等安全 preflight；普通搜索、总结、解释、能力咨询和表格语义交给 Catalog Planner | Legacy/故障降级仍保留 `_has_*`，需在 Shadow 回放达标后再删除重叠规则 |
| 9 | 调整后台 LexRank 摘要 Provider | 已完成 | 后台双摘要默认 CPU-only `Jieba + LexRank`；全局 LLM 开关不隐式外发；已有配置测试 | 继续保护最终回答必须回到 Evidence |
| 10 | Shadow 模式对比新旧 Planner 后再默认启用 | 部分完成 | 已实现 `legacy/shadow/enabled`、只读双决策、对比表、稳定分桶、灰度开关和失败回退；Shadow 不执行第二次 Tool | 尚未完成生产观察期、离线回放指标报表和 5%→100% 灰度，因此不得默认启用 Adaptive |

### 3.3 现有循环骨架与剩余缺口

当前运行图已经具备以下物理链路：

```text
planning
-> record_capability_suggestions
-> tool_dispatch
-> observe_tool_result
-> route_after_observation
   -> planning
   -> tool_dispatch
   -> async_job_wait
-> evidence_or_change
-> response
```

因此后续工作不是重新搭建 LangGraph，也不以拆出更多物理节点为完成条件。当前
`tool_dispatch`、绑定解析和结果记录可以继续保留为组合节点，只要职责边界、schema 校验和审计事实清晰。

当前语义闭环已经形成：

1. `ToolDefinition.observation_policy` 由后端决定是否需要重新进入 Planner。
2. `hybrid-search` 使用 `PLANNER_AFTER_EXECUTION`，不依赖 Tool 自报 `replan_required`。
3. 安全观察包含命中数量、实际条件、受控文件 ID、索引状态和允许的下一步，不包含正文和内部路径。
4. Planner 可以选择 `FINISH`、改变语义查询再次检索、调用 `evidence-answer`、
   `read-document-classifications`，或在范围不唯一时请求澄清。
5. 多轮检索最终采用最后一次有效结果，并在回执中保留后端确认的条件与各轮结果数量。
6. 已建立自然语言回归矩阵，覆盖“检索后回答正文”“检索后解释分类”“零结果后调整条件再查”。

当前剩余工作不是重新修改 LangGraph 主体，而是完成真实生产模型的 Shadow 指标观察和分阶段灰度。
`ADAPTIVE_PLANNER_MODE=shadow` 时 Adaptive Planner 仍只做只读对比，这是上线安全策略，不是循环缺失；
只有部署显式进入 `enabled` 灰度后，Adaptive 决策才接管真实 Tool 链路。

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

当前实际节点保持为：

```text
chat_intake
-> collect_context
-> build_catalog_snapshot
-> planning
-> record_capability_suggestions
   -> direct_response
   -> clarification_response
   -> tool_dispatch
-> observe_tool_result
   -> tool_dispatch
   -> planning
   -> async_job_wait
-> evidence_or_change
-> response
```

其中决策校验、步骤选择、结果绑定和结果记录可以继续作为 `planning`、`tool_dispatch` 内部的明确职责，
不强制为了节点数量而拆分物理节点。后续验收关注的是每一步是否受 schema、Catalog、权限、预算、确认和
审计边界控制，而不是图上节点名称是否与逻辑职责一一对应。

### 7.2 单步执行

现有 `tool_dispatch` 每次只执行一个 ToolStep。这样才能：

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

### 7.5 循环控制的两层职责

必须明确区分“后端观察策略”和“LLM 下一步决策”：

```text
Tool 完成
-> 后端根据 ToolDefinition.observation_policy 判断是否需要 Planner 观察
-> 生成脱敏 ExecutionObservation
-> Planner 根据原始用户目标和观察结果生成 PlannerDecision
-> 后端再次校验 Catalog、schema、范围、风险和预算
-> 执行下一步或结束
```

第一版建议为 ToolDefinition 增加：

```text
observation_policy:
  CONTINUE_PLAN
  PLANNER_AFTER_EXECUTION
  PLANNER_ON_SIGNAL
  FINALIZE
```

语义如下：

- `CONTINUE_PLAN`：成功后优先执行既有 ToolPlan 的下一步，不额外消耗规划轮数。
- `PLANNER_AFTER_EXECUTION`：每次成功执行后都让 Planner 判断下一步；`hybrid-search` 等发现型 Tool
  使用此策略。
- `PLANNER_ON_SIGNAL`：只有结构化业务信号要求调整计划时才进入 Planner，兼容现有
  `replan_required/replan_signal`。
- `FINALIZE`：结果本身已经满足本轮固定任务，直接进入汇总。

`observation_policy` 由后端 Catalog 定义，LLM 不能在 PlannerDecision 中修改。Tool 仍可返回业务信号，
但不能独占“是否允许 LLM 观察”的控制权。

### 7.6 搜索 Tool 的 ExecutionObservation

搜索 Tool 必须拥有独立严格输出 schema。Tool 原始业务结果经过后端投影后，再生成以下安全观察：

```json
{
  "tool_name": "hybrid-search",
  "status": "COMPLETED",
  "error_code": null,
  "query": "未来五年发展规划",
  "result_count": 6,
  "document_ids": ["document-uuid"],
  "effective_conditions": [
    {
      "label": "主题",
      "value": "未来五年计划及五年发展规划",
      "condition_type": "semantic",
      "status": "APPLIED"
    }
  ],
  "index_status": "READY",
  "partial": false,
  "available_next_actions": [
    "FINISH_WITH_RESULTS",
    "READ_MATCHED_DOCUMENTS",
    "REFINE_SEARCH"
  ]
}
```

安全边界：

1. `document_ids` 只能来自当前 Tool 的真实授权结果，必须去重并限制最大数量。
2. 观察不包含文件正文、OCR 全文、本地绝对路径、数据库字段或未授权文件名。
3. 后续 Tool 使用 `document_ids` 时必须通过 `ToolResultBindingResolver`，模型不能重新拼造 ID。
4. `effective_conditions` 是后端根据实际 Tool 输入和检索执行情况确认的条件，不直接照抄 LLM 文本。
5. Tool 失败时只暴露结构化错误代码和允许的下一步，不把数据库异常原文交给模型。

### 7.7 无法穷举条件时的查询表达

不能为学校业务中的每一种主题、人物、事件或关系预先增加固定字段。搜索输入采用三层表达：

1. `semantic_query`：保存不能穷举的自然语言主题和关系，由全文、摘要和向量检索处理。
2. `hard_filters`：只包含后端真实支持并可以确定性校验的年份、文件类型、文档 ID 和授权范围。
3. `interpreted_conditions`：保存 Planner 对用户目标的结构化理解，用于审计和回执；后端为每项标记是否
   真正执行。

`interpreted_conditions.status` 统一使用：

```text
APPLIED
SEMANTIC_ONLY
RELAXED
UNSUPPORTED
REJECTED
```

其中 `UNSUPPORTED` 不能伪装为已应用过滤条件；`REJECTED` 必须说明权限或安全边界；LLM 提取出的任意
业务概念可以作为 `SEMANTIC_ONLY` 条件参与语义查询，但不能直接转换为 SQL、绝对路径或数据库写操作。

### 7.8 搜索后的 Planner 决策

当 `hybrid-search` 使用 `PLANNER_AFTER_EXECUTION` 时，无论结果是否为空，都由 Planner 在剩余预算内
作出下一步决策：

- 用户只问“有哪些文件”且已经命中：结束并生成搜索结果回执。
- 用户要求总结、比较或回答正文问题且已经命中：把真实 `document_ids` 绑定到正文读取或
  `evidence-answer`。
- 结果为 `ZERO_RESULTS`：允许调整语义条件或放宽非强制条件后再次搜索。
- 结果为 `INDEX_PENDING`：进入异步等待，不改写用户语义条件。
- 结果为 `SEARCH_ENGINE_UNAVAILABLE`：停止语义重试并返回服务降级，不得当作零结果。
- 文件范围不唯一或关键条件互相冲突：返回 `CLARIFY`，只询问阻止继续执行的最小信息。
- 预算耗尽：使用已有结果生成部分完成回执，列出未满足条件，不得无限循环。

每轮仍受最大规划 3 轮、最多 5 次 Tool 调用、重复签名拒绝、副作用 Tool 不自动重试和 OperationPlan
确认边界限制。

### 7.9 查询条件与用户回执

普通用户不展示 Planner、Tool、Skill、规划轮数或 Catalog。搜索结果和后续证据回答应携带统一
`search_context`：

```text
本次查找条件
- 主题：未来五年计划及五年发展规划
- 范围：当前用户全部已整理文件
- 匹配位置：文件标题、摘要和正文
- 匹配方式：相关主题
- 条件调整：首次精确主题无结果，随后扩展到五年发展规划和规划纲要
```

回执中的条件必须来自后端确认的 `effective_conditions`。同时保留每次 `search_attempt`，最终结果选择
最后一次成功且有效的搜索；不得因为第一轮返回零结果而覆盖后续成功结果。若搜索后继续执行
`evidence-answer`，证据回答作为主结果，`search_context` 继续展示在结果上方。

### 7.10 完整文件名问题不依赖上一轮搜索上下文

当用户在当前消息中给出受支持扩展名的完整文件名，例如：

```text
为什么二级管理--建议（计算机）.docx 涉及到了学生工作管理
```

完整文件名本身已经构成明确文件范围。该请求不得强制读取上一轮 `search_context`，也不得因为上一轮曾经
展示过其他文件而扩大本次范围。正确链路为：

```text
当前消息提取完整文件名
-> 后端在当前用户可访问的 ACTIVE 工作副本中执行完整名称匹配
-> 唯一命中：固定 document_id 和 current_version_id
-> 多个同名命中：CLARIFY，让用户选择
-> 未命中：返回有限相似候选或明确未找到
-> 检查当前版本正文、摘要、分类证据和索引状态
-> 根据问题选择 read-document-classifications 或 evidence-answer
-> 返回原因和可定位证据
```

Planner 可以把“学生工作管理”理解为本次语义条件，但后端必须把完整文件名作为不可被模型放宽的硬条件。
如果用户询问的是正文为什么与该主题相关，直接使用 `evidence-answer`；只有问题明确指向“为什么这样
分类、为什么归到该目录”时，才优先使用 `read-document-classifications`。证据不足时必须明确返回无依据，
不能借用其他同名或相似文件回答。

`search_context` 只用于“这些文件”“刚才找到的结果”等明确依赖上一轮结果的省略表达，以及展示由搜索
继续进入证据回答时的真实查询条件；它不是完整文件名问题的必需输入。

### 7.10.1 同名文件选择后的封闭范围续跑

完整文件名匹配到多个活动工作副本时，用户只需要选择一次。选择完成后，后端必须把持久化
`FileSearchClarification` 作为本轮续跑的范围凭据，原问题中的完整文件名不能再次触发全库同名扫描。

```text
多个同名文件
-> 后端创建 DOCUMENT_SELECTION 记录
-> 前端只提交后端生成的 option_id
-> 后端校验用户、会话、选择状态和 option_id
-> 生成 document_ids + document_selection_clarification_id
-> evidence-answer 回查持久化选择记录
-> 校验记录中的完整 document_ids 与 Tool 输入一致
-> 将所选文件集合锁定为封闭范围
-> 继续执行最开始的问题并直接返回证据回答
```

安全和一致性规则：

1. `document_selection_clarification_id` 不是 LLM 的授权声明。即使模型生成该字段，服务端仍必须验证
   记录属于当前用户和会话、类型为 `DOCUMENT_SELECTION`、状态为 `RESOLVED`，并且文件集合完全一致。
2. 已确认范围不得被完整文件名解析、相似文件召回、上一轮 `search_context`、同名回收站项目或后续
   Planner 观察再次扩张。
3. 所选文件已经删除、失效或不再授权时，返回明确不可用状态，不得自动换用另一个同名文件。
4. 用户选择多份文件时，这几份文件共同构成封闭范围；系统不能补入未勾选的同名文件。
5. 该规则适用于总结、正文问答、分类原因解释和比较等自然语言读取任务。表格分析、重命名等其他
   Tool 也必须只消费已确认的稳定文件 ID，不得重新按名称猜测范围。
6. 运维日志必须记录“锁定用户所选文件”、选择记录 ID 和文件数量，但不得记录正文内容。

### 7.11 面向运维人员的中文诊断日志

现有 JSONL 技术日志继续保留，但不能只依赖英文事件名、内部枚举或需要阅读代码才能理解的字段。每个关键
事件使用同一条结构化记录，同时提供机器字段和运维可读字段，避免维护两套可能不一致的日志。

统一增加或规范以下字段：

```text
event
event_title
stage
status
operator_message
cause_code
recommended_action
request_id
agent_run_id
conversation_id
user_id
tool_name
document_id
document_version_id
filesystem_job_id
duration_ms
created_at
```

字段规则：

- `event`、`stage`、`cause_code` 使用稳定英文代码，供程序检索、告警和统计。
- `event_title` 使用简短中文，例如“精确定位文件”“检查正文索引”“等待后台分析”。
- `operator_message` 使用不需要阅读代码即可理解的中文，说明系统完成了什么、正在等待什么或为什么失败。
- `recommended_action` 只在等待过久或失败时提供明确操作，例如“确认 worker 进程正在运行，并检查该任务
  是否达到最大重试次数”。
- 普通成功事件不重复打印无意义建议。
- 日志不得包含文件正文、OCR 全文、完整 LLM prompt、API key、JWT、密码或本地绝对路径。
- 普通用户界面不展示 Tool、Skill、Job ID 和技术错误；`ops/admin` 的诊断页面可以查看受控技术字段。

完整文件名证据回答至少记录以下诊断时间线：

```text
收到自然语言请求
-> Planner 已生成受控决策
-> 已从当前消息提取完整文件名
-> 完整文件名匹配结果：唯一 / 多个 / 未找到
-> ACTIVE 工作副本和当前版本检查结果
-> 摘要、分类证据、DocumentChunk 和 EvidenceSpan 就绪状态
-> 是否创建检索就绪任务
-> worker 是否领取任务
-> 文件分析任务完成或失败
-> 等待中的 AgentRun 是否成功续跑
-> 证据回答完成、无证据或失败
-> 最终回执状态已更新
```

示例日志：

```json
{
  "event": "evidence_answer.index_checked",
  "event_title": "检查文件正文索引",
  "stage": "EVIDENCE_INDEX_CHECK",
  "status": "WAITING",
  "operator_message": "已唯一定位文件，但当前版本正文索引尚未完成，正在等待后台分析。",
  "cause_code": "INDEX_PENDING",
  "recommended_action": "若长时间未完成，请确认 worker 进程正在运行，并检查关联任务是否失败。",
  "request_id": "request-uuid",
  "agent_run_id": "agent-run-uuid",
  "document_id": "document-uuid",
  "document_version_id": "version-uuid",
  "filesystem_job_id": "job-uuid",
  "duration_ms": 24
}
```

运维入口建议增加：

```text
GET /api/admin/agent-runs
GET /api/admin/agent-runs/{agent_run_id}/diagnostics
/admin/agent-runs
```

诊断详情按时间顺序展示“阶段、状态、中文说明、耗时、原因、建议操作”，并关联 AgentRun、
ToolInvocation、FilesystemJob 和处理事件。页面默认隐藏原始 JSON，需要时由 ops/admin 展开；不能把
日志页面开放给普通 user。

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
- 搜索后 `FINISH/REFINE_SEARCH/READ_MATCHED_DOCUMENTS/CLARIFY` 决策正确率。
- 零结果后的有效条件调整率和无意义重复查询率。
- 搜索实际条件与用户目标的一致率。
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
8. 搜索回放集中不存在把服务故障解释为零结果、把未应用条件展示为已应用条件的样本。
9. 搜索后决策不存在越权扩大文件范围或重复执行相同 Tool 输入的样本。
10. 自动化测试、后端全量测试和前端构建通过。
11. 管理员可以一键回退 `legacy`，回退不要求数据库回滚。

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

1. 保留并复核当前已经落地的最小 LangGraph 循环。
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

### 阶段 3：完善 LangGraph 步骤级规划执行循环

目标：保留已经完成的单步循环骨架，补齐搜索观察和 LLM 下一步决策语义。

任务：

1. 保留现有 `planning -> tool_dispatch -> observe_tool_result` 物理节点和单步执行方式。
2. 为 ToolDefinition 增加后端不可被 LLM 覆盖的 `observation_policy`。
3. 为 `hybrid-search` 增加严格输出 schema 和 `PLANNER_AFTER_EXECUTION` 策略。
4. 扩展 `_safe_tool_observation`，投影结果数量、受控文件 ID、实际条件、索引状态和允许的下一步。
5. Planner 第二、三轮必须消费上一轮 `ExecutionObservation`，不能再次生成相同 Tool 和相同输入。
6. 建立 `semantic_query + hard_filters + interpreted_conditions` 三层查询表达。
7. 区分 `ZERO_RESULTS`、`INDEX_PENDING`、`SEARCH_ENGINE_UNAVAILABLE` 和范围歧义。
8. 修复主检索失败后的事务/savepoint 降级，基础设施失败不能被解释为没有相关文件。
9. 修复多轮聚合优先读取第一条搜索结果的问题，最终选择最后一次成功有效结果。
10. 为搜索结果回执和证据回答增加统一 `search_context`。
11. 保留全局预算、重复调用签名、副作用 Tool 不盲目重试和 OperationPlan 确认边界。
12. 为异步等待和确认恢复继续保留 checkpoint 接口；本阶段可保持同步最小实现。
13. 为完整文件名问题增加独立硬范围测试，不读取上一轮 `search_context`，不扩大到其他文件。
14. 为精确文件定位、证据就绪检查、异步任务领取、AgentRun 续跑和最终回执增加运维可读中文日志。
15. 增加 AgentRun 诊断时间线 API，并以现有 AgentRun、ToolInvocation、FilesystemJob 和结构化日志作为
    事实来源，不新增不可审计的自由文本状态。
16. 增加 `/admin/agent-runs` 诊断页面；普通用户聊天页面继续只显示任务结果和安全状态。

完成门槛：至少支持“搜索命中后结束”“零结果后放宽一次条件”“搜索结果绑定到证据回答”“索引等待”
和“检索故障停止语义重试”，且最终回执能展示后端确认的实际查询条件；运维人员能够仅根据中文诊断时间线
确认完整文件名问答卡在文件定位、证据索引、worker、续跑还是最终回执阶段。

截至 2026-07-30 的实施状态：

- 已完成 `observation_policy`、`hybrid-search` 安全观察、`FINISH`、三轮规划预算和搜索结果 ID 授权扩展。
- 已完成主搜索 savepoint 降级、多轮结果取最后一轮、`search_context` 持久化与普通用户安全投影。
- 已完成 JSONL 运维可读字段、`/api/admin/agent-runs` 诊断接口和 `/admin/agent-runs` 页面。
- 已完成完整文件名独立硬范围日志，当前消息中的完整文件名不依赖上一轮 `search_context`。
- 已增加“搜索后观察并结束”、实际条件状态、最新轮结果、诊断权限与中文时间线自动化测试。
- 已增加自然语言规划执行矩阵，覆盖“搜索后继续证据回答”“搜索后读取当前分类依据”和“零结果后调整
  语义条件再查”；测试使用 deterministic fake，不依赖外部模型。
- 已修复共享工作副本在 `read-document-insights` / `read-document-classifications` 中被导入者
  `Document.user_id` 错误隔离的问题；授权范围统一为用户自己的上传文件或共享工作区 ACTIVE 工作副本。
- 分类解释回执展示置信度与首条可定位原文依据；没有 quote 时明确要求人工复核，不再只列分类名称。
- 仍需在生产可用 LLM 和真实 PostgreSQL/pgvector 数据上完成“零结果后由模型放宽条件”和“搜索后继续
  证据回答”的手工烟测；这两条路径的运行时结构已经具备，但不能用 deterministic fake 代替生产语义验收。
- Adaptive Planner 默认值继续保持 `shadow`，待第 7 阶段的生产指标和回退演练完成后再灰度启用。

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
5. 回放覆盖搜索命中、零结果放宽、索引等待、搜索故障、搜索后证据回答和条件澄清。
6. 按稳定哈希执行 5%、25%、50%、100% 灰度。
7. 达到门槛后将 Adaptive Planner 设为默认值。
8. 保留即时回退 Legacy 的配置能力。

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
apps/api/app/modules/retrieval/service.py
apps/api/app/modules/conversations/user_receipt.py
apps/api/app/modules/admin/router.py
apps/api/app/modules/admin/service.py
apps/api/app/core/config.py
apps/web/src/features/admin/AgentRunsPage.tsx
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
- 搜索 Tool 执行后由 `PLANNER_AFTER_EXECUTION` 进入 Planner，而不是依赖 Tool 猜测任务是否完成。
- 第一次搜索零结果后，第二轮可以改变语义条件并成功返回文件。
- 搜索已经满足“列出文件”目标时直接结束，不产生无意义的正文读取。
- 用户要求总结或问答时，把搜索结果的真实 `document_ids` 绑定到证据 Tool。
- 相同搜索条件不能在第二轮重复执行。
- 第三轮仍不能完成时返回部分结果和未满足条件，不进入第四轮。
- `INDEX_PENDING` 进入异步等待，不触发查询条件放宽。
- `SEARCH_ENGINE_UNAVAILABLE` 返回服务降级，不触发语义重试。
- 多轮搜索最终采用最后一次成功有效结果，不采用第一轮零结果。
- 用户回执显示后端确认的 `search_context`，不显示 Tool、Skill 和内部规划预算。
- 当前消息包含完整文件名时，不读取上一轮 `search_context`，只在唯一 ACTIVE 工作副本范围内回答。
- 完整文件名存在多个同名活动副本时进入 CLARIFY，不合并内容。
- 完整文件名没有命中时只返回有限相似候选或明确未找到，不退回全库宽泛回答。
- `ops/admin` 可以按 AgentRun 查看中文诊断时间线，普通 user 无权访问。
- 诊断时间线可以区分文件定位、索引等待、worker 未领取、任务失败、续跑失败和回执更新失败。
- JSONL 和诊断 API 不包含文件正文、绝对路径、密钥、JWT 或完整 LLM prompt。

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
16. 搜索 Tool 的结果能够作为安全观察进入 Planner，由 LLM 在 3 轮预算内决定结束、改查、读取证据或澄清。
17. 最终查询条件来自后端确认的实际执行条件，零结果、索引等待和检索服务故障不会互相混淆。
18. 当前消息包含完整文件名时以该名称作为硬范围，不依赖上一轮搜索上下文，也不会扩大到其他文件。
19. ops/admin 可以通过中文诊断时间线定位文件定位、证据准备、worker、AgentRun 续跑和回执更新问题。
20. 运维可读字段与机器事件来自同一结构化日志事实，不形成两套互相矛盾的日志。

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
