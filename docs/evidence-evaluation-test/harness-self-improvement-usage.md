# Harness自主改代码：测试步骤与结果说明

日期：2026-11-11。四阶段最小开发及工程验收完成，真实模型效果待测试。

## 1. 现在能做什么

已经接通：读失败记录 → 模型提出代码修改 → 在隔离副本应用 → 固定检查和同题对照 → 独立打分 → 把结果交给下一轮。

第一版只允许给`app/modules/agent/planner.py`中“优先找文件”的那处判断增加保护条件。条件由模型自己生成，程序负责检查、形成`candidate.patch`并应用。测试答案、评分器、其他分支和预算不能修改。

本次用固定假模型和真实macOS隔离、真实补丁、真实LangGraph规划节点完成工程验收，没有发起真实模型请求。当前程序的实际路由分支没有改动，真实模型是否能提出有效补丁需要后续live测试。

不需要重新上传文件、重建库、安装服务或启动API/worker。继续使用原Python环境和`runtime/evidence-test/.env`；运行时不连接业务数据库。首版只支持当前两题所用的legacy规划模式，其他模式明确拒绝。

## 2. 已准备好的机器反馈

以下两份已经生成，不用再生成一次，模型调用为0，原实验保持不变：

- `runtime/evidence-test/experiments/harness-v1/root-cause-routing-v3-v1/`：模型理解正文任务，却被后端转成找文件。
- `runtime/evidence-test/experiments/harness-v1/root-cause-routing-v2-v1/`：模型调用失败，后面使用了备用规则。

每份的`root-cause-feedback.json`是程序读取的反馈，`source.json`用于核对来源。旧记录缺少实际规则轨迹时会写明缺失，后续新运行会补齐实际触发规则。

以后整理新的实验时，在项目根目录执行下面的命令。输出目录必须换成未使用过的名字，不能覆盖已有记录：

```bash
python scripts/run_evidence_test.py harness self-feedback \
  --experiment experiments/harness-v1/routing-live-v3 \
  --output experiments/harness-v1/换成新的反馈目录名
```

后续所有命令同样在项目根执行，参数路径相对于`runtime/evidence-test/`。

## 3. 先检查，不调用模型

```bash
python scripts/run_evidence_test.py harness self-cycle \
  --feedback experiments/harness-v1/root-cause-routing-v3-v1 \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --output experiments/harness-v1/self-live-v1 \
  --max-rounds 2 \
  --max-requests 26 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

看到`CHECK_PASSED`表示反馈、数据和预算可以使用。没有`--live`时不会调用模型，也不会创建输出目录；本次已执行此项检查并通过。

两题、每组一遍、最多两轮，最坏上限是26次请求：每轮1次补丁建议，加两组各两题、每题最多3次规划。正常每题只调用一次时，每轮通常5次；提前停止会更少。建议、回退、失败和跨轮次请求共用26次上限。

建议器输出额度8000、规划器3000分别设置，两组规划额度相同。单次模型等待60秒、无内部重试；整个流程最多1800秒。模型不能自己增加额度。

## 4. 真正开始实验

给上面的完整命令末尾追加`--live`即可。真实运行先检测系统隔离，再发模型请求；隔离副本拿不到人工答案、评分代码或模型密钥。

每轮依次进行：生成候选、范围检查、建立原版/修改版、编译、固定回归、两题对照、独立评分。第二轮读取第一轮事实和结论，不能读取人工答案。每个候选都与同一原版比较，不自动叠加前轮补丁。

达到改善标准、建议不改、补丁越界、重复补丁、连续失败、无进展上限或预算上限时会停。Ctrl+C保留已有记录；重新实验需用新输出目录，不自动补发旧请求。

建议直接运行`self-cycle`。不要先执行一次`self-propose --live`后期待cycle复用它：cycle会自行生成本轮候选，以保持源码及预算绑定一致。

## 5. 看哪里

先打开`runtime/evidence-test/experiments/harness-v1/self-live-v1/summary.md`，查看大白话结果、各轮对错数量、Token、耗时和停止原因。

| 文件或目录 | 作用 |
|---|---|
| `experiment.json`、`baseline/` | 本次授权和实际工作树源码快照，包含未提交代码，排除私有资料和评分器 |
| `sandbox-checks.json` | 越界读写、联网、启动其他程序是否被系统阻止 |
| `calls/` | 每次请求的发送记录、实际额度、用量及安全错误 |
| `rounds/0001/feedback.json`、`history.json` | 本轮使用的机器反馈和前轮结论 |
| `rounds/0001/proposal.json`、`candidate.patch` | 模型建议及真正应用的源码差异 |
| `rounds/0001/control/`、`candidate/` | 同一基线的原版与修改版 |
| `rounds/0001/checks.json`、`trials/` | 固定检查和逐题实际规划结果 |
| `rounds/0001/round-summary.json` | 本轮在哪一步结束，是否有执行错误 |
| `comparison.json` | 独立比较器的结果，不是模型自评分 |

第二轮在`rounds/0002/`。只读重算不调用模型：

```bash
python scripts/run_evidence_test.py harness self-compare \
  --experiment experiments/harness-v1/self-live-v1
