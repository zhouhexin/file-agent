# 保留旧方案，测试新的改进候选

本模式只改离线实验，不更新 WorkBuddy 插件或生产填表功能。旧实验目录保持原样。先读取完整的旧说明，在当前环境重新测量，然后从保留说明做小范围修改；新候选失败时保留旧方案。

## 1. Windows 检查命令（不调用模型）

项目根目录执行，使用已安装依赖的现有 Python。以下参考的是此前开发集 14/30 的旧固定候选，不代表它已经通过全部检查。

```powershell
Set-Location E:\PycharmProject\file-agent
$Python = 'D:\anaconda\envs\myenv\python.exe'
$HarnessArgs = @(
    '-B', 'scripts/run_evidence_test.py', 'harness', 'campaign-loop',
    '--source', 'experiments/harness-v1/adaptive-validation-codex-v2',
    '--reference', 'experiments/harness-v1/adaptive-validation-codex-v2',
    '--output', 'experiments/harness-v1/windows-reference-loop-v1',
    '--allow-model-profile-change',
    '--strategy', 'mixed', '--fresh-every', '4',
    '--max-cycles', '2', '--max-rounds', '2',
    '--repetitions', '1', '--top-k', '1', '--retest-repetitions', '2',
    '--max-requests', '604', '--max-active-seconds', '7200',
    '--advisor-max-output-tokens', '8000', '--planner-max-output-tokens', '3000'
)
& $Python @HarnessArgs
```

预期 `CHECK_PASSED`、`model_calls: 0`，不会创建输出实验目录。旧候选与本机执行 Profile 不同时，显式允许新实验更换环境；本次原始提示、旧方案和新候选均用同一个当前 Profile 运行，不拿旧成绩直接决定替换。已有输出目录必须换新名称，不会自动覆盖或恢复。

若参考的是开发搜索或复测批次，改为对应目录，并额外指定 `--reference-round 1` 等真实轮次。只接受已核验的开发来源；保留验收目录不能用于优化。首次原 C 验证来源不填轮次。

10 道开发题下，604 次是上述参数的保守上界：首次对照 `2×10×2×3`，两循环各最多 `2×(1+2×10×1×3)+1×2×10×2×3`。实际可能更少。回退最多三次请求，所有失败也占额度；604 不是 Token 数或收费金额。可以降低预算，额度不足就停止，不会跳过首次对照或自动续费。

沿用原活动时限检查；初始来源核验、快照和模型阶段计入活动时间，最终只读独立复算耗时另计。来源链核验和文件散列可能较慢，不能把两小时理解为整个进程一定在两小时内退出。本轮没有增加后台重试或突破该限制。

## 2. 明确开始真实实验

检查通过并确认接受预算后，执行：

```powershell
& $Python @HarnessArgs --live
```

顺序为：原始提示与旧方案固定重复对照 → 生成新候选 → 当前保留方案与候选初测 → 有改善信号时固定重复复测 → 严格满足条件才更新本次研究起点。永久旧参考不会覆盖，正式生产策略不会自动启用。

`mixed` 默认前三份从当前保留说明修改，第四份从原始提示探索；少于四份候选时可能没有 fresh。也可显式选择 `--strategy inherit` 或 `--strategy fresh`。fresh 仍然读取旧参考，并与当前保留方案对照。模型输出完整替代说明，仍最多 3000 字符，避免逐代追加。

为控制成本，每循环按生成顺序复测至多 top_k 份有提升信号的候选，本模式不按初测高分重新排序。初测信号不等于合格，原预检、失败、回退和全部题目继续保留。每次重复都提升且通过原严格门槛才替换；同分或有退步就继续保留旧方案。

## 3. 查看结果

```powershell
& $Python -B scripts/run_evidence_test.py harness campaign-loop-compare --experiment experiments/harness-v1/windows-reference-loop-v1
```

比较只读，不请求模型。进行中返回 UNFINALIZED；结束后重新核验真实记录。

| 字段 | 含义 |
|---|---|
| `reference` | 永久旧参考的说明与身份 |
| `retained` | 本次研究当前保留的说明，失败时不会被较差方案覆盖 |
| `reference_qualified` | 旧参考在本轮首次对照是否通过原严格门槛；false 仍可作为研究参考 |
| `promotions` | 真正通过重复对照后更新研究起点的记录；可能为空 |
| `phases` | 首次对照、初测、复测的完整分数，含失败和未运行分母 |
| `requests_used` / `known_tokens` / `unknown_token_calls` | 本次统一账本，未知费用不能当成零 |

`NO_ELIGIBLE_CANDIDATE` 表示没找到可替换方案。`DEVELOPMENT_SELECTED` 仅表示新候选通过本次开发对照，仍未运行保留验收，也未上线。新协议当前不接旧 `campaign-lock`，不能把新目录强行作为旧协议锁定来源。

新目录中，`reference-policy.json` 保存旧参考；`reference-evaluation/` 保存当前环境首次测量；`attempts/0001/context.json` 与 `lineage.json` 保存父版本和修改；同目录下 `exploration/`、`retest/`、`replacement.json` 保存实际对照和替换依据；根 `retained-policy.json` 保存最终研究起点。不要修改封存文件；下一次实验使用全新目录和新授权。

## 4. 开发自测（不调用真实模型）

在单独 PowerShell 测试窗口执行：

```powershell
$env:PYTHONUTF8 = '1'
$env:DATABASE_URL = 'postgresql://offline:offline@127.0.0.1/file_agent_unit_tests'
& $Python -B -m pytest apps/api/app/tests/test_reference_candidate_loop.py apps/api/app/tests/test_campaign_loop.py -q
```

数据库地址只用于导入离线测试配置，不连接、不迁移数据库。真实子进程专项仍使用模型替身：

```powershell
$env:HARNESS_OS_ACCEPTANCE = '1'
& $Python -B -m pytest apps/api/app/tests/test_reference_candidate_loop.py::test_reference_pair_uses_real_local_processes -q
```

本机准备见 [Windows 本地 Python 测试](harness-windows-local-python-plan.md)，设计边界见 [参考方案模式实施文档](harness-reference-candidate-plan.md)。
