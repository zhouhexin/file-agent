# B阶段：怎样测试模型自己改进规划提示

日期：2026-11-12。开发依据：[B阶段开发方案](harness-adaptive-stage-b-plan.md)。

已完成首轮真实测试`adaptive-policy-v1`，结果NO_GAIN，原版和候选各1/2。机器反馈已补齐“最终目标、当前动作、应用意图契约”三组事实；下一次新候选请使用本文第8节的`adaptive-policy-v2`目录。旧实验保留原结论。

## 1. 这次能做到什么

现在可以走这条链：

```text
读取已经核验的A测试记录
→ 建议模型分析失败，自主写一段通用规划说明
→ 程序生成实际补丁，只追加到实验副本的原提示末尾
→ 在隔离环境中分别运行原版和候选
→ 独立比较器判断有没有改善、正常题是否退步
→ 保留候选，或者记录不修改、无收益、退步、无法判断
```

具体的补充说明由建议模型生成，没有人工预设修复供它选择。它也可以有依据地选择不修改。这属于“在明确范围内自主改进一类策略”。本次开放的只有Adaptive规划提示补充说明，不包含任意源码修改或自动上线。

A的真实结果为两题正确1/2、均由Adaptive完成、无回退。R002保留了后续回答的目标描述，但用了自创intent，程序仍按找文件路线识别；SEARCH01找文件正确。这是本阶段的真实反馈来源。首轮路线正确仍不代表实际检索和答案已经正确，那部分留给D。

## 2. 先执行检查，不调用模型

在项目根目录，使用原来的Python环境和`runtime/evidence-test/.env`。不需要改env，不需要重启服务或重新上传文件。程序不连接业务数据库或执行文件Tool。

```bash
python scripts/run_evidence_test.py harness adaptive-cycle \
  --baseline experiments/harness-v1/adaptive-baseline-v1 \
  --output experiments/harness-v1/adaptive-policy-v1 \
  --repetitions 1 \
  --max-requests 13 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

应看到`CHECK_PASSED`、`model_calls: 0`、两题、原基线`baseline_correct: 1`，以及请求上界13次。默认检查不创建输出目录。

13次是最坏上界：建议器1次，加两组×两题×每题最多3次。正常无回退时通常共5次。失败、回退、中断也占预算，不能因为没得到答案就当作免费；用量未知另外记录。建议器和规划器的输出额度分别配置，每次超时60秒，总时长含来源核对、快照和隔离检查。

若模型名称、接口地址、Catalog、真实规划源码或冻结数据发生变化，应重新建立A基线。已验收的A-v1记录有明确版本兼容，但仍逐文件核对原快照并重放真实决策，不会跳过源码或标签检查。

## 3. 正式运行模型自主建议和隔离对照

检查通过后，使用相同命令增加`--live`：

```bash
python scripts/run_evidence_test.py harness adaptive-cycle \
  --baseline experiments/harness-v1/adaptive-baseline-v1 \
  --output experiments/harness-v1/adaptive-policy-v1 \
  --repetitions 1 \
  --max-requests 13 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000 \
  --live
```

只有这一步调用已配置的真实模型。一次运行只生成一个候选，不自动重试建议器，也不自动生成第二个候选。原A记录、当前活动源码和线上模式都不改。

如果提示目录已存在，改用全新的`adaptive-policy-v2`等名称。Ctrl+C会停止并保留已开始的请求；强制终止或断电可能留下未收尾目录，不自动续跑。

模型可以选择`NO_CHANGE`。例如它认为问题超出提示补充范围，或目前证据不足，这都是允许的；但已知还有错题时，不能声称“没有错误”。建议违反范围会被拒绝，不会为了产生候选强行改写它。

## 4. 独立核对

```bash
python scripts/run_evidence_test.py harness adaptive-compare \
  --experiment experiments/harness-v1/adaptive-policy-v1
