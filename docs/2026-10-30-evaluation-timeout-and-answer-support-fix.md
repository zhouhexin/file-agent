# 评估超时调整与回答支持性校验修复

## 1. 本次处理结果

- 独立测试环境 `runtime/evidence-test/.env` 增加 `EVIDENCE_EVALUATION_TIMEOUT_SECONDS=60`。
  实际加载校验为评估60秒、普通回答180秒；私有env仍为0600、被Git忽略。
- 无密钥模板同步设置60秒，精简测试env由23项变为24项。
- 修复整页否定词误伤无关肯定结论的问题。
- 记录逐条校验的拒绝原因计数；全部被拒时，在既有预算内进行一次支持性纠错。

评估读取等待上限增长只影响测试配置，应用默认仍为20秒。
这能缓解等待上限偏紧，但不能保证消除模型服务或网络延迟。

## 2. 为什么有原文仍出现“模型没有生成可由当前原文证据支持的结论”

该提示表示本轮模型结论没有一条通过后端支持性检查，不等于文件不存在。
旧逻辑把结论的肯定/否定关系与整页原文比较。照片通知包含“原文件不进行任何修改”，
导致同页“电子版照片为jpg格式”等正确肯定事实也可能被拒绝。

只读使用真实通知第1页已存证据核对，正确的归档范围、电子格式、原件保护、打包、截止日期五项
现在都能通过；其中四项会被旧整页否定判断拒绝。
该验证使用人工摘录的忠实候选，不调用模型、不改文件或数据库。
历史请求没有保存模型原始回复和逐条拒绝原因，不能据此还原当时每条结论具体为何被拒。

## 3. 修复边界

```text
模型生成结论
→ 引用、数字、词项门槛校验（继续保留）
→ 在最相关的局部原文分句中核对否定关系
→ 有有效结论：只展示有效部分
→ 全部被拒：既有预算允许时纠错一次并重新校验
→ 仍全部不受支持：继续拒答，不返回模型的无依据内容
```

局部校验按句号、分号和分句拆分，PDF软换行不作为语义边界；保留千分位数字。
只使用词项最相关且数字一致的分句，不从低相关片段挑一个肯定句凑支持。
同分片段极性冲突时拒绝。明确的“无需/不得”等否定翻转仍会被拒绝，
“不仅/不但/未来”等非否定用法不会被简单当作禁止条款。

结构化格式修复与支持性纠错共用 `evidence_answer_repair_calls`，不另加一套预算。
默认最多一次修复，总调用仍受 `evidence_answer_max_calls` 限制；超过单次输入预算时不追加支持性纠错。
字段表格沿用原字段校验，不被改写成普通文本。纠错后仍需验证文件当前版本，不能引用生成期间被替换的来源。

## 4. 新日志

`evidence_answer.claims_validated` 记录：

- `claim_count`、`accepted_claim_count`、`rejected_claim_count`。
- `rejection_reason_counts`：固定拒绝原因码及计数。

| 原因码 | 意义 |
| --- | --- |
| `CLAIM_REFERENCE_INVALID` | 结论没有本轮可用引用。 |
| `CLAIM_NUMBER_UNSUPPORTED` | 结论数字不在所引原文中。 |
| `CLAIM_TERM_OVERLAP_INSUFFICIENT` | 与所引原文缺少足够事实词项重合。 |
| `CLAIM_LOCAL_SUPPORT_MISSING` | 无法定位到足够相关的局部支持分句。 |
| `CLAIM_POLARITY_MISMATCH` | 最相关原文与结论的否定关系不一致或存在歧义。 |

支持性纠错记录 `evidence_answer.support_repair`、结果、耗时；失败时携带安全的 `error_details`。
usage中 `support_repair_calls` 标识是否使用该修复，`llm_calls` 包含此次调用。
日志不写原始模型结论、原文、密钥或接口响应体。部分被拒时普通用户只看到固定的“仅保留通过校验的结论”说明。

项目根目录可执行：

```bash
rg --no-ignore -n 'evidence_evaluation.shadow|evidence_answer.claims_validated|evidence_answer.support_repair' runtime/evidence-test/logs/
```

## 5. 文件清单

| 文件 | 变动 |
| --- | --- |
| `runtime/evidence-test/.env` | 私有配置新增评估60秒，未提交、不展示其他值。 |
| `docs/evidence-evaluation-test/standalone.env.example` | 模板同步60秒。 |
| `apps/api/app/modules/evidence_answer/claim_support.py` | 新增：局部原文及否定关系校验。 |
| `apps/api/app/modules/evidence_answer/service.py` | 两条证据回答链复用修复入口，记录拒绝原因，复用既有修复预算；提示单条原子事实和避免自加序号。 |
| `apps/api/app/modules/llm/diagnostics.py` | 支持性纠错增加阶段 `ANSWER_SUPPORT_REPAIR`，沿用安全错误分类。 |
| `apps/api/app/tests/test_evidence_answer_claim_support.py` | 新增13项场景：无关否定、不正确否定翻转、年份混用、修复预算、持续错误拒答及日志不泄漏。 |
| 本文、README、runbook及独立测试文档 | 同步配置项数量、60秒设置、诊断与生效说明。 |

## 6. 验证和生效

相关493项回归通过。原有非法数字、伪造引用及否定翻转仍被拒绝。
测试使用fake模型/HTTP和隔离SQLite；真实通知仅作只读校验，未自动调用付费模型。

测试中个别只读就绪日志需要全局DATABASE_URL才能加载Settings，运行回归时显式提供虚假的
`postgresql://test:test@localhost/file_agent_test`，不读取真实连接或访问该数据库；业务测试仍使用隔离SQLite。

当前进程需重启才能读取新env和代码。在原API、analysis终端按Ctrl+C后，从项目根分别运行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py api
```

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py analysis
```

然后重发原问题，无需重新上传或重建索引。完整真实模型回答仍需重启后验证。
