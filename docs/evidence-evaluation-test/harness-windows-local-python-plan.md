# Windows 本机 Python 自动改进测试

本方案替代之前的 Docker 测试方案。直接使用当前已安装的 Python 和依赖，不创建虚拟环境、不构建镜像，也不启动 Docker。原来的预算、原版与候选对照、独立评分、逐轮反馈和不自动上线规则继续保留。

## 一、具体怎么运行

可信控制程序读取冻结题库和历史记录，把当前源码复制到实验目录。模型只能提出两类修改：在固定提示词后追加一段文字，或给固定判断增加白名单布尔条件。执行前会再次逐文件核对副本，只允许一个获准文件存在有限 AST 差异；额外导入、函数调用、其他源码改动和新增文件均被拒绝。

随后启动同一份本机 Python，用独立工作目录运行候选，通过 JSON 管道向父进程申请模型结果。凭证、题库标签和真实数据库配置留在父进程。候选进程不能调用真实业务 Tool 写文件。

Windows 使用两层检查：

- Python 审计检查：普通文件读取限于源码副本、Python 运行依赖及临时目录；写入仅限临时目录；阻止普通 Python 的网络、启动程序和目录链接操作。
- Windows Job：在发送任务前设置单进程数量限制、内存限制和关闭时终止进程；超时、异常和中断都会清理本次进程，不按名称结束其他服务。

