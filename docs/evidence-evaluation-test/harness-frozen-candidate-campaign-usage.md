# 带历史、逐次冻结和统一选优：启动测试

2026-11-17更新：多候选探索和固定复测请使用[新操作步骤](harness-exploration-retest-usage.md)，现默认6份候选初测1次，再筛前2份各复测5次。下文第1—3节保留已执行的三候选实验示例，输出目录已有记录，不能原样再次启动。第4—6节的正式锁定与独立验收仍适用。

以下命令在项目根目录执行。继续使用`runtime/evidence-test/.env`中的Codex或原API配置，以及已经可用的代理。无需上传文件、重跑C、建库或重启API/worker。

本次只测试初始规划。Harness自行生成通用规划说明，程序负责冻结、隔离、预算、留档和独立比较。正式服务不会自动更新。

## 1. 先检查，不调用模型

```bash
python scripts/run_evidence_test.py harness campaign-run \
  --source experiments/harness-v1/adaptive-iteration-v2 \
  --output experiments/harness-v1/frozen-campaign-v1 \
  --max-rounds 3 \
  --max-no-progress 3 \
  --repetitions 2 \
  --max-requests 363 \
  --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

应看到`CHECK_PASSED`和`model_calls: 0`，当前来源应显示10道开发题、1批历史、2份历史候选及历史已用45次调用。检查会独立核验旧记录和本机模型配置，可能需要等待，但不请求建议或规划模型，不创建新实验目录。

`initial_preflight_case_ids`列出原来源中始终由固定预检处理的题，目前包括R035。当前允许优化的提示覆盖不到该分支，原严格“全部实际使用Adaptive”门槛仍然保留，因此含这道题时无法满足整体严格入选条件；本批仍可保留所有结果、展示观察排名，但不会给出可锁定推荐。程序不会删题或改标签求通过。

## 2. 开始真实测试

检查通过后，使用同一条命令加`--live`：

```bash
python scripts/run_evidence_test.py harness campaign-run \
  --source experiments/harness-v1/adaptive-iteration-v2 \
  --output experiments/harness-v1/frozen-campaign-v1 \
  --max-rounds 3 \
  --max-no-progress 3 \
  --repetitions 2 \
  --max-requests 363 \
  --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000 \
  --live
```

这次最多提出3份新候选，每份在原版/候选两组各重复两次。每份先冻结再测，测试结束封存并独立打分，之后建议器才能根据全部已尝试方案和具体观察调整下一版。不会因第一份候选改善就立刻停止；仍尊重不修改、重复、连续无进展、失败、中断及预算停止。

预算上界为`3 × (1 + 2 × 10 × 2 × 3) = 363`次。全部走模型且无回退时约123次，固定预检可能减少调用，回退可能增加调用；时间先到也会停止，不能保证两小时跑满上界。所有候选共用这份预算，不按轮重置。Codex计数为CLI调用，内部HTTP数不可观测，Token请求额度也不是CLI硬上限；缺失用量记未知。

若只想先测两份新候选，将两条命令的`--max-rounds`和`--max-no-progress`都改为`2`，`--max-requests`改为`242`。开始过的输出目录不能复用。

## 3. 独立比较全部候选

```bash
python scripts/run_evidence_test.py harness campaign-compare \
  --experiment experiments/harness-v1/frozen-campaign-v1
```

零模型调用，重新核验历史、候选、冻结条件、原始响应、每轮分数和最终排名，不直接相信缓存成绩。

| 结果字段 | 大白话含义 |
|---|---|
| `selection.ranking` | 本批与历史的候选记录，含可比性及是否合格 |
| `selection.selected` | 达到原有门槛、可以锁定的入选编号；没有则为null |
| `selection.best_observed` | 可比记录里分数较高的一份观察；不等于合格推荐 |
| `DEVELOPMENT_SELECTED` | 找到开发集上合格且排名靠前的方案，尚未通过独立确认或保留集 |
| `NO_ELIGIBLE_CANDIDATE` | 当前没有达到原门槛的方案，保留原版 |
| `INCONCLUSIVE` | 本批未完整正常封存，不能输出可锁定推荐 |
| `historical_requests_used` | 已结束历史的调用数，单独审计 |
| `requests_used` | 本次明确授权中新花的调用数 |
| `lifetime_requests_used` | 两者合计，历史批次去重计算 |

排名先看业务正确率和全次正确题数，再对费用完整的同条件记录比较Token与延迟。输入、模型Profile、重复数或原版不一致的历史不会冒充直接对照。回退、违规或不完整试次不会因为更便宜而被推荐。没有满足门槛时，不运行后面的锁定和确认命令。

## 4. 有合格推荐时，锁定同一份候选

只有`selection.selected`非null时执行：

```bash
python scripts/run_evidence_test.py harness campaign-lock \
  --campaign experiments/harness-v1/frozen-campaign-v1 \
  --output experiments/harness-v1/frozen-campaign-lock-v1
