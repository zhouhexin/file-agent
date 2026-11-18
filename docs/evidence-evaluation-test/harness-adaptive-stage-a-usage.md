# A阶段：怎样测试由模型生成的规划

日期：2026-11-12。A阶段开发已完成，真实模型效果由你接下来运行live确认。

后续进展：用户已完成`adaptive-baseline-v1`，两题正确1/2。B阶段已根据真实反馈开发完成，可直接按[B操作步骤](harness-adaptive-stage-b-usage.md)继续；B从A原始记录重建反馈，不直接信下面的反馈缓存。

## 1. 这次加了什么

现在可以让现有Adaptive Planner真正用模型生成计划，再单独检查它是否选对任务路线。模型提出工具步骤，后端继续检查工具、参数、文件范围和确认要求。非法计划仍按现有流程回退。

报告会明确写出计划最终来自Adaptive、legacy还是规则。回退后选对路线，也不会算成Adaptive独立成功。没有调用模型的前置处理另外列出。

这次先建立原版基线。还没有开放Adaptive策略修改，也不调用建议器或生成补丁。原来的legacy `self-cycle`继续按原有范围运行。

本阶段只检查首轮规划，不会实际检索文件、读取正文或生成答案，因此不需要重新上传文件，不需要启动API、worker、embedding或数据库服务。测试启动入口仍会检查现有独立env的数据库名称和存储路径，但不会连接业务数据库。

## 2. 先检查，暂不调用模型

所有命令在项目根目录执行，使用之前的Python环境和`runtime/evidence-test/.env`，不用修改env里的线上规划模式。

```bash
python scripts/run_evidence_test.py harness planning-evaluate \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --planner-mode enabled \
  --output experiments/harness-v1/adaptive-baseline-v1 \
  --repetitions 1 \
  --max-requests 6 \
  --max-active-seconds 1800 \
  --planner-max-output-tokens 3000
```

正常应看到`stage: CHECK_PASSED`、`model_calls: 0`、`planner_mode: enabled`和`case_count: 2`。检查不会创建实验目录，也不会调用模型。

原冻结数据中的模式仍保留为legacy。程序只在这次实验的输入副本中指定enabled，保存原数据身份、实验模式和新输入身份，不改写题目、附件、上下文或人工标签。

`planned_request_upper_bound: 6`表示两题各最多3次：Adaptive一次，必要时legacy回退及来源参数修复最多两次。正常没有回退时通常共2次。失败和回退都占总额度，没有建议器费用。若`authorization_covers_upper_bound: false`，表示额度不足以覆盖所有备用路径，运行可能提前停止；不会自行增加授权。重复三轮的最坏请求上界是18次，需要由操作者明确调整总预算。

## 3. 确认检查通过后，运行真实基线

与上一条命令相比，只增加`--live`：

```bash
python scripts/run_evidence_test.py harness planning-evaluate \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --planner-mode enabled \
  --output experiments/harness-v1/adaptive-baseline-v1 \
  --repetitions 1 \
  --max-requests 6 \
  --max-active-seconds 1800 \
  --planner-max-output-tokens 3000 \
  --live
```

这一步会调用你配置的真实模型。每次模型调用超时60秒，隔离启动和检查也计入总时长；剩余时间不足以容纳下一次调用时先停止。Ctrl+C会保存已知调用和中断状态；强制结束进程或断电可能只留下未完成产物，比较器会标记无法判断，不自动续跑。

输出必须使用全新目录。再次测试改用`adaptive-baseline-v2`等新名称，旧实验保留。命令不会修改当前活动源码或切换线上模式。

## 4. 重新核对结果

```bash
python scripts/run_evidence_test.py harness planning-compare \
  --experiment experiments/harness-v1/adaptive-baseline-v1
```

这一步不调用模型、不执行文件工具、不写回实验。比较器核对输入、人工标签、模型调用、授权和源码身份，并用已保存的模型返回结构重放真实规划校验，不信缓存分数或自报的模式。

必须在运行基线时的同版代码和原冻结数据上重算。后续修改源码或数据后，旧实验可能返回`INVALID_EXPERIMENT`；旧记录仍然保留，但不能据此作同版验收。