**这不是任意恶意 Python 程序的系统文件沙箱。** Python 官方明确说明审计钩子不能单独用于实现安全沙箱，参见 [Python 审计接口](https://docs.python.org/3/library/sys.html#sys.addaudithook)。因此本地模式只接受前述受限候选，不能用来运行任意上传脚本；回执会明确记录 `os_sandbox: false`。进程数量与生命周期限制使用 [Windows Job](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

如果选择 Codex，直接调用本机原生 `codex.exe`，保持原安装目录及资源，不通过 cmd 或 PowerShell 包装。它作为可信模型适配器使用自己的 CLI 登录，关闭工具、插件、应用、子 Agent 和项目规则发现，并接受 Job 控制。它本身不是文件沙箱，Profile 会明确记录 `cli_os_filesystem_sandbox: false`，不能冒充原 macOS 的系统隔离身份。

## 二、检查本机 Python，不调用真实模型

在项目根目录执行：

```powershell
Set-Location E:\PycharmProject\file-agent
$Python = 'D:\anaconda\envs\myenv\python.exe'
.\scripts\run_harness_windows.ps1 -Action Check -Python $Python
```

这里的 `$Python` 指向现有环境的解释器，沿用其中已经安装的依赖。使用其他环境时改这一项即可。**不再执行 `-Action Build`。**

检查读取 `runtime/evidence-test/.env`，不回退使用项目根 `.env`，也不连接数据库或模型。它会创建全新的时间戳检查目录，执行真实编译、越界读写、联网和执行程序探针。正常输出应包含：

```json
{
  "stage": "LOCAL_TEST_VERIFIED",
  "checks": {
    "compile": true,
    "read_blocked": true,
    "write_blocked": true,
    "network_blocked": true,
    "exec_blocked": true
  },
  "model_calls": 0,
  "runtime": {
    "policy": "WINDOWS_LOCAL_AUDIT_JOB_V1",
    "os_sandbox": false,
    "filesystem_guard": "CPYTHON_AUDIT",
    "process_guard": "WINDOWS_JOB_OBJECT"
  }
}
```

实际回执还包含 Python 版本与脱敏散列。这些结果表示本地受限模式已通过检查，不表示具有完整系统文件隔离能力。

## 三、运行假模型验收

```powershell
.\scripts\run_harness_windows.ps1 -Action Acceptance -Python $Python
```

这一步不会请求真实模型。它使用真实 Python 子进程和 Windows Job，验证代码准入、超时清理、网络与文件审计、中文传输、模型代理协议以及固定规划回归。原版与候选都运行真实源码，模型接口使用确定性替身。

如需单独运行完整两轮源码改进闭环，可执行以下命令；仍不调用真实模型：

```powershell
$env:PYTHONUTF8 = '1'
$env:HARNESS_OS_ACCEPTANCE = '1'
$env:DATABASE_URL = 'postgresql://offline:offline@127.0.0.1/file_agent_unit_tests'
& $Python -B -m pytest apps/api/app/tests/test_harness_self_improvement.py::test_os_isolated_two_round_code_improvement -q
```

这里的数据库地址只用于导入测试模块，不会连接或迁移该数据库。`HARNESS_OS_ACCEPTANCE` 是沿用的专项开关名称；Windows 实际采用本地审计与 Job，不因此宣称系统文件沙箱。建议在单独的测试 PowerShell 窗口运行，避免覆盖其他终端的应用环境。

## 四、真实模型还需要什么

继续使用测试 `.env` 已选择的 Provider，不自动换模型或回落到另一种付费接口。

如果选择 `openai-compatible`，使用测试 `.env` 原有的模型配置。若选择 `codex-login`，本机 CLI 必须单独登录；工作台或其他应用的登录不等于 CLI 登录。

先查看 CLI 状态：

```powershell
codex login status
```

未登录时由用户在本机交互登录：

```powershell
codex login
```

然后执行不调用模型的登录检查：

```powershell
.\scripts\run_harness_windows.ps1 -Action Check -Python $Python -CheckModelLogin
```

或者只检查 CLI：

```powershell
& $Python -B scripts/run_evidence_test.py harness codex-probe
```

应得到 `CHECK_PASSED`、`model_calls: 0`。`CODEX_CHATGPT_LOGIN_REQUIRED` 表示尚未完成所需的 CLI 登录，不能自动借用其他账号或 API key。只有显式添加 `--live` 才会实际调用模型。

## 五、沿用已导入资料和历史

原件、题库、旧实验以及现有 `.env` 保留原样，不重新 OCR、不重新上传。只读重放示例：

```powershell
& $Python -B scripts/run_evidence_test.py harness planning-compare --experiment experiments/harness-v1/adaptive-baseline-v1
& $Python -B scripts/run_evidence_test.py harness adaptive-compare --experiment experiments/harness-v1/adaptive-policy-v2
```

Windows 本机模式的执行身份与原 macOS 不同，不能直接修改旧 JSON 假装已经验证。先用原冻结题库和固定候选建立本机验证记录。以下命令只是检查：

```powershell
$ValidationArgs = @(
    'harness', 'adaptive-validate',
    '--dataset', 'datasets/adaptive-validation-v2/cases-v1.json',
    '--output', 'experiments/harness-v1/windows-local-validation-v1',
    '--repetitions', '2',
    '--max-requests', '168',
    '--max-active-seconds', '7200',
    '--allow-model-profile-change'
)
& $Python -B scripts/run_evidence_test.py @ValidationArgs
```

确认 `CHECK_PASSED` 后，用户明确开始真实验证时执行：

```powershell
& $Python -B scripts/run_evidence_test.py @ValidationArgs --live
```

168 为原 14 题、两组、2 次重复、每试次最多3次规划调用的总上限；失败与回退也记账。它不是费用金额，未知 Token 用量仍记未知。C 阶段仍分别记录开发与保留结果，保留结果不交给后续建议器。

## 六、启动自动探索、复测、反馈循环

本机验证完成并通过独立核验后，执行小规模闭环的零调用检查：

```powershell
$LoopArgs = @(
    'harness', 'campaign-loop',
    '--source', 'experiments/harness-v1/windows-local-validation-v1',
    '--output', 'experiments/harness-v1/windows-local-auto-loop-v1',
    '--max-cycles', '2',
    '--max-rounds', '2',
    '--repetitions', '1',
    '--top-k', '1',
    '--retest-repetitions', '2',
    '--max-requests', '484',
    '--max-active-seconds', '14400',
    '--advisor-max-output-tokens', '8000',
    '--planner-max-output-tokens', '3000'
)
& $Python -B scripts/run_evidence_test.py @LoopArgs
```

明确开始真实调用：

```powershell
& $Python -B scripts/run_evidence_test.py @LoopArgs --live
```

所有循环、原版、候选、建议器、失败及回退共用484次上限和4小时执行预算，不按阶段重置，不自动续费或上线。独立历史重放不调用模型，但历史链较长时会较慢，其耗时仍按原流程另计。

查看进度及结束后的独立重算：

```powershell
& $Python -B scripts/run_evidence_test.py harness campaign-loop-compare --experiment experiments/harness-v1/windows-local-auto-loop-v1
```

已使用的输出目录不能重用，需要重跑时换新目录和明确的新预算，不覆盖旧记录。

## 七、实现位置与配置保护

| 文件 | 作用 |
|---|---|
| `improvement/local_runtime.py` | 逐文件及 AST 准入、原生 CLI 定位、Job 数量/内存/生命周期控制 |
| `improvement/local_worker.py` | 受控 stdin 引导、独立测试配置、运行依赖加载和 Python 审计 |
| `improvement/isolation.py` | 根据操作系统选择执行边界、冻结副本、有界 IPC 和探针 |
| `improvement/codex_backend.py` | 本机 CLI、禁用工具、登录检查、事件校验和预算记录 |
| `evidence_harness/paths.py` | Windows 长路径和统一相对路径，不更改注册表或全局 pathlib |
| `scripts/run_evidence_test.py` | 独立 `.env`、UTF-8、等待真实子进程并传回退出码 |
| `scripts/run_harness_windows.ps1` | 本机 Check、Acceptance 和可选登录检查 |

启动使用 `-X utf8 -I -S -B`，不继承旧数据库、API key、`PYTHONPATH`、用户 site 或可执行 `.pth`。只添加本机环境的正常依赖目录，认证和真实模型请求留给宿主。没有新建虚拟环境，没有数据库迁移，也不需要更新 WorkBuddy 插件。

## 八、2026-11-19 本机验证记录

- `Check` 已通过，五项检查均为 `true`，输出 `LOCAL_TEST_VERIFIED`、`model_calls: 0`。
- `Acceptance` 已通过：**18 项**，包括真实 Windows Job、超时退出、五项本地边界检查、固定回归、中文的 legacy/Adaptive 规划、候选提示确实进入子进程及长路径导入。
- 完整两轮源码改进专项已通过：第一轮 `NO_GAIN`，第二轮读取上一轮反馈后为 `IMPROVED`，10 次调用均为假模型，未自动采纳到正式源码。
- 基础启动、CLI 协议、代理、模型选择及本地准入回归 **80 项通过**。以上测试有交叉，不把次数相加作为独立测试总数。
- 已确认本机原生 CLI 可运行，但 `login status` 返回尚未登录；未运行真实模型探测或付费批次。真实 Codex 测试需先完成第四节登录。
- 用户提供的 `.env` 逐字节保持一致。未迁移数据库、未修改原始测试文件、未执行 Git 提交。
