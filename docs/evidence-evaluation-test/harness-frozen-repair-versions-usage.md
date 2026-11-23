# 每个冻结版本分别测试

希望先理解每一步由谁执行、何时调用模型、为什么“执行完成”仍可能“效果未定”，可先读[当前运行流程的大白话说明](harness-current-runtime-explained-20261123.md)。

本模式不选优、不自动留用，不修改旧参考方案。只有已通过安全与模拟检查的版本可以进入真实对照；分数低、同分或回归不会使版本从列表中消失。第一批补丁范围是已有白名单的规划提示常量，包含机械附带真实意图清单及参数规则，不开放任意函数体、代理、凭证或评分器修改。

沿用现有 Python 和 `runtime/evidence-test/.env`。本模式使用新的 `repair-*` 命令，不改变旧 `campaign-loop` 的历史行为。不需要更新 WorkBuddy 插件、生产 API 或数据库。

## 1. 检查冻结命令，不调用模型

在项目根目录使用 PowerShell 参数数组，不使用反斜杠或反引号续行：

```powershell
Set-Location E:\PycharmProject\file-agent
$Python = 'D:\anaconda\envs\myenv\python.exe'
$FreezeArgs = @(
    '-B', 'scripts/run_evidence_test.py', 'harness', 'repair-freeze',
    '--source', 'experiments/harness-v1/adaptive-validation-codex-v2',
    '--reference', 'experiments/harness-v1/adaptive-validation-codex-v2',
    '--diagnostics', 'E:\PycharmProject\file-agent\runtime\reference-diagnostics-review-20261120-134333-509503.json',
    '--output', 'experiments/harness-v1/frozen-repair-v1',
    '--max-versions', '3', '--max-requests', '3',
    '--max-active-seconds', '1800', '--allow-model-profile-change'
)
& $Python @FreezeArgs
```

预期 `CHECK_PASSED`、`model_calls: 0`，不创建实验目录。当前 CLI 须已完成登录；登录方法和 Windows 环境检查见 [本机 Python 操作](harness-windows-local-python-plan.md)。模型和端点只从独立测试 env 读取，不自动换配置。

原 C 来源不填 `--reference-round`；开发迭代或复测来源需要指定真实轮次。诊断文件须位于独立 runtime 目录内。已存在的输出目录必须换名，不能覆盖或在封存目录内补写文件。

## 2. 明确授权生成并冻结三个候选

```powershell
& $Python @FreezeArgs --live
```

这里的 live 只允许建议器生成最多三份补丁并运行编译及固定模拟回归，最多三次建议调用。不会自动调用真实规划模型做业务对照。三个版本可能得到 FROZEN、GENERATION_FAILED、NO_CHANGE、REJECTED、SIMULATION_FAILED 等不同状态，全部保留。模拟通过不代表实际业务质量提高。

新生成协议为 v2。单次连接或返回格式失败只标记该版本失败，在原预算内继续下一版，不重试同一版、不自动补足三份成功版本；连续基础设施失败达到阈值后停止。生成阶段遇认证、权限、额度或限流错误立即停止，避免继续花费。每个失败版本的 `failure` 字段展示固定诊断及连接线索，不再只给通用错误。

只读查看版本：

```powershell
& $Python -B scripts/run_evidence_test.py harness repair-list --experiment experiments/harness-v1/frozen-repair-v1
```

每个版本记录在 `versions/0001/` 等目录，有原始提案、`version.json`、`candidate.patch`、`simulation.json` 及两组模拟副本。冻结目录封存后只读。无赢家，无自动留用，无参考版本更新。

若返回 PARTIAL_VERSIONS_FROZEN，说明只有部分版本可测。即使整批 stage 为 FAILED，已标记 FROZEN、testable=true 的版本仍可单独测试。新读取器兼容修复前的已知 v1 批次，不修改旧文件；例如已有 `frozen-repair-v1/versions/0001` 不必重生成。不要重复运行旧冻结输出目录；如需再次生成，必须使用新的输出目录和明确的新预算。

## 3. 单独测试每个冻结版本

先检查第一个版本：

```powershell
$TestArgs = @(
    '-B', 'scripts/run_evidence_test.py', 'harness', 'repair-test',
    '--batch', 'experiments/harness-v1/frozen-repair-v1',
    '--version', '0001',
    '--output', 'experiments/harness-v1/frozen-repair-v1-test-0001',
    '--repetitions', '2', '--max-requests', '120',
    '--max-active-seconds', '3600', '--max-consecutive-failures', '3',
    '--allow-model-profile-change'
)
& $Python @TestArgs
```

检查通过，再执行同一数组并加 `--live`：

```powershell
& $Python @TestArgs --live
```

测试第二、三个版本时，把 `--version` 对应值分别改为 `0002`、`0003`，同时为 `--output` 使用新的目录。可以用多个 `--version` 显式指定几份版本。每次命令有自己明确的新授权和账本，不隐式恢复旧任务。

10 道开发题、两组、每组重复两次时，120 是单个版本的保守请求上界（每试次最多三次模型请求）。所有失败及回退也计数；请求数不是 Token 数或收费金额。每版与同一个固定旧参考交错对照，真实测试阶段没有建议器调用，不会因分数低而跳过后续版本。

## 4. 也可一次明确授权全部冻结版本

```powershell
$AllArgs = @(
    '-B', 'scripts/run_evidence_test.py', 'harness', 'repair-test',
    '--batch', 'experiments/harness-v1/frozen-repair-v1', '--all',
    '--output', 'experiments/harness-v1/frozen-repair-v1-test-all',
    '--repetitions', '2', '--max-requests', '360',
    '--max-active-seconds', '7200', '--max-consecutive-failures', '3',
    '--allow-model-profile-change'
)
& $Python @AllArgs
```

检查通过且接受总授权后才执行 `& $Python @AllArgs --live`。三版各最多 120 次，全部共用 360 次和两小时的活动预算，不给每版重新发一份总预算。版本数或题数改变时，以零调用检查输出的 `planned_request_upper_bound` 为准。

连续三次基础设施调用失败、额度耗尽、时限不足或人工中断都会停止。未完成及未开始的版本会标明 INCOMPLETE、NOT_RUN；模拟失败版本标明 BLOCKED。不能声称“全部已测试”，也不会自动重试。需要继续时，重新明确选择未完成版本并使用新目录、新授权。

## 5. 读取所有结果，不产生留用动作

```powershell
& $Python -B scripts/run_evidence_test.py harness repair-compare --experiment experiments/harness-v1/frozen-repair-v1-test-all
```

输出逐版 `status`、`score`、失败/回退及未运行分母，`selection` 始终为空，`automatically_adopted` 和 `reference_updated` 始终为 false。即便一版 IMPROVED、另一版 REGRESSION，也只分别展示，不更新任何父版本或生产程序。

模型、CLI、业务源码或生成契约在冻结后变化时，应重新冻结新批次，不把不同环境混为同一个版本的对照。原评分、预检和全部失败记录保留。活动时限沿用现有检查方式；最终只读独立复算耗时另计，不能把活动预算解释为整个进程的硬退出时限。

方案见 [逐版本冻结与测试](harness-frozen-repair-versions-plan.md)，已知问题依据见 [调用及契约错误诊断](harness-reference-failure-diagnosis-20261120.md)。
