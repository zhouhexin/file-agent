# 自动循环怎么运行

需要保留旧方案作为参考、从旧方案继续改进时，使用新增的[参考方案模式](harness-reference-candidate-usage.md)：`campaign-loop --reference ...`。它先在当前环境重测旧方案，失败不覆盖旧参考。本文后续命令不传 `--reference`，沿用原有重新探索流程及实验协议。

Windows 请先完成 [Windows 环境准备及隔离验收](harness-windows-local-python-plan.md)，再使用本文的自动循环。PowerShell 使用该文档中的参数数组写法；本文的 Bash 反斜杠换行不能直接粘贴到 PowerShell。跨平台迁移后先建立本机执行环境的验证记录，不能直接把 macOS 结果当作 Windows 的运行基线。

在项目根目录执行。沿用`runtime/evidence-test/.env`的模型和已修复代理，不需要重启服务。只优化开发集上的初始规划提示，真实检索及最终答案尚不在本轮评分范围。

## 1. 先检查

```bash
python scripts/run_evidence_test.py harness campaign-loop \
  --source experiments/harness-v1/development-retest-v4 \
  --output experiments/harness-v1/auto-loop-v1 \
  --max-cycles 2 \
  --max-rounds 6 \
  --repetitions 1 \
  --top-k 2 \
  --retest-repetitions 5 \
  --max-requests 1932 \
  --max-active-seconds 14400 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

应看到`CHECK_PASSED`、`model_calls: 0`。检查会重算旧记录及第一轮建议输入，可能较慢；不会创建新任务目录或请求模型。已开始过的输出目录必须换名。

## 2. 一次启动整个循环

检查通过后，在完全相同命令末尾加`--live`，再执行一次。

程序自动完成：读取两份候选的5次复测结果→提出最多6份不同的新候选并逐份初测→筛选未复测的有希望策略→固定原样复测→把新结果交回建议器→再进行一轮。无需手工把复测目录改成下一条探索命令。

这是整个任务的1932次调用上限和4小时执行时限，所有循环、原版、候选、建议、失败及回退共用，不按阶段重置。1932来自`2 × (6 × (1 + 2 × 10 × 1 × 3) + 2 × 2 × 10 × 5 × 3)`，实际通常更少。Codex内部HTTP调用、重试及Token硬限制仍不可完全观测，未知用量继续记未知；不把调用次数当收费金额。最后的只读独立核验不发起模型调用，耗时另计。

若先做较小规模的闭环验证，可把`--max-rounds`改为2、`--top-k`改为1、`--retest-repetitions`改为2、`--max-requests`改为484，其他条件保持。此时两循环仍有相同完整题库和原评分规则，只减少候选数及重复数。先检查再live。最多允许3循环及5000次请求、28800秒，需要显式指定，不会自动增加。

每个候选在测试期间固定，只有阶段封存后才能继续。起始复测和当前任务已经完整复测过的策略，不在本任务中重复选择。没有新的研究候选时记为跳过；模型选择不修改、重复候选、连续无进展、预算耗尽、异常或中断都会停止，并保留实际记录。不自动恢复或重新收费。

## 3. 核对进度和结果

```bash
python scripts/run_evidence_test.py harness campaign-loop-compare \
  --experiment experiments/harness-v1/auto-loop-v1
```

进行中返回`UNFINALIZED`及暂存调用数、已开始阶段数，不输出正式推荐。结束后独立重算控制顺序、来源、全部子阶段、原始响应及预算。

| 字段 | 含义 |
|---|---|
| `phases` | 每轮探索、复测或跳过的实际状态及调用数 |
| `requests_used` | 本任务新发出的总调用数，历史不重计 |
| `known_tokens` / `unknown_token_calls` | 已知用量与用量不完整的调用 |
| `research_best_paired_gain` | 开发观察中候选相对原版的最好正确率差，仅用于研究进展判断 |
| `recommendation` | 通过原严格门槛的候选和复测来源；可能为空 |
| `last_feedback_source` | 最后完整完成、可核验的反馈来源；手动启动另一个新任务时可使用 |

`COMPLETED`只表示控制流程结束。`NO_ELIGIBLE_CANDIDATE`表示仍没有达到原严格要求，不会自动挑最高分上线。R035预检、意图契约、参数错误、模型失败和回退继续参与原判定。

只有`DEVELOPMENT_SELECTED`且`recommendation`非空，才把其中`experiment`作为`campaign-lock --campaign`的来源，在任务目录外生成新的正式锁，再按原流程独立确认和保留验收。自动循环本身不读取保留题、不自动上线。

## 4. 文件放在哪里

- 根目录`loop.json`：完整任务授权、源码及模型身份、初始来源。
- `events/0001.json`：在每个阶段开始前写下的剩余额度、来源、阶段、时限和输出位置。
- `cycles/0001/exploration/`：原探索完整记录，包含每份候选、冻结条件及模型实际阅读的历史摘要。
- `cycles/0001/retest/`：原固定复测记录，后续循环使用它作为反馈来源。
- 结束后根目录`experiment.json`、`run-summary.json`、`integrity.json`、`comparison.json`：全任务封存及独立结论。

所有阶段由控制器维护。不要在封存目录里加文件或修改JSON；如需继续，使用新任务目录和明确的新预算。开发方案及文件变更说明见[自动循环方案](harness-auto-loop-plan.md)。
