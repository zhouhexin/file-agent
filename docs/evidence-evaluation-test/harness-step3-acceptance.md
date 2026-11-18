# Harness第三步：受控复测、独立评分与中断记录

后续更新：单命令编排及实验历史回写现已完成，见[第四步交付与操作步骤](harness-step4-acceptance.md)。本文保留第三步独立run/compare的操作方式。

日期：2026-11-09。依据：[闭环方案第7节第3步](feedback-driven-harness-mvp-plan.md#7-开发顺序与验收)。工程验收通过，相关回归287项通过。本次使用合成输入、确定性Fake、HTTP替身和无网络子进程测试，没有执行新的真实模型复测。

2026-11-10用户真实测试更新：一次建议、控制/候选各9轮已执行，独立复算NO_GAIN，19次总请求合规。建议为6000 Token下将超时60→120秒；两组均6/9匹配，R034候选组三轮仍截断，未采纳。本机核对说明：`runtime/evidence-test/experiments/harness-v1/step3-live-v1-review/summary.md`。下文“尚无真实建议/尚未live”为原开发验收时状态，不代表本次测试未完成。

## 1. 加入哪个环节

```text
第二步得到VALIDATED建议
→ 重新核验反馈、建议、基线、冻结输入与代码
→ 创建实际控制Profile和候选Profile
→ 操作者显式--live后，预留一次性预算租约
→ 同版控制组3题×3轮
→ 同版候选组3题×3轮
→ 独立程序逐轮对照Gold和引用评分
→ 改善 / 无效 / 退步 / 无法确定 / 实验无效
→ 保存结果，自动采纳始终为false
```

旧3000/6000报告仅作建议的历史依据；本步骤会重新执行同版控制组，不把旧结果冒充本轮控制组。虽然评估器和提示词保持不变，新增了进程等待控制和记录机制，因此两组均使用新的同版执行器，另存其代码散列。

## 2. 新增与修改文件

| 文件 | 变动 | 大白话用途 |
|---|---|---|
| `apps/api/app/modules/evidence_harness/executor.py` | 新增 | 核验建议、应用实验Profile、总预算、逐请求及逐轮记录、停止处理 |
| `.../evidence_harness/transport.py` | 新增 | 真实请求放到独立子进程，限制单次总等待，超时/中断后清理，不重试 |
| `.../evidence_harness/comparator.py` | 新增 | 不调用模型，重新读取Gold和所有轮次计算指标与结论 |
| `.../evidence_harness/schemas.py` | 修改 | 严格ExperimentProfile和ExecutionAuthorization |
| `apps/api/app/scripts/evidence_harness.py` | 修改 | 新增run及只读compare；run默认检查，--live才发送复测请求 |
| `apps/api/app/tests/test_evidence_harness_execution.py` | 新增 | 改善/无效/退步/中断、预算、真实120秒传参、完整性、费用未知及进程清理测试 |
| 本文、原方案、README与runbook | 更新/新增 | 使用、验收和实际范围 |

复用原EvidenceEvaluationService、SemanticChecker及VersionChecker，不修改其线上默认行为，不放宽普通env的60秒上限，不改检索、证据、标签、文件或数据库。

## 3. 实际Profile与预算

ExperimentProfile只接受原基线的输入额度、输出额度、单题调用上限、超时和零重试设置。候选从控制Profile复制且只改建议中的一个字段。

离线设置副本通过已校验Profile注入客户端；120秒实际传到请求进程，不再经过普通env的60秒裁剪。原Settings对象和env不变。输出额度实际传到max_tokens。真实Profile已做无网络注入检查；真实供应商能否成功响应仍需live验证。

ExecutionAuthorization由CLI操作者给定，不从模型建议读取：

| 上限 | 默认与硬上限 | 如何落实 |
|---|---|---|
| 总请求数 | 19 | 之前建议调用1次已计入；最多新增控制9次、候选9次 |
| 活跃时长 | 1800秒 | 计入之前建议调用耗时及本轮校验/复测活跃时间；两条命令之间的人工等待不计入 |
| 真实请求重试 | 0 | 子进程客户端固定零重试，不在父进程补发 |
| 每题每轮新请求 | 最多1 | 多批/截断包在预检查时拒绝；运行中意外第二次请求在发送前停止 |

可用`--max-requests`、`--max-active-seconds`降低本次上限，不能超过19和1800。预算不足时保存部分结果，不为了跑满样本越过授权。此初版仅适用于固定三题且每题单批，不是通用任意规模测试集授权。

每次请求发送前先扣预算、保存STARTED。剩余活跃时间不足以容纳完整Profile超时和两秒进程清理余量时，直接停止，不静默把120秒变成更短超时。独立比较及本地最终汇总不再发模型请求，单独在复测结束后完成；活跃时限不是操作系统对任意磁盘故障的硬终止保证。

真实请求的父进程按Profile限制总等待，包含进程启动；超时后terminate/join，必要时kill，清理最多另等两秒。这样不会只依赖HTTP每次读取的超时而被持续小块响应无限拖延。客户端退出不证明供应商停止生成或没有扣费；失败未知用量不记成零成本。

### 同一建议不能重复领取预算

在`experiments/harness-v1/_execution-leases/`按数据集、建议内容和来源生成一次性租约，先排他预留再发送请求。同一建议即使换输出目录或并发运行，也不能再次取得预算。中断、预算耗尽或无调用的提前停止都不自动释放租约，避免不确定计费状态下重复发送。

当前没有恢复/重试命令。不要删除租约或复制改写旧记录来重跑；先检查已有记录。后续如需人工批准的恢复策略，另行设计可追溯的剩余预算与请求去重协议。本步骤不实现该能力。

## 4. 独立比较器如何评分

比较器不读取模型的“预计有效”作为评分，也不相信已有摘要中的首轮成功数。它从冻结数据集重新读取Gold，按每题每轮重新计算：

- COMPLETED且问题类型和引用集合与Gold匹配，才算严格通过；失败空issues不算正常。
- 每组分母固定9；未执行、部分完成和失败都不能从分母删除。
- 每题、每轮以及整体的NORMAL/MISSING/CONFLICT/OUTDATED P/R/F1；没有正例时Recall为null。
- 已有正常题是否退步；任何题的严格匹配数或完成数下降，都记录为退步。
- 完成与失败各自耗时、整体及请求耗时p50/p95。
- 依据请求记录计算已知Token小计、未知用量请求数，以及包含建议请求的总已知消耗。未知部分不能当零。

独立核验数据/标签散列、建议绑定、单变量、实际Profile、引用、每轮序号、全局请求序号、租约和完整性清单。篡改或结构不一致输出INVALID_EXPERIMENT。

| decision | 判定 |
|---|---|
| IMPROVED | 两组完整执行，候选严格通过数增加且没有题退步 |
| NO_GAIN | 两组完整执行，但严格通过数没有提高；两组都连续失败也属于无改善 |
| REGRESSION | 候选严格通过数减少，或某题完成/通过数下降，不能被另一题改善抵消 |
| INCONCLUSIVE | 中断、预算停止、未完成、缺少最终清单或超出活跃时限，不能做完整结论 |
| INVALID_EXPERIMENT | 输入、标签、Profile、日志绑定或完整性存在问题 |

7/9优于6/9可以记录局部改善，但不满足复核门槛。只有9/9严格通过、原题不退步且预算合规，才满足`meets_review_criteria`；Fake结果不会标为`live_review_candidate`。用量不完整时费用仍不确定；不能说“整体最优”或“更省钱”。增加等待后完成率改善也不等于推理变快。

所有结果`automatically_adopted=false`，不改生产配置或ACTIVE Skill。

## 5. 用户如何测试

### 5.1 先取得真实VALIDATED建议

第三步只能读取第二步完整产物。当前开发验收时尚未发现`step2-live-v1/manifest.json`，因此没有真实可执行建议，也没有代用户执行复测。

若尚未运行第二步，在项目根执行：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness propose \
  --feedback experiments/harness-v1/step1-real-v1 \
  --output experiments/harness-v1/step2-live-v1 --live
```

这会请求一次建议模型。只有VALIDATED才进入第三步；NO_CHANGE、REJECTED、OPTIMIZER_FAILED均停止，不伪造一个建议继续。已有目录先检查，不重复请求。

### 5.2 先检查，零新增模型请求

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness run \
  --feedback experiments/harness-v1/step1-real-v1 \
  --proposal experiments/harness-v1/step2-live-v1 \
  --output experiments/harness-v1/step3-live-v1
```

应显示CHECK_PASSED、两组实际Profile、计划新增18次及已有建议1次。检查不创建目录或租约，也不调用模型。Fake建议不能从真实CLI执行，源代码/配置与基线不符时停止。

### 5.3 显式执行复测

检查通过后，同一命令增加`--live`：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness run \
  --feedback experiments/harness-v1/step1-real-v1 \
  --proposal experiments/harness-v1/step2-live-v1 \
  --output experiments/harness-v1/step3-live-v1 --live
```

这一次命令已经包含控制组三题三轮、候选组三题三轮，最多新增18次模型请求。它将冻结原文证据发送到原配置授权的模型，无需重传、前端提问、重启API/worker或启动数据库服务。

两组按顺序运行，终端可能长时间没有新输出；进度保存在calls/rounds中。正常Ctrl-C或SIGTERM会尝试清理请求进程并保存中断记录。SIGKILL、掉电等可能只留下STARTED，表示请求可能已发生，不能假定零费用或自动续跑。

### 5.4 只读检查或重新计算

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness compare \
  --experiment experiments/harness-v1/step3-live-v1
```

compare不请求模型、不修改任何记录。对未发布最终清单的实验返回INCONCLUSIVE与UNFINALIZED，此时可能是仍在运行或被硬中断，不能自行恢复。INVALID_EXPERIMENT返回非零退出码；已中断/停止的run也返回非零。COMPLETED只表示复测循环完成，效果仍看comparison字段。

## 6. 本机产物

```text
experiments/harness-v1/step3-live-v1/
  experiment.json              # 输入、Gold、建议绑定、Profile、授权、代码版本及租约
  control/
    calls/002.started.json      # 此前建议已占序号1；先记开始再请求
    calls/002.finished.json     # 返回/失败/中断、耗时及已知用量，无原文或密钥
    rounds/<案例散列>-1.json    # 本轮真实评估结果、输入散列及请求关联
  candidate/                   # 同结构
  run-summary.json             # 完成/停止/中断及原因、总请求数、活跃时间
  integrity.json               # 正式比较使用的完整性清单
  comparison.json              # 独立Gold评分、每轮指标与结论
  manifest.json                # 最终执行状态、比较结果、不自动采纳
```

JSON文件0600，各层实验目录0700，不覆盖历史。凭证只留在运行时和进程内存IPC，不放入文件、Shell参数或模型Prompt。普通日志不写业务原文。

## 7. 按方案验收

| 第三步要求 | 结果及证据 |
|---|---|
| Fake跑出改善、无效、退步、中断 | 均通过，另覆盖无效实验及连续失败 |
| 总预算在每次请求前生效 | 额度3包含既有1次，仅新增2次便停止；意外同轮第二次请求也被阻止 |
| 相同建议不得换目录续领预算 | 一次性租约及测试通过 |
| 活跃时间耗尽停止 | 剩余不够完整超时则零新请求；执行后耗尽不发送下一次 |
| 实际Profile，120秒不被裁成60 | Settings副本、实际请求工厂、IPC worker传参和真实反馈零网络注入检查通过；原Settings不变 |
| 逐轮Gold评分和失败分母 | 每组9个位置；空失败不当正常；正常题退步优先发现；7/9不达复核门槛 |
| Gold不进入评估Prompt | 检查实际Fake收到的载荷，不含expected_issues与Gold解释 |
| 原数据不改、结果不覆盖 | 路径、来源、散列核验；重复输出或已消费建议拒绝；独立比较只读 |
| 用量未知不当0 | 请求记录计数并显示unknown_usage_requests，费用不完整不宣称节省 |
| 中断、硬停止、请求进程清理 | 保存已完成轮次与开始/中断记录；缺少最终清单不视为完成；子进程等待超时终止测试通过 |
| 不上线、不改变Shadow | 原证据回答、评估集成、固定输入及诊断相关回归通过；Fake不变成真实候选 |

自动测试及相关回归287项通过，有一条既有Jieba依赖弃用提示。JUnit和工程验收文件位于`runtime/evidence-test/experiments/harness-v1/step3-acceptance-v1/`。真实资料目录JSON和测试env在无网络Profile检查前后散列一致。

复测命令：

```bash
DATABASE_URL=postgresql://fake:fake@127.0.0.1/file_agent_unit_tests \
PYTHONPATH=apps/api /opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q \
  apps/api/app/tests/test_evidence_harness_execution.py \
  apps/api/app/tests/test_evidence_harness_optimizer.py \
  apps/api/app/tests/test_evidence_harness_feedback.py \
  apps/api/app/tests/test_evidence_test_environment.py \
  apps/api/app/tests/test_frozen_evidence_inputs.py \
  apps/api/app/tests/test_evidence_evaluation_diagnostics.py \
  apps/api/app/tests/test_evidence_output_budget.py \
  apps/api/app/tests/test_evidence_evaluation_integration.py \
  apps/api/app/tests/test_evidence_answer.py
```

本轮也修正了新CLI测试中配置替身缺少cache_clear的夹具问题，未修改全局conftest或业务配置来绕过。最新完整回归无失败或错误。

## 8. 当前边界

已可从有效建议进行受控复测与独立比较；尚未提供一条命令自动串起反馈生成、建议调用和复测，也不实现自动多候选搜索、失败续跑、自动发布或检索策略自优化。这些分别属于后续完整离线编排和B阶段。现有三题仍不能验收CONFLICT、OUTDATED的阳性识别能力；Fake改善也不代表真实模型性能已提高。
