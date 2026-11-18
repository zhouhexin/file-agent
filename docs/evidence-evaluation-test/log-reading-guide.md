# 大白话读懂证据测试日志

核对样本：2026-10-30 11:43:33—11:44:17。
本次运行 `agent_run_id=ea0f83c8-64a1-43cd-be8e-55add2417f16`。
本文只解释已有日志，不改变程序、配置或测试结果。

## 1. 先记住：状态必须和事件一起看

`status=COMPLETED` 只说明这条 `event` 对应的步骤完成，不能单独理解为整次任务全部成功。
这次实际链路是：

```text
收到提问
→ 找到通知和原文证据
→ 模型返回JSON对象：COMPLETED
→ 证据评估：COMPLETED，但发现MISSING
→ 回答校验：5条结论中4条通过、1条被拦，PARTIAL
→ 保存部分回答：PARTIAL
→ 整个Agent任务结束：NEEDS_REVIEW
```

这些状态回答的是不同的问题，因此可以同时存在。

| event | 本次status | 大白话 |
| --- | --- | --- |
| `agent.run.started` | `RECEIVED` | 系统收到这次任务。 |
| `agent.planning.llm_intent` | `COMPLETED` | 已识别任务意图，不是已经回答完。 |
| `tool.invoke.started`，hybrid-search | `STARTED` | 开始查文件。 |
| `tool.invoke.completed`，hybrid-search | `COMPLETED` | 本轮查找步骤已结束，仍要看结果数量。 |
| `evidence_answer.retrieval_completed` | `INDEX_READY` | 通过现有索引取得本轮证据，不代表每个问题都有答案。 |
| `evidence_answer.package_built` | `COMPLETED` | 给模型的证据包准备好了。 |
| `evidence_evaluation.llm_response_shape` | `COMPLETED` | 模型内容已成功解析为JSON对象。 |
| `evidence_evaluation.shadow` | `COMPLETED` | 本期已实现的版本/语义检查都已执行完成。 |
| `evidence_answer.claims_validated` | `PARTIAL` | 模型结论有一部分未通过原文支持性校验。 |
| `evidence_answer.persisted` | `PARTIAL` | 保存的是通过校验的部分回答。 |
| `tool.invoke.completed`，evidence-answer | `PARTIAL` | 证据回答Tool结束，结果是部分完成。 |
| `agent.run.completed` | `NEEDS_REVIEW` | 整次任务已结束，结果还有需要核对或补充之处。 |

事件名里的 `.completed` 是“这个调用结束了”，最终结果仍要看status。
例如 `tool.invoke.completed + PARTIAL` 完全正常，不是状态自相矛盾。

## 2. 模型返回形态的COMPLETED是什么意思

本次 `evidence_evaluation.llm_response_shape`：

| 字段 | 本次值 | 说明 |
| --- | --- | --- |
| `http_status` | 200 | 接口返回了正常HTTP响应，不代表业务检查通过。 |
| `envelope_type` | object | HTTP外层JSON是对象。 |
| `choices_type` / `choices_count` | array / 1 | 接口返回一个choice。 |
| `content_type` | string | 模型正文按接口协议放在字符串里，这是正常类型。 |
| `content_empty` / `content_chars` | false / 1424 | 正文不为空，共1424字符。 |
| `reasoning_chars` | 2812 | 接口另返回2812字符的思考内容；日志只记长度。 |
| `finish_reason` | stop | 接口报告正常结束。 |
| `output_truncated` | false | 接口本次未报告因长度限制结束，不等于所有事实都回答完整。 |
| `parsed_json_type` | object | 从content字符串成功解析出了JSON对象。 |
| `parse_outcome` | MODEL_JSON_OBJECT | JSON解析步骤成功。 |

`content_type=string` 与 `parsed_json_type=object` 不冲突：前者是接口字段类型，后者是解析字符串后得到的类型。
这个事件只证明“返回内容可供程序继续处理”，不能证明字段全部合规、结论正确或证据充分。

## 3. 评估COMPLETED为什么还能有MISSING

评估器像检查员：检查已经做完，可以发现材料缺项。
`COMPLETED` 表示检查执行完成，`MISSING` 表示检查发现的问题。