```

不再调用模型，也不改写实验。比较器会从原始记录重新验证反馈、提案、实际补丁、请求、两组源码和模型响应。只有提示常量可以不同，其余代码必须一致；利用这一约束重放原版和候选的真实规划校验，不接受自报成功。

| 结果 | 大白话含义 |
|---|---|
| `IMPROVED` | 两组都完整使用Adaptive，候选正确题数增加，正常题没有退步，安全检查通过 |
| `NO_GAIN` | 有效对照已经跑完，候选没达到改善标准；两组都错也可能得到这个结果 |
| `REGRESSION` | 有原来正确的题退步，或完整对照中发现候选安全违规 |
| `INCONCLUSIVE` | 存在失败、回退、预算不足、中断等，暂时不能判断策略效果 |
| `NOT_RUN` | 模型选择不修改，或提案被拒绝，没有运行候选对照；看`no_change_reason`或`rejection_code` |
| `INVALID_EXPERIMENT` | 产物、输入、源码或调用身份核对不通过，不采信分数 |

重点看`arms.control.correct`与`arms.candidate.correct`，以及两组`rows`中的实际路线、实际模式、回退、违规项和耗时。`known_tokens`是已知用量，`unknown_token_calls`是费用统计不完整的调用数。

`automatically_adopted`始终是false。即使IMPROVED，也只是这两题上的候选改善，不能直接视为普遍可靠或上线依据。下一步C阶段扩大题库并重复验证。

## 5. 到哪里看模型究竟改了什么

完整实验目录为`runtime/evidence-test/experiments/harness-v1/adaptive-policy-v1/`。以下均指该目录中的文件：

| 文件或目录 | 内容 |
|---|---|
| `feedback.json` | 从A原始记录重建的机器反馈；不给建议器人工预期标签或参考答案 |
| `proposal.json` | 模型诊断、修改假设、实际追加说明，或不修改原因 |
| `policy.json` | 实际追加说明、完整候选提示身份和补丁身份 |
| `candidate.patch` | 对实验源码产生的真实差异 |
| `baseline/`、`control/`、`candidate/` | 同一原始快照及原版、候选副本 |
| `checks.json` | 两组编译和五项固定legacy保护回归；不代表完整Adaptive业务质量 |
| `calls/` | 每次请求的额度、状态、耗时、已知Token及安全错误诊断 |
| `trials/` | 两组逐题计划、原始模型返回、后端校验和实际回退 |
| `comparison.json`、`summary.md` | 首次独立核对结果和简要说明 |
| `run-summary.json`、`integrity.json` | 完成/停止/中断状态及原始产物身份 |

这些是Git忽略的私有实验产物，部分内容可能包含任务文字或模型响应。普通应用日志不记录完整Prompt或原始响应。

## 6. 文件变更及所在环节

| 新增文件 | 用途 |
|---|---|
| `apps/api/app/modules/evidence_harness/improvement/adaptive_policy.py` | B的机器反馈、严格提案、受限提示补丁、隔离对照及独立比较 |
| `apps/api/app/tests/test_adaptive_policy.py` | B专项工程验收 |
| `docs/evidence-evaluation-test/harness-adaptive-stage-b-plan.md` | 开发前固定的B方案与边界 |
| 本操作文档 | 默认检查、真实运行和结果解释 |

修改`improvement/executor.py`，让可信宿主指定已验证候选的精确提示，子进程不能自行选择；修改`planning_evaluation.py`登记已验收A-v1兼容身份，其余源校验保留；命令行增加`adaptive-cycle/adaptive-compare`。README、runbook及总方案同步更新。

加在“A基线之后 → 模型提出策略 → 隔离运行原版/候选 → 独立核对”这一段。没有修改当前活动的Adaptive提示内容，没有安装新服务或改变数据库。

## 7. 已完成的工程验收

| 检查 | 结果 |
|---|---|
| 包含A、legacy自主补丁、路由、日志、原Harness等的相关回归 | 300项通过，8项OS专项按设计跳过 |
| B专项补充后的快速测试 | 32项通过，1项OS专项另行执行 |
| B真实macOS隔离 | 1项通过：生成候选、实际规划、独立重放，且env、Gold和其他实验读取被拒绝 |
| 原A/legacy隔离回归 | 专项共81项通过，其中7项为真实OS隔离，其余是重复协议回归 |
| 现有真实两题的B默认检查 | CHECK_PASSED，基线1/2，上界13次，实际模型调用0，未创建live目录 |

这些测试有重合，不能简单累加。JUnit分别在实验根目录的`planning-stage-b-related.xml`、`planning-stage-b-protocol.xml`、`planning-stage-b-os-acceptance.xml`和`planning-stage-a-os-acceptance.xml`。所有工程用例使用Fake模型，没有发起真实模型调用。

工程验收中的IMPROVED只是Fake模型用例通过，真实自主改进效果需要执行第3步后由独立比较器判断。

## 8. 补齐机器反馈后，生成一个新候选

新增反馈会让建议器同时看到：用户原始任务和模型的目标描述、首步实际规划的工具及其职责、模型intent与后端intent、JSON结构是否合格，以及这些intent能否映射为程序支持的任务目标。所有信息从已独立核验的原始记录重建，不给建议器人工标签或参考答案。

例如R002现在会明确显示：模型保留了“后续依据原文回答”的目标描述，首步工具hybrid-search负责发现文件，JSON结构校验通过，但自由文本intent无法映射应用支持的任务意图。SEARCH01也可能出现自由intent，虽然文件列表路线碰巧正确，所以“契约问题数量”不等于“错题数量”。程序不通过关键词或Gold给最终目标定语义类别。

“没有执行检索和最终回答”也会明确记录。建议器需要先分析已观察到的契约事实，不能把检索失败当成已经证实的根因；具体要不要修改、写什么补充说明，仍由它决定。

第一步在项目根执行默认检查：

```bash
python scripts/run_evidence_test.py harness adaptive-cycle \
  --baseline experiments/harness-v1/adaptive-baseline-v1 \
  --output experiments/harness-v1/adaptive-policy-v2 \
  --repetitions 1 \
  --max-requests 13 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

