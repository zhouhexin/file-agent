# 固定输入输出额度对照：具体操作步骤

日期：2026-11-09。对应[固定真实输入方案](frozen-real-input-plan.md)。本次已准备独立配置和运行入口，只执行无网络检查，没有替用户运行新的live。

## 1. 本次只比较什么

旧组已经完成，不需要再跑。新组仍使用同样三题，每题评估三次。

| 项目 | 旧组：保留的基线 | 新组：本次准备 |
|---|---|---|
| 数据集 | `datasets/frozen-real-v1/cases-v1.json` | 相同 |
| 模型 | glm-5.3 | 相同 |
| 输出Token上限 | 3000 | **6000** |
| 评估超时 | 60秒 | 60秒 |
| 输入额度、提示词、评估代码、重试设置 | 原配置 | 相同 |
| 人工标签 | R001正常；R016金额缺失；R034正常 | 相同 |
| 调用次数 | 3题×3次，共9次 | 同样9次正常评估调用，实际次数以新报告为准 |
| 报告 | `runs/live-v1-01.json` | `runs/live-output6000-v1-01.json` |

提高上限是为了验证“R034是否因为输出额度不足而截断”，不是保证新组一定成功，也不是按答案改标签。6000只是允许的上限，不代表每次必然用满。超时保持60秒；若新组改为超时，也要原样记录，不在本轮中途再改超时。

## 2. 已经准备好的文件

- `runtime/evidence-test/experiments/output-budget-6000.json`：本机独立实验配置，只含输出额度，没有复制密钥。
- `scripts/run_evidence_output_budget.py`：读取原独立测试env，只在子进程里覆盖输出额度，默认仅检查；显式加`--run`才执行live。
- `apps/api/app/tests/test_evidence_output_budget.py`：验证额外变量被拒绝、默认不运行模型、终端密钥不会继承等边界。
- 本操作文档；README、runbook及固定输入操作文档已增加入口。

独立配置的完整内容如下，不需要手改原来的 `.env`：

```json
{
  "max_output_tokens": 6000
}
```

入口在原测试目录运行，从 `runtime/evidence-test/.env` 取得原模型连接配置。只注入 `EVIDENCE_EVALUATION_MAX_OUTPUT_TOKENS=6000`，不会修改该env文件。启动前验证三题输入/标签散列与旧live报告一致，并要求除输出额度外的评估配置、代码散列全部一致；不同则停止，避免把多项改动误当成单变量实验。

不要直接在旧启动器命令前写 `EVIDENCE_EVALUATION_MAX_OUTPUT_TOKENS=6000`：旧启动器会清除终端应用配置变量，可能使你仍在测试3000。请使用下面的新入口。

## 3. 第一步：进入项目根目录

在终端执行：

```bash
cd /Users/zhouhexin/PycharmProjects/file-agent
```

无需重新上传文件、在聊天页提问、重启API或worker。本次从本地冻结包直接运行评估，不走规划、检索和最终答案生成；不连接数据库，不要求数据库服务在线。真实模型接口仍需可访问。

## 4. 第二步：先检查，不调用模型

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_output_budget.py
```

预期输出：

```text
检查通过：三题输入和人工标签一致；仅输出额度从3000变为6000；超时仍为60秒。
本次仅检查，没有连接数据库或调用模型。
```

已在当前本机执行此检查并通过。检查不能保证模型网络、额度或下一次响应一定正常；这些在live结果中记录。

如果提示基线、输入、代码或配置不同，先停止并保留提示，不修改旧报告或标签来凑通过。入口只打印差异字段名，不打印密钥与连接信息。

## 5. 第三步：正式运行新组

检查通过后，执行：

```bash
/opt/homebrew/anaconda3/envs/py311/bin/python scripts/run_evidence_output_budget.py --run
```

这一条命令等价于：加载独立6000 Token配置后，对同一 `cases-v1.json` 执行 `--mode live --repetitions 3`，另存新报告。

这一步会请求模型并消耗Token。一次执行已经包含三题各三次，不需要再把命令重复执行三遍。评估按样本顺序逐次进行，中间终端可能暂时没有新输出；按60秒单次上限估算，九次最慢可能约9分钟，另有少量处理时间。正常/失败记录以报告为准。

入口拒绝覆盖已有同名报告。若你后来再次执行看到“拒绝覆盖已有实验报告”，说明该输出名已存在，先核对现有结果；不要为了重跑删除基线或旧结果。若执行被中断且未生成报告，已经完成的模型请求仍可能产生费用，不要把缺报告当作没调用过。

## 6. 第四步：确认报告并比较

新报告位于：

```text
runtime/evidence-test/datasets/frozen-real-v1/runs/live-output6000-v1-01.json
```

旧报告继续保留：

```text
runtime/evidence-test/datasets/frozen-real-v1/runs/live-v1-01.json
```

新报告应显示 `mode=live`、`sample_count=3`、`repetitions=3`、`runtime_config.max_output_tokens=6000`、`timeout_seconds=60`。因为输出额度有意改变，`capture_config_matches_runtime=false`是预期现象；入口在调用前已要求只有这一处差异。

重点比较：

| 检查项 | 看什么 |
|---|---|
| R034是否能够完成 | `cases`中R034的三条`rounds`，分别看`evaluation_status` |
| 是否仍被截断 | 失败诊断中的`finish_reason=length`、`output_truncated`，以及JSON是否可解析 |
| 完成后是否判断正确 | 人工标签为正常；不能只因COMPLETED就认定正确，还要看是否误报MISSING、CONFLICT或OUTDATED |
| 其他题有无变化 | R001应正常，R016应识别金额缺失；逐轮比较，不只看第一轮 |
| 成本与等待时间 | 每次的Token及`duration_ms`；用量不完整时不能把0当实际零消耗 |

三种结果都应保留：新组完成且正确，说明提高输出额度对这批输入有帮助；仍截断，说明尚未解决；改成超时，说明当前60秒时限下不能完成，不能算成功。不要把重复失败的“稳定率100%”当成准确率。

完成后告诉我“6000 Token对照已执行”，我会读取新报告进行比较，无需你复制密钥、env或整份JSON。

## 7. 本次验证范围

仅新增实验入口与独立配置，不修改评估器、提示词、检索规则或Shadow边界。当前基线数据/配置一致性检查通过，实验入口及独立环境相关自动测试22项通过。自动测试使用替身验证入口，不请求真实模型。真实6000 Token组尚未运行；本方案不根据工程检查预判模型结果，也不能用这三题验收尚无阳性样本的CONFLICT、OUTDATED识别能力。
