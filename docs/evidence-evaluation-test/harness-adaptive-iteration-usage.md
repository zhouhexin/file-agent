# 自动诊断与自主试改：测试步骤

开发方案：[最小开发方案](harness-adaptive-iteration-plan.md)。以下命令都在项目根目录执行，路径参数相对于 `runtime/evidence-test/`。继续使用已经跑通的测试 env 和 Codex 网络代理，不需要重启 API、worker、建库或重新上传文件。

## 1. 这次程序会自己做什么

读取已经完成的 C 实验，只提取 10 道开发题；自动重验模型原来生成的工具参数，将具体约束错误交给建议模型。模型可以提出新的通用规划说明，也可以决定不修改。

提出说明后，程序在隔离副本中运行原版与候选，由独立程序核对结果。没改善时，把本轮的对错、参数错误和已尝试说明交给下一轮模型。最多运行你授权的轮数，所有轮共用总额度；成功、失败、重复、不修改或额度不足都有明确停止原因。

当前改动范围仍是“规划提示末尾的一段说明”。实际检索和最终回答不在本次执行范围内，生产服务不会自行更新。

## 2. 先做零调用检查

```bash
python scripts/run_evidence_test.py harness adaptive-iterate \
  --source experiments/harness-v1/adaptive-validation-codex-v2 \
  --output experiments/harness-v1/adaptive-iteration-v2 \
  --max-rounds 2 \
  --repetitions 1 \
  --max-requests 122 \
  --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

应看到 `CHECK_PASSED`、`model_calls: 0`、`development_case_count: 10`。这一步会独立核验旧产物和本机 Codex 登录状态，可能需要等一会，但不请求建议或规划模型，不新建输出目录。原测试配置选择 API 时，也可沿用 API；命令的 `--provider openai-compatible` 可临时覆盖 env。

`parameter_diagnostic_count` 是从真实校验器自动取出的错误数量，不是人工补写的错题原因。新实验输入及反馈不复制四道保留题。

## 3. 执行有限迭代

检查通过后，执行同一条命令，末尾加 `--live`：

```bash
python scripts/run_evidence_test.py harness adaptive-iterate \
  --source experiments/harness-v1/adaptive-validation-codex-v2 \
  --output experiments/harness-v1/adaptive-iteration-v2 \
  --max-rounds 2 \
  --repetitions 1 \
  --max-requests 122 \
  --max-active-seconds 7200 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000 \
  --live
```

这是收费模型执行，不是零调用检查。本次先每题每组跑一次，适合验证自动闭环；不等于已经证明重复稳定。两轮正常情况下约 `2 × (1 + 2 × 10) = 42` 次调用；若各题有回退修复，最多授权 122 次。这个上界不能保证两小时内完成，时间先耗尽也会停。第二轮不重新获得 122 次额度。建议器 8000 和规划器 3000 分开指定。

Codex 计数单位仍是 CLI 调用，内部 HTTP 次数不可观测；输出 Token 限制仍是请求值，不能保证原生 CLI 硬执行。返回用量照实记账，缺失标未知，不用未知当零。

若想每题重复两次，把两条命令的 `--repetitions` 都改为 `2`、`--max-requests` 都改为 `242`。通常两轮 82 次，上界 242 次；也仍受同一个两小时时限约束。已运行过的目录不能覆盖，再试需换 `adaptive-iteration-v3` 等新名字。

## 4. 独立核对，不再请求模型

```bash
python scripts/run_evidence_test.py harness adaptive-iteration-compare \
  --experiment experiments/harness-v1/adaptive-iteration-v2