| 字段 | 大白话含义 |
|---|---|
| `decision: EVALUATED` | 指定规划模式已在所有题上真正跑完，可以看逐题对错；不等于全对 |
| `decision: INCONCLUSIVE` | 出现回退、前置处理或没有跑完，不能当作完整Adaptive基线 |
| `decision: INVALID_EXPERIMENT` | 记录、输入、模式或同版代码核对不通过，不能采信分数 |
| `stage: UNFINALIZED` | 没有完整收尾记录，不自动补跑 |
| `correct` | 包括备用路径在内，最终有几题路线正确 |
| `requested_planner_correct` | 指定模式自身有几题正确，Adaptive回退后答对不算在这里 |
| `effective_modes` | 实际用了多少次`ADAPTIVE`、`LEGACY`、`RULE`、`PREFLIGHT`或未取得结果 |
| `rows` | 每题的预期路线、实际路线、真正模式、回退、违规项和耗时 |
| `failed_model_calls` | 模型请求失败数；模型返回成功但schema错误会在私有审计中体现 |
| `known_tokens` / `unknown_token_calls` | 已知用量和用量未知的调用，未知不当作零 |
| `target_file_retrieval` / `answer_quality` | 始终是`NOT_EXECUTED`，本次未测实际检索或答案 |

`PREFLIGHT`表示现有流程在模型调用前完成了处理。`fallback_kind`为`ADAPTIVE_TO_LEGACY`或`ADAPTIVE_TO_RULE`时，表示Adaptive没有独立完成。legacy仍是模型判断意图、程序组成工具步骤，不能理解成模型生成了完整步骤。

如果要观察legacy与Adaptive的差别，另开新目录运行相同命令，模式改为`--planner-mode legacy`。这只能说明切换模式后的差别；A阶段没有候选补丁，不能把模式对照称为补丁优化收益。

## 5. 结果保存在哪里

相对目录都位于`runtime/evidence-test/`下，以下文件保存在本次实验目录内：

| 文件或目录 | 保存内容 |
|---|---|
| `experiment.json` | 原冻结输入与标签身份、本次模式、实际输入身份、预算和模型身份摘要 |
| `baseline/` | 实验使用的原版代码副本 |
| `sandbox-checks.json` | 编译、越界读写、联网及启动其他进程的隔离检查 |
| `calls/` | 每次请求的开始、结束、用量和安全错误类别；中断也留痕 |
| `trials/` | 逐题计划、模型返回结构、schema结果、后端接受/拒绝和回退轨迹 |
| `run-summary.json` | 整次实验完成、停止、中断或受阻的状态 |
| `comparison.json` / `summary.md` | 首次独立核对结果及简要说明 |
| `feedback.json` | 可重新生成的逐题机器反馈，供后续B阶段使用；尚未自动接入建议器 |
| `integrity.json` | 原始产物身份校验清单 |

实验目录是Git忽略的私有产物。`trials/`可能含问题和模型返回的原始结构，只用于核对，不写入普通应用日志。它们不是无正文的可公开报告。

## 6. 新增和修改了哪些代码

新增`improvement/planning_evaluation.py`，负责独立基线、模式身份、只读重放和评分；新增`test_planning_evaluation.py`及本操作文档。

在现有`improvement/service.py`、`comparator.py`和命令行中增加入口；在`schemas.py`增加没有建议器额度的独立授权；在`isolation.py`、`worker.py`和`executor.py`接入受控模式、Adaptive请求校验与回退记账；在`routing/runtime.py`、`diagnostics.py`记录规划审计并识别正文目标；在`core/logging.py`加入只供离线只读重放使用的静默上下文，默认日志行为不变。

这些变动加在“固定任务 → 隔离规划 → 保存轨迹 → 独立核对”这一段。业务Tool、原件保护和生产配置不变。

## 7. 开发验收

使用固定Fake模型完成工程验收，没有发起真实模型请求。覆盖成功、schema错误、未知工具、文件范围错误、确认要求、澄清、回退、全模型失败、来源参数修复、前置处理、预算、中断及记录篡改。真实macOS隔离另行检查上级env和模型密钥无法被worker读取。

真实效果需要你执行第3步后判断；两题只用于冒烟，不足以说明一般可靠性。A阶段结果可用于选择B阶段真实失败目标，不能直接宣称已完成Adaptive自主修改。

本次270项相关回归通过；7项真实OS隔离测试在专项执行中通过（专项共81项，其余与前者重复）。A专项自身为23项快速测试及3项真实隔离测试。记录位于`runtime/evidence-test/experiments/harness-v1/planning-stage-a-related.xml`和`planning-stage-a-os-acceptance.xml`。现有两题默认检查已通过，原数据与Gold身份未变，没有创建live目录。