```

| 结果 | 大白话含义 |
|---|---|
| `IMPROVED` | 本次题目全部正确、有改善、正常题不退步，固定检查通过 |
| `NO_GAIN` | 条件足以比较，但未达到上述改善标准；可看逐题是否有局部变化 |
| `REGRESSION` | 正常题退步，或回归/安全检查不通过 |
| `INCONCLUSIVE` | 模型失败、降级、预算中断等使本次不足以下结论 |
| `NOT_RUN` | 没有执行可比较补丁，例如建议不改或补丁被拒绝 |
| `INVALID_EXPERIMENT` | 记录、源码、标签或其他绑定被改过，不能使用结论 |

## 6. 单独测某一阶段

只验证本机隔离，不调用模型：

```bash
python scripts/run_evidence_test.py harness self-sandbox-check \
  --output experiments/harness-v1/self-sandbox-v1
```

只生成候选、不执行候选，默认只检查，追加`--live`才调用一次模型：

```bash
python scripts/run_evidence_test.py harness self-propose \
  --feedback experiments/harness-v1/root-cause-routing-v3-v1 \
  --output experiments/harness-v1/self-proposal-v1 \
  --advisor-max-output-tokens 8000
```

## 7. 目前的边界

首版使用macOS自带`sandbox-exec`，不可用时停止，不安装或更改系统配置。独立目录本身不当作隔离：已实际验证越界读写、联网和启动其他程序被阻止。固定回归保护正常找文件、明确附件、缺少源文件的改名、分类和带年份检索，规划图不执行实际文件动作。

本次只评价“首先走哪条路线”。即使`IMPROVED`，也没证明实际检索找到了目标文件、证据足够或最终答案正确。R002与SEARCH01用于验证闭环，扩大范围还需独立样本。通过后仅保留候选，不自动写回当前源码、合并或上线。

## 8. 新增了哪些文件、加在哪里

下表的`improvement/`和`routing/`都位于`apps/api/app/modules/evidence_harness/`。

| 新增文件 | 大白话职责 | 所在环节 |
|---|---|---|
| `routing/diagnostics.py` | 记录模型想做什么、程序最后做了什么、用了哪条备用规则 | 原首轮规划过程内，只观察 |
| `improvement/__init__.py` | 独立自主补丁模块入口 | 离线Harness |
| `improvement/feedback.py` | 把历史失败整理成模型能读取的事实 | 第一阶段 |
| `improvement/schemas.py` | 固定补丁格式和操作者预算 | 第二至第四阶段 |
| `improvement/patches.py` | 给模型提供相关源码，检查修改并生成真实差异 | 第二阶段 |
| `improvement/isolation.py` | 复制真实源码、限制访问权限和进程等待 | 第三阶段 |
| `improvement/worker.py` | 在受限制的副本里编译、检查和规划 | 第三阶段 |
| `improvement/executor.py` | 统一请求预算、固定回归和模型请求代理 | 第三至第四阶段 |
| `improvement/comparator.py` | 读取人工答案独立打分，不信模型自评分 | 第四阶段 |
| `improvement/service.py` | 串起各步骤和多轮反馈，保存大白话回执 | 第四阶段 |
| `apps/api/app/tests/test_harness_self_improvement.py` | 验证四阶段及失败、安全、隔离边界 | 工程验收 |
| 本操作文档 | 提供可直接复制的检查、live及只读重算步骤 | 测试入口 |

本次还修改了`routing/runtime.py`接入离线诊断、`apps/api/app/scripts/evidence_harness.py`加入5个`self-*`命令，以及四阶段方案、测试首页、原路由方案、根README和runbook。

相关回归176项通过；另外4项真实OS隔离测试通过。完整自主模块本机验收32项通过，其中包括与回归重复的28项，不把重复执行重复计数。真实模型调用为0。

## 9. `.env`隔离问题修复后的复测

首次真实`self-live-v1`中，模型成功提出补丁，但公共配置加载器向上读取`.env`时被隔离规则阻止，两题都未复测，最终为“无法判断”。本次已修复隔离启动：宿主明确交付固定测试配置，worker在导入日志和规划组件前绑定该配置；不寻找上级`.env`，不读取宿主环境中的应用设置。隔离权限保持不变，模型密钥继续只留在宿主。

不需要修改`.env`、重新上传或重启服务。保留旧实验，项目根目录执行下面的检查命令，看到`CHECK_PASSED`后末尾追加`--live`进行新一轮真实实验：

```bash
python scripts/run_evidence_test.py harness self-cycle \
  --feedback experiments/harness-v1/root-cause-routing-v3-v1 \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --output experiments/harness-v1/self-live-v2 \
  --max-rounds 2 \
  --max-requests 26 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

