# 参考方案实验的调用失败与契约错误诊断

日期：2026-11-20。实验：`windows-reference-loop-v1`。

依据：操作者只读导出的 `reference-score-review-20261120-133255-062106.json` 和 `reference-diagnostics-review-20261120-134333-509503.json`。本轮只分析既有记录，并用当前真实 Tool 输入模型离线重验；没有调用真实模型、执行业务 Tool、修改评分或重新封存实验。

## 1. 模型调用失败：确定到 CLI 失败终态，尚不能确定全部网络根因

149 次调用中，118 次完成、31 次失败。失败包括 30 次规划调用和 1 次建议调用。按规划审计分解，30 次规划失败为 19 次 Adaptive 和 11 次 Legacy。31 次诊断均为 `PROVIDER_ERROR`，CLI 均已开始 turn，随后返回 `turn.failed`，均未收到完成事件。

其中 13 次带 `CONNECTION_HINT`，18 次没有该线索。连续失败区间包括调用 49—55（7 次）、121—132（12 次）。这支持连接链路不稳定的判断，但无法凭当前记录进一步定位为本机网络、代理、TLS、服务端或其他具体原因。

适配器 `codex_backend._failure_category()` 只精确识别部分额度和认证错误码，其余错误默认归 `PROVIDER_ERROR`。HTTP 状态在本轮全部为空；原始 CLI 自由错误文本没有持久化，stderr 被丢弃。因此不能把这个类别直接解释为“31 次都是上游服务器故障”，也不能排除未被现有分类识别的其他问题。没有证据支持直接归因为额度不足、认证过期、60 秒超时或 Windows 文件权限拒绝。

第 149 次为第四份候选的建议调用，8093 毫秒后 `turn.failed`，含连接线索。随后 Journal 抛出调用异常，`reference_loop.run()` 捕获为通用 `REFERENCE_EXECUTION_FAILED`；第四份提案未生成，整个实验以 FAILED 结束。停止时请求数 149/604、活动时间约 3737/7200 秒，预算和活动时限均未耗尽。

所有 118 次成功和 31 次失败调用均记录两条启动 notice。启动 notice 数量不能解释成功/失败差异，不能把非致命 notice 再次误判为请求失败的根因。

## 2. 工具参数错误：30 个试次均可用真实模型重现

复用 `planning_diagnosis.parameter_diagnostics()` 和实际 Registry 的输入模型离线验证，不运行 handler。

| 已报告错误 | 试次数 | 真实约束 |
|---|---:|---|
| `SHOW_ALL_REQUIRES_RESOLVED_CLARIFICATION` | 22 | 设置 `show_all_results=true`，但没有后端澄清 ID 和 `show-all-results` 选项 |
| `semantic_plan.core_topics[0].phrase` 超长 | 8 | 本轮实际值长 35 字符，字段上限 30；全部集中在 R001 |
| `phrases` 数组元素超长 | 1 个附加错误 | 同一个 R001 试次同时提供了 35 字符的数组元素；已包含在上面的 8 个试次中 |

30 个出错试次中，实际有 25 个设置了 `show_all_results=true` 且缺少对应澄清字段。其中 3 个同时发生短语字段错误，Pydantic 先拒绝字段，未执行后续整体澄清校验，所以显式报告该错误码的为 22 个。这里区分实际参数状态和首轮校验已经暴露的错误，不能简单把所有计数相加。

R001 的 35 字符短语都出现在 query 中，没有 Unicode 替换字符。错误来自把长查询片段放进受限短语字段，不是 Excel、PDF、编码或者金额识别问题。后续若调整构造方式，必须保留完整来源约束，不能靠直接截断文件名或短语改变查询含义。

## 3. 意图契约交付缺口

107 次完成的 Adaptive 原始输出中，41 次被当前内容回答/文件检索意图映射识别，66 次未被该映射识别。这是响应层的名称映射统计，不能解释为 66 个不同问题全部回答错误。

常见未识别名字包括 `ANSWER_FROM_DOCUMENTS`（13）、`ANSWER_FROM_DOCUMENT`（11）、`ANSWER_FROM_DOCUMENT_EVIDENCE`（9）、`FILE_SEARCH`（5）。当前支持的名字包括 `ANSWER_FROM_FILES`、`EVIDENCE_ANSWER`、`SEARCH_FILES` 等，名称相近不会自动等价。

代码中的两项事实：

1. `PlannerDecision.intent` 只要求长度 1—120 的字符串，并没有受支持名称的枚举约束。结构校验通过不代表路由映射能够识别；本轮完成的 Adaptive 响应均有接受记录。
2. `adaptive_policy.policy_context()` 把 `application_contract` 提供给改进建议器；`AdaptivePlannerService.decide()` 的实际请求只包含 message、attachments、context_documents、observation、catalog_snapshot、output_schema，没有同名的 `application_contract` 字段。三份新候选的说明多次只要求“从 application_contract 选择”，未明确携带完整的可识别名字清单。

因此，建议器与实际规划器拿到的契约信息存在交付缺口。不能仅靠反复加一句“遵守契约”保证名称正确，也不能据此证明全部错误只由这项缺口引起。

此外，Codex CLI 的 `--output-schema` 当前只约束外层 `result_json` 字符串，内部业务 JSON 的 schema 作为提示数据传入。`literal_input` 同样是普通字典，具体 Tool 的跨字段约束仍需真实输入模型校验。这说明生成端约束较宽、消费端约束较严；后端拒绝上述参数符合现有规则，不应为提高评分直接放宽闸门。

## 4. 五个超范围 Tool 试次是失败后的规则兜底

五个试次均为 `effective_mode=RULE`，工具均为 `intent-summary`，不是模型成功生成的 Adaptive 计划。它们来自调用失败后的兜底路径。当前路由评测只允许 `hybrid-search`、`managed-file-list`、`evidence-answer`，所以标记 `UNSUPPORTED_ROUTE_TOOL`。

应把它们解释为模型调用失败及兜底的结果，不能据此声称“新候选主动选择了非法 Tool”。原评分保留这些试次；不能删除它们抬高正确率。

## 5. 时间与后续顺序

活动时间约 62 分 17 秒。成功调用计时合计 2109.55 秒，失败调用合计 586.25 秒，全部调用合计约 44 分 56 秒。与活动时间相差约 17 分 22 秒，属于调用外处理；现有记录未将快照、文件核对、隔离进程及独立评分分别计时，不能把差额全部归到某一种操作。调用计时也包含 CLI 启动与通信，不等于纯模型推理时间。

建议下一步按顺序处理，尚未实施：

1. 补充有限、脱敏的连接错误类别和结构化错误码，保留失败终态与受控阶段，不记录凭证、完整网址、自由错误正文或模型推理；连续连接失败时停止本批，避免继续消耗请求。
2. 在离线实验中显式把相同的可识别意图契约交给建议器和规划器，并核对生成端与消费端的名称约束；需要更新协议和审计身份，不能改写旧实验。
3. 对具体 Tool 参数做提交前自检，明确澄清前禁用展示全部结果，长来源条件与短主题字段分别表达；不通过静默截断、伪造澄清或扩展别名粉饰旧分数。
4. 先用确定性模型替身验证，再由操作者授权小规模真实固定对照；当前不建议继续大批量搜索提示词。
