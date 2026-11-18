# 显式来源问答：精确短语校验与一次规划纠错

## 1. 真实问题与原因

2026-10-30 10:15，用户提交：

```text
只根据 2023届毕业生合影照片的通知，说明照片归档范围、电子照片格式、文件命名要求、打包提交方式，以及最晚归档日期。请引用原文，不补充文件外的信息，并说明今年是否还按照这些要求执行
```

该请求的 LLM 已选中文件问答，并返回来源检索词“2023届毕业生合影照片通知”，
但把“毕业生合影照片归档”设为必需的 `EXACT_PHRASE`。
真实通知中有“毕业生合影照片”和归档要求，却没有这个拼接后的连续短语。
检索先召回一份文件，再因精确短语校验不通过而排除，最终显示零结果。

这次没有把“今年”当成2026年过滤，来源年份仍为2023。
09:59另一次只有“今年是否还按这些要求执行”的独立消息缺少明确来源，属于不同的会话指代问题，
不能把那次的查询条件解释为本次完整提问的原因。

## 2. 修复位置与边界

```text
LLM 判断业务分支、输出声明式计划
→ 后端校验来源检索条件
   → 合法：受控检索 → 真实文件ID → 证据评估 → 引用回答
   → 不合法：一次 LLM 参数纠错
      → 合法：继续原任务
      → 仍失败：已有的显式来源只读兜底
```

没有按业务关键词重新选择分支；没有把精确短语放宽成拆词 OR，也没有把弱候选升级成事实证据。

对“根据/依据某来源，说明/列出等正文任务”这类明确来源结构：

- 必需精确主题与机构短语必须是来源描述中的连续原话；仅统一Unicode、空白和大小写。
- 来源检索词应有来源原话支持，不能拼入待回答字段或其他业务主题。
- 年/月过滤须与来源描述一致，不能用“今年是否仍执行”改写历史来源时间条件，也不能丢掉来源年份。
- 有有效结构化主题、没有精简query时保持兼容，但原问题解析出的时间条件仍须符合来源。
- 没有结构化主题时，现有检索会把整个cleaned query作为精确短语；该短语也须连续出自来源，
  不能通过删除语义计划绕过校验、继续使用拼接的查询。
- 用户已选定附件时沿用后端附件边界；非显式来源句式继续使用既有检索协议。

Legacy 参数无效时，最多增加一次规划调用。纠错只调整来源参数，锁定原业务意图和完整用户目标；
不能通过改成闲聊、直接回复或换任务绕过校验。接口本身失败时不为此追加网络重试。
Adaptive 使用真实 `user_message` 校验全局来源搜索；若失败，沿既有 Legacy 回退链修正。

纠错失败后仍不执行错误条件。确定性兜底只用用户明确的来源描述发起只读发现，后续仍须通过真实检索
绑定文件和版本，不能猜ID、改文件或忽略证据门槛。无法核对的来源范围不声称“已找到”。

## 3. 历史要求与当前适用性

“只根据2023通知”限制了证据来源。“今年是否还执行”是新增的待回答事实，不会自动授予跨文件或联网搜索。
回答与评估提示均明确：历史要求、历史期限和当前适用性分别核对；没有当前适用依据时不能标为支持，
也不能据此断言文件已经失效。预期回答应说明：可以列出通知中的历史要求，但仅凭该文件无法确认今年是否仍执行。

这不新增法规废止查询或业务有效性验证能力；`BUSINESS_VALIDITY_NOT_VERIFIED` 仍表示该能力边界。

## 4. 日志与成本

无效条件执行前记录 `agent.planning.source_scope_rejected`，只有固定错误码，不记录原始异常文本：

