# 多尝试不同方案，再对较好方案多测几次

日期：2026-11-17。所有命令在项目根目录执行，沿用当前Python环境和`runtime/evidence-test/.env`中的模型、登录及代理配置。无需改env、建库、上传文件或重启服务。

已经执行`frozen-exploration-v1`并遇到`RETEST_SOURCE_NOT_COMPLETED`时，请先看[第8节](#8-遇到retest_source_not_completed后怎样继续)。新代码修复历史输入过长，不要覆盖或手改旧实验。

2026-11-18：v2/v3建议器连接失败后，已修复独立harness进程的显式代理并完成一次真实探测。请使用[代理修复文档中的v4启动命令](harness-proxy-fix.md#继续测试)，不要重复使用已封存目录。

## 1. 先让Harness尝试更多不同方案

```bash
python scripts/run_evidence_test.py harness campaign-run \
  --source experiments/harness-v1/frozen-campaign-v1 \
  --output experiments/harness-v1/frozen-exploration-v1 \
  --max-rounds 6 \
  --max-no-progress 6 \
  --repetitions 1 \
  --max-requests 366 \
  --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

先看到`CHECK_PASSED`、`model_calls: 0`，再执行同一条命令，在末尾加`--live`。检查不调用模型、不创建输出目录；开始过的目录不能再用，重跑须换新名称。

这批最多尝试6份新候选，每题在原版、候选两组各初测1次。建议器会读取已做过的开发实验，尝试提出不同的策略假设；具体改法仍由它自己决定。每份候选先冻结，测完、封存和评分后才允许生成下一份。提示词要求解释新方案与旧方案的区别，但不代表可以保证模型一定想出6种有效办法。合理不修改、重复、失败、中断或额度耗尽都可以提前结束。

当前10道开发题，请求上界为`6 × (1 + 2 × 10 × 1 × 3) = 366`。全部需要模型且没有回退时约126次；实际以记录为准。两小时是整批总时限，时间先到也会停止。Codex计数为CLI调用，内部HTTP请求数不可观测，缺失Token用量仍记未知。

默认值已是6份候选、连续无进展上限6、每题每组1次。若要探索10份，可把两个`6`都改为`10`，请求上界改为`610`；不自动增加授权。长实验可显式提高`--max-active-seconds`，上限28800秒，默认仍7200秒。

## 2. 看初测，挑值得继续验证的候选

```bash
python scripts/run_evidence_test.py harness campaign-compare \
  --experiment experiments/harness-v1/frozen-exploration-v1
```

这是零模型调用的独立重算。

| 字段 | 大白话 |
|---|---|
| `research_candidates.ranking` | 研究排序，包含新候选及可核验的开发历史 |
| `research_candidates.shortlist` | 有改善迹象、允许继续开发复测的编号 |
| `retest_exclusion_reasons` | 为什么暂不值得或不允许复测 |
| `selection.selected` | 达到原严格门槛的正式开发入选者；可能为空 |
| `selection.best_observed` | 原严格比较中的最好观察，不等于合格推荐 |

初测只有1次，不足以正式入选。参数错误或意图不匹配仍记录，可以继续观察；未知工具、写操作、越权和范围违规仍禁止获得研究资格。不会因为有研究排序，就放宽最后的Gold和安全检查。

旧`frozen-campaign-v1`保持原有比较输出；用新复测入口会按独立研究规则重新筛选，旧成绩、排名和结论不改写。

## 3. 固定较好的候选，各复测5次

搜索完整结束后执行：

```bash
python scripts/run_evidence_test.py harness campaign-retest \
  --campaign experiments/harness-v1/frozen-exploration-v1 \
  --output experiments/harness-v1/development-retest-v1 \
  --top-k 2 \
  --repetitions 5 \
  --max-requests 600 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000
```

先检查，看到`CHECK_PASSED`和`retest_candidates`，再在同一条命令末尾加`--live`。如果只有一份候选符合研究规则，就只测这一份；没有则返回`RETEST_NO_PROMISING_CANDIDATE`，不自动花费额度。

这批默认选前2份，每份每题每组5次，最多600次调用。候选共享这600次和整批时限，第二份不会重新获得一份600次额度。重复期间不调用建议器，不允许修改候选。模型、题库、原版和Profile必须一致。

若要各重复10次，把`--repetitions`改为`10`、`--max-requests`改为`1200`。先做零调用检查，再明确加live。最多可选3份，预算按`候选数 × 2 × 开发题数 × 重复次数 × 3`重算；并非保证用满或跑完。

复测也可以直接选已完整结束的`frozen-campaign-v1`：只需把`--campaign`换成该路径；但该批是否有符合研究条件的候选，必须以检查结果为准。

## 4. 独立核对复测结果

```bash
python scripts/run_evidence_test.py harness campaign-retest-compare \
  --experiment experiments/harness-v1/development-retest-v1
```

`rounds`保留每份候选的严格分数，`requests_used`是整批实际调用总数，`advisor_calls`应为0。`selection.selected`为空就没有正式入选者；`DEVELOPMENT_SELECTED`才允许继续生成正式锁。

研究锁明确标为`DEVELOPMENT_RETEST_ONLY`，只能测开发题，不能拿去测保留题，也不能当最终锁。固定预检、模型失败、回退、退步和安全问题全部参与最终严格判断。当前R035仍受固定预检限制；增加轮数并不会消除这个限制，最终仍可能没有合格推荐。

## 5. 有正式入选者才进入保留验收

只有上一步`selection.selected`不为空，才执行：

```bash
python scripts/run_evidence_test.py harness campaign-lock \
  --campaign experiments/harness-v1/development-retest-v1 \
  --output experiments/harness-v1/development-retest-lock-v1
```

随后按[原操作文档第5—6节](harness-frozen-candidate-campaign-usage.md#5-冻结后的独立开发确认)对同一正式锁进行独立开发确认和保留验收；把`--lock`换成本节新锁，输出目录也换新名称。锁定本身零模型调用，保留验收另行授权；通过后仍不自动上线。

## 6. 下一批继续自主调整

可把下一次`campaign-run`的`--source`设置为已封存的`development-retest-v1`，并指定全新输出目录和预算。程序只把开发复测的实际结果承接给建议器，保留每次尝试。独立确认子目录和保留验收结果禁止作为优化历史；不能反复利用保留题改候选后，仍把它称为独立验收。

## 7. 新记录放在哪里

- 新搜索仍使用逐轮`feedback.json`、`history.json`、proposal、补丁、冻结条件、调用、试次与封存文件；增加研究排序。
- 复测批次根目录：`batch.json`、`experiment.json`、`run-summary.json`、`comparison.json`、`integrity.json`。
- `locks/0001/lock.json`：原样候选的开发研究锁。
- `rounds/0001/`：该候选的源码副本、冻结顺序、隔离检查、原始调用、每题试次与严格比较；第二份在`0002`。

不要手动编辑封存目录，也不要往目录内添加报告。进行中的未封存或中断记录不能冒充完成结果。真实检索和最终回答质量不在本轮测试范围内。

## 8. 遇到RETEST_SOURCE_NOT_COMPLETED后怎样继续

已确认旧`frozen-exploration-v1`尝试了两份候选，第3轮建议输入为413,642字符，超过400,000宿主限制，因此是`STOPPED`。复测入口拒绝该来源符合现有规则。旧记录、两份候选及已有56次调用全部保留。

现在只把发给建议器的旧历史改为统计摘要和有限示例，完整报告仍在本地。所有历史错误参与计数，当前轮完整反馈和Tool契约不裁剪。新请求固定上限360,000字符，历史视图上限200,000字符，最多100份尝试摘要；放不下时明确拒绝，不删候选、改状态或调高宿主上限。本次是内部实验协议v3，命令和env用法不变。

先用旧批次作为历史来源，检查全新批次：

```bash
python scripts/run_evidence_test.py harness campaign-run \
  --source experiments/harness-v1/frozen-exploration-v1 \
  --output experiments/harness-v1/frozen-exploration-v2 \
  --max-rounds 6 \
  --max-no-progress 6 \
  --repetitions 1 \
  --max-requests 366 \
  --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

本机零调用检查已看到`CHECK_PASSED`，10道开发题、3批历史、7份历史候选，历史调用合计229次，第一轮建议输入108,069字符，限制360,000字符。旧第3轮单独投影后为107,838字符，原8份历史摘要全部保留，当前反馈保持相同。之后实际数字以检查输出为准。

检查通过后，在上述完整命令末尾加`--live`再执行。这是新授权的最多6份候选，不是在旧目录补写剩余轮数；输出目录开始过就必须换名。

运行完成后独立比较：

```bash
python scripts/run_evidence_test.py harness campaign-compare \
  --experiment experiments/harness-v1/frozen-exploration-v2
```

只有`stage`为`COMPLETED`才执行后面的复测检查。`STOPPED`或其他异常应先核对原因，不要手动改状态。研究候选可能来自旧批次或新批次，均从原始记录核验后使用：

```bash
python scripts/run_evidence_test.py harness campaign-retest \
  --campaign experiments/harness-v1/frozen-exploration-v2 \
  --output experiments/harness-v1/development-retest-v2 \
  --top-k 2 \
  --repetitions 5 \
  --max-requests 600 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000
```

复测检查通过，再在同一条命令末尾加`--live`。完成后用`campaign-retest-compare --experiment experiments/harness-v1/development-retest-v2`独立核对。

每轮新增`advisor-memory.json`显示建议器实际看到的历史摘要、原历史身份、输入字符数和请求身份；完整`history.json`和`feedback.json`仍在。比较器从完整历史重建摘要，不直接相信摘要中的计数。修复不改变R035预检限制、Gold、安全规则或最终入选门槛，模型调用错误也仍按实际记录。设计和文件清单见[本次修复方案](harness-advisor-history-budget-plan.md)。