```

它会重建每轮反馈、核对建议器实际输入身份、重建唯一允许的源码差异、重放原始规划响应并重新评分。缓存中的“成功”不能决定结果，旧 C 来源和活动代码均不会被它修改。

这次入口的验收范围到开发候选为止。旧 `adaptive-validation-prepare` 仍只接受原B已核验的候选目录，不能直接把新的整个迭代目录传给它。若取得开发改善，应先固定本次保留轮次，再接独立保留集验证；本次没有开放跳过该来源契约的捷径。

## 5. 结果怎么看

| 字段或结果 | 大白话意思 |
|---|---|
| `DEVELOPMENT_IMPROVED` | 在本次开发题的完整有效对照中改善，且没触发退步或违规门槛；只保留离线候选 |
| `REGRESSION` | 至少有一项配对退步、契约退步或参数/权限违规，不能接受 |
| `INCONCLUSIVE` | 缺少完整有效对照，例如固定预检、回退、调用失败、预算中断；不能据此宣称整体改善 |
| `NO_GAIN` | 有效完整对照没有增加正确数 |
| `NOT_RUN` | 没进入有效候选复测，例如模型选择不改或建议被拒绝 |
| `MODEL_SELECTED_NO_CHANGE` | 建议模型自主决定不修改，程序不会强迫它编补丁 |
| `REPEATED_CANDIDATE` / `NO_PROGRESS_LIMIT` | 重复说明或连续无进展，自动停止消费额度 |
| `non_adaptive_case_ids` | 这些题没有实际使用待优化的 Adaptive 提示，不能把它们包装成候选成功 |
| `candidate_retained` | 仅表示开发实验中保留候选，不代表已通过保留集或已上线 |
| `automatically_adopted: false` | 没有改正式服务 |

例如 R035 如果一直由固定预检处理，提示候选覆盖不到它。新反馈会自动标记 `OUTSIDE_PROMPT_POLICY`，不会改 Gold、删除该题或放宽门槛求通过。最终可能仍然无法判断整体收益，即使部分开发题变好了。

## 6. 去哪里看具体记录

目录：`runtime/evidence-test/experiments/harness-v1/adaptive-iteration-v2/`。

| 文件 | 内容 |
|---|---|
| `initial-feedback.json` | 从旧 C 开发题自动生成的初始诊断 |
| `development-inputs.json` | 实际开发题和固定 Catalog，保留题不复制进来 |
| `development-gold.json` | 仅宿主评分用的开发标签，不给模型和候选进程 |
| `calls/` | 整个循环的逐次调用、预算、脱敏错误和 Token 记录 |
| `rounds/0001/feedback.json` | 第一轮建议器收到的开发反馈 |
| `rounds/0001/history.json` | 已尝试说明及独立开发结果 |
| `rounds/0001/proposal.json` | 建议模型实际提出的说明或不修改决定 |
| `rounds/0001/reference-map.json` | 本轮短编号与完整反馈哈希的固定对应表，绑定原版源码和反馈 |
| `rounds/0001/resolved-proposal.json` | 程序精确映射后的提案，与原始回复分开保存 |
| `rounds/0001/candidate.patch` | 真正应用到隔离副本的差异 |
| `rounds/0001/checks.json` | 编译和固定 legacy 保护回归 |
| `rounds/0001/trials/` | 每题原始规划与实际模式，未执行检索 |
| `rounds/0001/score.json` | 本轮逐题独立评分；比较器会重算而非直接相信它 |
| `rounds/0002/feedback.json` | 第一轮执行后自动生成、交给第二轮的反馈 |
| `run-summary.json` / `comparison.json` | 停止原因和最终核验汇总 |

记录含私有问题和模型结构，仅在受 Git 忽略的实验目录保存。不要往已经封存的实验内部增加文件或手动修改它；这会使完整性检查失败。

本次工程验收用 Fake 模型；真实模型能否改善，要由上述 `--live` 的数据判断。开发题改善后，仍需锁定候选并进行独立保留集验收，不能把保留题的失败细节送回这一轮建议器。

## 7. 长编号漏字修复后怎么复测

原`adaptive-iteration-v1`保留原样，仍按旧协议显示`PATCH_SCHEMA_INVALID`，不能直接改它的`proposal.json`补字再假称通过。使用本文上方已更新的`adaptive-iteration-v2`命令，先零调用检查，之后显式加`--live`生成一份新建议；不需要重跑C或上传文件。

新建议里的`feedback_refs`使用`R1F001`等短编号，第二轮改为`R2F001`。模型从本轮允许列表中选择，程序严格查表恢复64位反馈哈希。源码基线用`baseline_ref: BASELINE`表示，真实源码哈希由程序绑定。两个长哈希均不用模型抄写。

如果仍有格式问题，报告的`rounds[].proposal_diagnostic`会给出阶段、字段位置和错误类型：`ITERATION_PROPOSAL_SCHEMA_INVALID`表示结构不合要求，`ITERATION_FEEDBACK_REFERENCE_UNKNOWN`表示引用不属于本轮，`ITERATION_FEEDBACK_REFERENCE_DUPLICATE`表示重复引用。程序不会猜测或自动补齐。

原有路径和命令检查仍有效，建议器已明确获知用“和”“或”表达并列，不使用斜杠列举。短引用修复只保证程序可以精确关联模型选择的反馈，不代表模型建议必然有效或复测必然改善。