| 错误码 | 含义 |
| --- | --- |
| `SOURCE_EXACT_PHRASE_NOT_GROUNDED` | 必需精确短语不在用户指定来源原话中。 |
| `SOURCE_TIME_FILTER_MISMATCH` | 来源年份/月和检索过滤不一致。 |
| `SOURCE_QUERY_NOT_GROUNDED` | 检索词混入来源外的内容。 |
| `SOURCE_QUERY_REQUIRED` | 缺少可用来源检索词和必需主题。 |
| `SOURCE_REPAIR_CHANGED_TASK` | 参数纠错擅自改变业务意图或决策类型。 |
| `SOURCE_PLAN_SCHEMA_INVALID` | Adaptive 来源检索输入不符合Tool schema。 |

`agent.planning.llm_intent.source_plan_repair_attempt` 和运行时规划元数据中的
`source_plan_repair_count` 标记0/1次纠错。只在无效参数触发时增加最多一次 Legacy 意图模型调用；
语义评估日志的 `llm_calls` 仍只统计语义评估，不是整次请求的所有模型调用数。

项目根目录可检索：

```bash
rg --no-ignore -n 'agent.planning.source_scope_rejected|evidence_evaluation.shadow' runtime/evidence-test/logs/
```

## 5. 文件清单

| 文件 | 变动与环节 |
| --- | --- |
| `apps/api/app/modules/retrieval/source_reference.py` | 新增：显式来源提取、连续原话/时间/检索词校验及固定错误码。 |
| `apps/api/app/tests/test_evidence_source_scope.py` | 新增：19个参数化场景，验证历史来源、复合精确短语拒绝、一次纠错、失败兜底、Adaptive输入类型保护及删除语义计划不能绕过校验。 |
| `apps/api/app/modules/agent/planner.py` | 计划转换前校验来源条件；旧确定性来源提取复用新模块，不新增业务分支规则。 |
| `apps/api/app/modules/agent/graph.py` | 增加一次来源参数纠错、固定反馈与审计；保留原问题，拒绝无效计划执行。 |
| `apps/api/app/modules/agent/adaptive_planner.py` | 传入真实用户约束后校验来源Tool输入；补充精确主题与历史来源提示。 |
| `apps/api/app/modules/llm/prompts.py` | 说明来源原话、精确短语、回答时态及一次参数纠错契约。 |
| `apps/api/app/modules/evidence_answer/service.py` | 回答提示区分历史要求和当前适用性，缺依据进入limitations。 |
| `apps/api/app/modules/evidence_evaluation/semantic_checker.py` | 评估提示要求当前适用性独立核对，不用历史日期自动支持当前。 |
| `apps/api/app/modules/llm/diagnostics.py` | 新增 `INTENT_PLAN_VALIDATION` 阶段，schema中文说明兼容规划与评估协议。 |
| 本文、README、runbook、真实测试题与诊断说明 | 同步修复原因、调用边界、错误码和验证方式。 |

## 6. 验证与生效

使用原失败请求的持久化计划进行只读核对：旧拼造短语被拒绝为
`SOURCE_EXACT_PHRASE_NOT_GROUNDED`；模拟模型把主题修正为原话“毕业生合影照片”后，
保留原query、完整问题及2023年条件，真实测试库准确返回
`2023_关于接收2023 届毕业生合影照片的通知.pdf`，相关性 `SUPPORTED`、`partial=false`。

该核对没有调用真实LLM、没有写测试库或修改文件。模型纠错本身由 deterministic fake 验证，
真实模型的纠错输出和最终回答仍需用户重启后测试，不能把本次只读检索回放当作完整模型验收。

本轮相关回归472项通过，涵盖来源校验、原问句、Planner、Agent Runtime、Adaptive、证据回答、评估及安全诊断、
模型客户端、结构化检索、短语策略与检索集成。模型/HTTP均为fake。

在原终端停止并重启API和analysis后重发原问题，无需重新上传或重建索引：

```bash
# 项目根目录，API终端
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api
```

```bash
# 项目根目录，analysis终端
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis
```

本次不新增env项或数据库迁移，不改模型账号和计费设置。