```

这一步不调用模型。程序重新比较后，把入选候选与来源身份写入新锁定目录，旧实验保持原样。没有合格推荐会明确拒绝，不把`best_observed`直接当赢家。

## 5. 冻结后的独立开发确认

```bash
python scripts/run_evidence_test.py harness campaign-confirm \
  --lock experiments/harness-v1/frozen-campaign-lock-v1 \
  --split development \
  --output experiments/harness-v1/frozen-campaign-confirm-v1 \
  --repetitions 3 \
  --max-requests 180 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000
```

先检查，确认通过再在相同命令末尾加`--live`。仍是10道开发题，但固定同一份入选说明重复三次，不重新调用建议器。另行明确授权最多180次，不自动消耗搜索批次的剩余额度。模型、Provider和输出额度必须与锁定Profile一致，不能用env中的模型变更许可绕过。

核对结果：

```bash
python scripts/run_evidence_test.py harness campaign-confirm-compare \
  --experiment experiments/harness-v1/frozen-campaign-confirm-v1
```

`DEVELOPMENT_CONFIRMED`表示本次独立开发对照再次证实改善。其他结果应按实际解释，不能把一次搜索分数当成稳定效果。

## 6. 最后的独立保留集验收

开发确认通过后，对同一锁定候选执行：

```bash
python scripts/run_evidence_test.py harness campaign-confirm \
  --lock experiments/harness-v1/frozen-campaign-lock-v1 \
  --split holdout \
  --output experiments/harness-v1/frozen-campaign-holdout-v1 \
  --repetitions 3 \
  --max-requests 72 \
  --max-active-seconds 7200 \
  --planner-max-output-tokens 3000
```

先检查，再加`--live`。四道保留题按两组各重复三次，最多72次；没有建议调用，不重新挑候选。最后用`campaign-confirm-compare --experiment experiments/harness-v1/frozen-campaign-holdout-v1`核对。`HOLDOUT_PASSED`才表示这次固定候选的保留验收通过，仍不自动上线，也不证明实际检索和最终回答已经通过。

确认产物不能作为`campaign-run`的历史来源。不要把保留题逐题失败再喂回优化流程并继续称该批保留题独立。

## 7. 下一批怎样承接

如需继续开发搜索，明确授权新批次，把`--source`换成已结束的`experiments/harness-v1/frozen-campaign-v1`，`--output`换成全新`experiments/harness-v1/frozen-campaign-v2`。程序只读承接开发历史并去重计费记录；旧记录不续写。也可通过`--history`增加同一C来源下的其他已核验迭代/搜索批次。

历史导入受数量和递归深度限制，循环、输入不一致、无法核验、保留确认类型等明确拒绝。新批预算需要重新明确指定，不会因为历史尚有余额而自行发起调用。

## 8. 文件在哪里

搜索根目录为`runtime/evidence-test/experiments/harness-v1/frozen-campaign-v1/`。每轮仍有`feedback.json`、`history.json`、原始及恢复后的proposal、补丁、源码副本和`trials/`；新增：

- `experiment.json.history_sources`：历史来源、完整性身份和已有费用。
- `rounds/0001/frozen-evaluation.json`：固定代码、提示、输入、Profile、预算、顺序及评分协议。
- `rounds/0001/round-sealed.json`：本轮所有试次和独立分数的封存身份。
- `comparison.json.selection`：所有候选、统一排名、入选或无合格候选的结果。
- 锁定目录的`lock.json`：入选者及来源身份；确认目录有独立冻结、调用和试次记录。

不要修改或在封存目录内新增文件。运行中的按轮记录都由程序维护；跨批次始终使用新目录。开发方案及新增文件说明见[实验管理方案](harness-frozen-candidate-campaign-plan.md)。