本次改变了执行器源码版本，因此旧`self-live-v1`保留为旧版运行记录，不回写原补丁、原评分或完整性清单；新版比较器严格核验执行器版本，不能把旧版运行当作新版验收。新结果查看`self-live-v2/summary.md`，只读重算使用`self-compare --experiment experiments/harness-v1/self-live-v2`。

修复仅修改`improvement/isolation.py`（明确传入配置）、`improvement/worker.py`（业务组件导入前绑定配置）和原验收测试，并同步本节、第三阶段方案、README及runbook，没有新增服务或更改普通API的配置加载方式。

修复验收：82项相关回归通过；另外4项真实OS隔离测试通过。自主模块完整本机验收40项通过（包含重复的36项，不重复计算）。确认上级有`.env`时两轮Fake复测能够完成，直接读取该文件仍被拒绝。`self-live-v2`默认检查已通过，尚未发起真实模型调用。

## 10. 建议器现在怎样区分“没收益”和“没错误”

根据[HarnessDev提示词改进方案](harnessdev-advisor-prompt-recommendation.md)，新版建议器同时读取三类信息：独立程序给出的任务对错、调用及隔离是否正常、过去实际尝试过什么。两组一样但都答错，会保留“没有收益，同时错误仍存在”的事实，不会只因为接口没报错就把任务当作正确。

机器反馈使用`root-cause-feedback-v2`，新增`task_outcome`（正确/错误/未评分）、`execution_state`和`change_kind`（提示策略/代码补丁）。未运行不算已经答错；模型失败后产生的最终计划若可以评分，对错与执行异常分别记录。建议模型能看到开发集安全对错摘要，看不到预期路由、Gold理由、参考答案或保留集逐题信息。

建议使用`patch-proposal-v2`，分别记录`diagnosis`（问题判断）、`evidence_summary`（简短依据）、`hypothesis`（可检验假设）和`no_change_reason`（不修改原因）。仍允许证据不足、超出修改范围或没有可行动问题时不改；声称“相同代码候选已被验证无效”，还需引用实际有效的代码实验。提示策略没收益、模型失败或隔离失败均不能替代这个依据。

如果模型引用已知任务错误却说“没有问题”，会以`NO_CHANGE_CONTRADICTS_TASK_FAILURE`保存拒绝原因；如果没有对应有效代码历史却声称已经证伪，会以`NO_CHANGE_VERIFIED_ATTEMPT_MISSING`拒绝。两种情况都不复测、不强制改成补丁、不自动追问或重新请求模型。合法NO_CHANGE仍正常结束，具体原因同时写入`summary.md`。

本次修改的代码文件仍在`evidence_harness/improvement/`：

| 文件 | 改在哪里 |
|---|---|
| `feedback.py` | 原始实验→机器反馈：投影独立任务评分，旧v1先核验再在内存重建v2 |
| `service.py` | 反馈→模型→下一轮：更新提示词、请求、历史及大白话回执 |
| `schemas.py` | 模型输出：新增问题判断、假设及不修改原因字段 |
| `patches.py` | 应用前校验：检查依据是否与已知评分或真实历史矛盾 |
| `comparator.py` | 独立验收：重建评分摘要、历史和模型实际请求身份 |
| `apps/api/app/tests/test_harness_self_improvement.py` | 保护“两组都错”“两组都对”、合法不改、非法依据、旧反馈兼容及篡改检查 |

新实验协议为`self-improvement-v2`。旧v1根因反馈可继续用于新请求，程序从同一来源重算新摘要，不改写旧反馈文件；旧自主补丁实验仍是旧版运行记录，不伪装为新版验收。

无需重新上传、改env或重启服务。使用新目录，先检查：

```bash
python scripts/run_evidence_test.py harness self-cycle \
  --feedback experiments/harness-v1/root-cause-routing-v3-v1 \
  --dataset datasets/routing-real-v1/cases-v1.json \
  --output experiments/harness-v1/self-live-v3 \
  --max-rounds 2 \
  --max-requests 26 \
  --max-active-seconds 1800 \
  --advisor-max-output-tokens 8000 \
  --planner-max-output-tokens 3000
```

默认检查已通过，模型调用0次；你需要真实测试时末尾追加`--live`。新结果看`self-live-v3/summary.md`，只读重算用`self-compare --experiment experiments/harness-v1/self-live-v3`。改进提示词和测试通过，不保证真实模型每次都生成有效补丁；真实效果仍由显式live评测判断。

本次206项相关回归通过，另4项真实系统隔离测试通过；完整自主模块55项通过（其中51项重复计入相关回归，不重复计算）。没有执行真实模型调用。