应为`CHECK_PASSED`、`model_calls: 0`，`feedback.schema_version`为`adaptive-policy-feedback-v2`。现有两题的`task_diagnostic_count`和`intent_contract_issue_count`均为2；原基线仍正确1/2。不会创建输出目录，不需要改env或重跑A。

第二步使用相同命令，增加`--live`：

```bash
python scripts/run_evidence_test.py harness adaptive-cycle \
  --baseline experiments/harness-v1/adaptive-baseline-v1 \
  --output experiments/harness-v1/adaptive-policy-v2 \
  --repetitions 1 \
  --max-requests 13 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000 \
  --live
```

这一步才调用真实模型。模型读取新版反馈，生成一个新候选或合理NO_CHANGE，并按原有预算完成隔离对照。它仍从A基线生成本轮候选，不自动把旧候选叠加到新提示，也不自动续跑旧B实验。

第三步只读重算新结果：

```bash
python scripts/run_evidence_test.py harness adaptive-compare \
  --experiment experiments/harness-v1/adaptive-policy-v2
```

在新目录的`feedback.json`里看每条`task_diagnostic`；`proposal.json`保存模型的新解释和实际说明。其他结果字段含义与第4节相同。

旧B仍可用`adaptive-compare --experiment experiments/harness-v1/adaptive-policy-v1`只读核对。比较器按旧反馈和旧建议器请求重建，旧NO_GAIN不会被新版反馈覆盖。新版实验绑定`adaptive-policy-experiment-v2`及反馈v2，混用或修改机器事实会被拒绝。

本次新增`improvement/planning_feedback.py`和`test_planning_feedback.py`；修改`adaptive_policy.py`的反馈、建议器规则、版本绑定及旧记录兼容，修改CLI的默认检查摘要和相应测试。接入位置为“核验A记录 → 构建建议器输入”，路由、Gold、评分方法、活动规划提示和原候选修改边界保持原样。

本次317项相关回归通过，另有1项真实OS隔离专项通过；专题快速测试47项包含在相关回归内，不重复累加。记录为实验根下的`planning-feedback-v2-related.xml`和`planning-feedback-v2-os-acceptance.xml`。真实A的新版默认检查已通过；本次没有调用真实模型生成新候选，第2步live由操作者执行。