本次 `evidence_evaluation.shadow`：

```json
{
  "status": "COMPLETED",
  "version_status": "COMPLETED",
  "semantic_status": "COMPLETED",
  "issue_types": ["MISSING"],
  "error_codes": [],
  "limitation_codes": ["BUSINESS_VALIDITY_NOT_VERIFIED"]
}
```

| 字段 | 含义 |
| --- | --- |
| `version_status=COMPLETED` | 本期的内容版本核对执行完成，不等于通知在业务上仍有效。 |
| `semantic_status=COMPLETED` | 要求支持性与同口径冲突检查执行完成。 |
| `issue_types=[MISSING]` | 当前证据包没有支持某项回答必需的事实。 |
| `reason_code=REQUIRED_FACT_UNSUPPORTED` | 缺项的固定原因码。 |
| `error_codes=[]` | 没有记录程序/接口执行错误，不是“没有证据问题”。 |
| `BUSINESS_VALIDITY_NOT_VERIFIED` | 没有核实通知是否仍为现行规定，是能力边界提示。 |

这次安全日志没有保存缺失要求的完整文字，因此不能单凭该条日志断定缺的是哪个具体字段。
结合问题，“今年是否仍执行”可能属于缺项，但这仍只是结合提问的推测，不是日志已经证明的对应关系。
MISSING里的 `evidence_ids=[]` 可以正常出现：缺的恰恰是支持该要求的证据，不应编造引用ID。

`BUSINESS_VALIDITY_NOT_VERIFIED` 不是“已过期”，也不是程序报错。
业务有效性本来就不在本期已实现检查范围内，所以这个提示不会单独让评估从COMPLETED变为PARTIAL。

## 4. 回答PARTIAL是什么意思

评估器检查“证据包能否满足问题”；回答校验器检查“模型刚生成的这些结论能否由所引原文支持”。
它们是两个步骤，不能把两边的计数直接对应起来。

本次 `evidence_answer.claims_validated`：

```json
{
  "status": "PARTIAL",
  "claim_count": 5,
  "accepted_claim_count": 4,
  "rejected_claim_count": 1,
  "rejection_reason_counts": {"CLAIM_TERM_OVERLAP_INSUFFICIENT": 1}
}
```

含义是生成5条结论、4条可展示、1条被移除。
“5条结论”不一定等于问题里的“5个栏目”，一条结论也可能涵盖不同程度的事实。
`CLAIM_TERM_OVERLAP_INSUFFICIENT` 表示与所引原文缺少足够可核对的事实词项；
它是保守词法校验的拒绝原因，不等于已经证明该句事实一定错误，也可能与改写措辞有关。

日志只记原因码和数量，不保存被拒的模型句子，因此不能用本条记录还原具体被拦的是哪句话。
评估发现的MISSING，也不能直接当作被拦的这一条结论。
本次有4条通过，因此不会触发“所有结论都被拒后再修正一次”的分支。

`evidence_answer.persisted=PARTIAL` 说明通过校验的部分回答已保存；
后面的 `tool.invoke.completed=PARTIAL` 是Tool层对同一结果的汇总，不是又失败了一次。

## 5. 整个任务NEEDS_REVIEW是什么意思

当前Graph的回答分支会把回答状态PARTIAL汇总为任务状态NEEDS_REVIEW。
因此本次直接原因是回答部分完成，不能说是MISSING诊断直接驱动了任务状态。
评估器仍是shadow旁观模式，不直接修改答案或任务决策。

这里表示“结果有未完全解决的部分，需要核对或补充”。本次运行已经结束，
不是仍在后台等模型，也不代表自动创建了一条人工审核任务或承诺有人会随后处理。

## 6. 执行错误、证据问题、能力边界分开读

