# Harness第四步：完整离线流程与实验历史反馈

日期：2026-11-10。对应[开发方案第7节第4步](feedback-driven-harness-mvp-plan.md#7-开发顺序与验收)。开发及工程验收完成，相关回归307项通过；本次没有请求真实模型。第五步的单命令真实运行仍需另行执行。

## 1. 现在能做什么

之前需要分别整理反馈、请求建议、执行复测、比较结果。现在可以用一个`harness cycle`命令完成：

```text
固定输入＋已有报告＋历史实验
→ 整理安全反馈
→ LLM提出一个建议
→ 程序校验
→ 同版控制组和候选组各三题三轮
→ 独立程序按人工标签评分
→ 保存结论，并生成包含这次结果的新反馈
```

每轮只尝试一个候选。模型说不改、建议被拒绝、建议调用失败时立即结束，不自动再问模型。改善只保留为候选；无改善、退步及中断也保留记录。人工标签、原文、原配置、旧实验和Shadow回答链均不改写。

模型能够读取历史实验的独立比较结论、预算、逐轮执行状态及脱敏失败类别。它看不到题干、文件名、原文、人工标签和人工答案。复测评估器仍按原有授权读取固定证据包；这与建议模型的输入范围不同。

## 2. 新增和修改了哪些文件

| 文件 | 本次变化 | 加在哪个环节 |
|---|---|---|
| `apps/api/app/modules/evidence_harness/service.py` | 新增 | 串联整个离线流程，检查输入，统筹预算，保存阶段、结果和完整性记录 |
| `apps/api/app/modules/evidence_harness/history.py` | 新增 | 独立重算历史实验，构建新版反馈；原反馈保持不变 |
| `apps/api/app/tests/test_evidence_harness_cycle.py` | 新增20项测试 | 验证完整流程及预算、历史、终止和只读边界 |
| `.../evidence_harness/schemas.py` | 修改 | 新增v2反馈和历史协议，保留v1序列化及兼容读取 |
| `.../evidence_harness/policy.py` | 修改 | 拒绝同条件下已完整验证为无改善或退步的同一候选 |
| `.../evidence_harness/optimizer.py` | 修改 | 验证v2反馈来源，支持完整流程的受限请求传输；原建议协议和提示词保持兼容 |
| `.../evidence_harness/executor.py` | 修改 | 接收进入复测前已经消耗的整轮时间，避免每个阶段重新领时间预算 |
| `.../evidence_harness/comparator.py` | 修改 | 独立核验累计时间字段，同时兼容已有实验 |
| `apps/api/app/scripts/evidence_harness.py` | 修改 | 新增`cycle`、`inspect-cycle`和`--previous-cycle` |
| 本文、开发方案、`README.md`、`docs/runbook.md`、第三步交付记录 | 新增/更新 | 使用、验收和进度说明 |

这是独立测试CLI中的流程，没有接入用户上传、检索或线上LangGraph节点，也没有新增数据库迁移。

## 3. 历史如何起作用

旧反馈仍是v1。包含实验历史的新快照使用`harness-feedback-v2`和`harness-optimizer-input-v2`，避免给旧反馈添加字段后破坏旧建议及实验的散列绑定。

读取历史时，程序重新检查数据、标签、实际Profile、调用记录和逐轮结果，通过独立比较器重算；不直接相信旧`comparison.json`里写的结论。来源不完整、被修改、数据不一致或Fake混入live时，停止检查，不悄悄略过。

已将你完成的`step3-live-v1`只读导入新快照：

- 原实验仍为NO_GAIN：6000 Token下，60→120秒没有提高严格匹配数，两组均6/9。
- 18条逐轮历史被保留，包含候选组R034三次输出截断。
- 同条件再次建议60→120秒会被拒绝，原因是`PREVIOUSLY_UNSUCCESSFUL_EXPERIMENT`。
- 上述拒绝通过程序构造的假设建议验收，没有冒充一次新的模型建议。
- 中断或INCONCLUSIVE只作为历史，不据此认定候选已被充分证明无效。

新快照在本机`runtime/evidence-test/experiments/harness-v1/step4-feedback-v2/`。下一次新流程可以继承上一流程的历史；它是一次新的显式实验，不是续跑旧请求。

## 4. 具体执行步骤

以下命令均在项目根目录执行，复用`runtime/evidence-test/.env`。参数中的相对路径由独立启动器按测试目录解析。无需重新上传文件，也不需要启动数据库或检索服务。

### 4.1 先检查，不调用模型

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness cycle \
  --dataset datasets/frozen-real-v1/cases-v1.json \
  --baseline datasets/frozen-real-v1/runs/live-output6000-v1-01.json \
  --history datasets/frozen-real-v1/runs/live-v1-01.json \
  --experiments experiments/harness-v1/step3-live-v1 \
  --output experiments/harness-v1/cycle-live-v1
```

预期`CHECK_PASSED`、`model_calls: 0`、`history_count: 1`。默认检查不创建输出目录。输出目录必须是新的；已有目录不能覆盖或复用。

### 4.2 明确执行一次真实流程

检查通过后，对同一条命令追加`--live`：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness cycle \
  --dataset datasets/frozen-real-v1/cases-v1.json \
  --baseline datasets/frozen-real-v1/runs/live-output6000-v1-01.json \
  --history datasets/frozen-real-v1/runs/live-v1-01.json \
  --experiments experiments/harness-v1/step3-live-v1 \
  --output experiments/harness-v1/cycle-live-v1 \
  --max-requests 19 --max-active-seconds 1800 --live
```

最多1次建议＋18次复测，整轮活跃时间上限1800秒。建议模型只读安全反馈，评估模型读取已冻结证据。失败请求也占次数；用量未返回记未知，不能当0。真实调用使用有总等待上限的子进程，无自动重试。

如果模型仍建议已验证无效的120秒，流程会停在REJECTED，只消费建议调用；这属于正常保护行为。NO_CHANGE也正常结束，不强求模型一定找到改进。

### 4.3 查看结果或中断状态

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness inspect-cycle \
  --cycle experiments/harness-v1/cycle-live-v1
```

该命令只读，不调用模型、不恢复请求。重点看`stage`、`comparison`、`total_request_attempts`、`stop_reason`、`history_count_after`和`next_feedback_source`。`automatically_adopted`应始终为false。

正常中断保留逐请求及已完成结果。强制结束进程可能来不及写最终清单，此时显示UNFINALIZED，并根据发送前记录显示可能已消费的请求数。产物不完整或被改动显示INVALID_CYCLE。不要通过反复重跑旧命令恢复。

### 4.4 如需下一轮，显式继承历史

上一轮有完整清单、来源验证通过且有`next_feedback_source`后，可以使用新目录：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_test.py harness cycle \
  --dataset datasets/frozen-real-v1/cases-v1.json \
  --baseline datasets/frozen-real-v1/runs/live-output6000-v1-01.json \
  --history datasets/frozen-real-v1/runs/live-v1-01.json \
  --previous-cycle experiments/harness-v1/cycle-live-v1 \
  --output experiments/harness-v1/cycle-live-v2
```

默认仍只检查。确认需要新一轮真实实验时再追加`--live`。程序继承上一轮快照中全部实验历史，不必手抄实验目录，也不会恢复上一轮已发送的请求。每轮预算独立，历史累计费用不被清零或声称已节省。

首批最多接收9条既有历史，为本轮留出第10条；依赖历史嵌套最多8层，模型输入最多64000字符。超过边界会拒绝，不自动删历史或继续无限搜索。当前只接受3个均可单批处理的冻结样本；扩充数据集需另行规划预算。

## 5. 新流程的产物

```text
cycle-live-v1/
  cycle.json           # 输入、标签散列、授权、历史和代码来源
  events/              # 各阶段及当时请求数、耗时
  feedback/            # 本轮建议使用的不可变v2反馈
  proposal/            # 建议、调用和校验记录
  execution/           # 实际执行后才存在：控制/候选/逐轮/比较
  feedback-next/       # 有可验证新实验时才存在：包含新增历史
  cycle-summary.json   # 整轮结论及下一反馈位置
  integrity.json       # 子产物完整性记录
  manifest.json        # 最后写入的结束标记
```

NO_CHANGE、REJECTED和建议调用失败不伪造实验或新增历史，`next_feedback_source`指向本轮初始反馈。有效中断记录可进入历史，独立评分为INCONCLUSIVE。单个实验细节仍按[第三步记录](harness-step3-acceptance.md)查看。

## 6. 本次验收结果

| 验收项 | 结果 |
|---|---|
| Fake完整闭环：改善、无改善、退步、中断 | 通过；Fake不冒充live |
| NO_CHANGE、拒绝、建议失败和建议中断 | 通过；不发起复测、不循环建议 |
| 请求与时间总预算 | 通过；建议计入，阶段间传递已消耗时间 |
| 历史回写和下一轮继承 | 通过；生成新反馈，旧实验不变 |
| 同条件重复无改善候选 | 拒绝；包含真实历史的只读检查也通过 |
| 历史篡改、伪造缓存结论、来源混用 | 拒绝或独立重算；不盲信比较缓存 |
| Gold和原文隔离、旧v1兼容 | 通过 |
| 只读检查、拒绝覆盖、中断不自动续跑 | 通过 |
| 相关自动化回归 | 307项全部通过，原有Jieba依赖提示不影响结果 |
| 真实资料核对 | 100个既有文件（含测试env）核对前后字节散列一致；旧实验独立重算仍为NO_GAIN |
| 真实单命令流程 | 本次未执行；默认检查通过，零真实模型调用 |

本机验收记录：`runtime/evidence-test/experiments/harness-v1/step4-acceptance-v1/pytest-results-final.xml`、`final-verification.json`及真实历史核对`verification.json`。这些包含本机来源信息的材料保持私有，不纳入普通日志。

复现自动化验收：

```bash
DATABASE_URL=postgresql://fake:fake@127.0.0.1/file_agent_unit_tests PYTHONPATH=apps/api \
/opt/homebrew/anaconda3/envs/py311/bin/python -m pytest -q \
  apps/api/app/tests/test_evidence_harness_cycle.py \
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

这里的数据库URL仅满足测试配置校验，测试使用Fake，不连接该数据库。本次验收证明离线工程流程可用；不代表已完成60条状态评测、策略泛化验证、检索策略优化或生产自动上线。