| 类别 | 常见值 | 你应如何理解 |
| --- | --- | --- |
| 进行中 | RECEIVED、PLANNING、STARTED、RUNNING | 正在这个阶段处理，查看后续同一运行的结束记录。 |
| 步骤结束 | COMPLETED | 这一步执行完成，继续看问题类型与业务结果。 |
| 部分完成 | PARTIAL | 有可用结果，但有缺项、过滤结果或未检查范围；原因要结合event看。 |
| 评估执行失败 | EVALUATION_FAILED | 本次评估没有完整完成；看error_codes/error_details。 |
| 未执行 | NOT_EVALUATED、DISABLED | 因开关、预算或前置条件没有检查，不是检查通过。 |
| 无可回答证据 | NO_EVIDENCE | 当前没有可支持回答的材料或结论，不一定是文件不存在。 |
| 整体需核对 | NEEDS_REVIEW | 任务结束但结果需注意，具体看子步骤。 |
| 缺事实支持 | MISSING | 当前证据包缺少回答要求的支持，不代表全库没有。 |
| 同口径矛盾 | CONFLICT | 同一事实、口径可比的证据不兼容。 |
| 旧内容版本 | OUTDATED | 当前问题引用了已被就绪新内容版本替换的旧证据，不是仅看文件年份。 |
| 能力边界 | BUSINESS_VALIDITY_NOT_VERIFIED | 未核实材料在业务上是否仍现行有效。 |

评估失败时，已经确定的版本问题仍可能保留；但空的issue_types不能解释成“没有缺项或冲突”。

## 7. level、stage与耗时不是另一套业务结论

- `level=INFO` 是常规记录，`WARNING` 提醒有异常、部分结果或降级，`ERROR` 记录错误。
  INFO不保证业务完整，WARNING也不必然表示程序崩溃。本次任务结束日志为INFO，结果仍是NEEDS_REVIEW。
- 外层 `stage` 是日志大类，例如AGENT、TOOL、EVIDENCE、SYSTEM；
  `error_details[].stage` 则定位到具体调用阶段，例如LLM_REQUEST。两者都不是业务完成状态。
- `duration_ms` 为毫秒，1000毫秒=1秒。先看event，才能知道计时范围。

本次评估约22.217秒，答案模型生成约8.309秒，证据回答步骤累计约30.887秒，整个Agent任务约44.059秒。
这些范围互相包含，不能把所有duration_ms直接相加。
`evidence_answer.persisted` 上的约30.9秒是整个回答步骤累计耗时，不能理解为数据库保存单独用了30.9秒。

## 8. 数量与token

本次 `document_count=1`、`evidence_count=1` 表示一份文件、一段证据，不是一条事实。
同一段原文可以包含范围、格式、命名等多项要求，被不同结论引用。

评估日志 `llm_calls=1`、输入1699 token、输出2053 token、`token_usage_complete=true`：
表示本次评估的调用数，以及接口提供的输入/输出token统计都已取得。
这些不是整次任务的全部模型调用或费用；规划与生成回答另外有调用。
字符数与token不是同一计数单位，不能拿1424字符直接要求等于2053 token；
日志也没有据此记录供应商的详细计价规则。

## 9. 以后按这个顺序看

1. 找最新 `evidence_evaluation.shadow`，判断本次评估是否执行完成。
2. 失败就看error_details；完成就看issue_types；PARTIAL时额外看限制码与子检查状态。
3. 查看同一agent_run_id的 `evidence_answer.claims_validated` 与 `evidence_answer.persisted`，判断实际展示了多少结论。
4. 最后看 `agent.run.completed`，判断整次任务的最终回执状态。

`agent_run_id` 是一次执行的流水号；同一conversation_id里可以有很多次执行，不能用conversation_id替代它。
request_id跟踪HTTP请求，后台任务可能没有；本次链路优先使用agent_run_id。

在项目根目录查看评估与回答的核心状态：

```bash
rg --no-ignore -n 'evidence_evaluation\.(llm_response_shape|shadow)|evidence_answer\.(claims_validated|persisted)|agent\.run\.completed' runtime/evidence-test/logs/
```

查看本文这一轮完整记录：

```bash
rg --no-ignore -n -F 'ea0f83c8-64a1-43cd-be8e-55add2417f16' runtime/evidence-test/logs/file-agent-2026-10-30.log
```

`--no-ignore` 必须保留，runtime与日志目录被.gitignore忽略。

本次结果可读为：找到文件、模型格式正常、评估完成并发现缺项；回答有4条通过、1条被拦，
所以返回部分回答，整个任务标为需要核对。不能概括成“全部成功”，也不能概括成“全部失败”。
