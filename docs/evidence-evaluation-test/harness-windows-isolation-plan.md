# 旧 Docker 测试方案（已停用，保留设计记录）

用户已明确改用本机 Python。下方的 Docker、Build 和镜像操作不再执行。当前实现、可复制命令及边界说明以 [Windows 本机 Python 自动改进测试](harness-windows-local-python-plan.md) 为准。

## 目标

在 Windows 上运行现有的“读取反馈 → 模型提出候选 → 原版与候选对照 → 独立评分 → 反馈下一轮”流程。只调整测试环境和跨平台文件处理，不改变业务技能、正式文件、数据库结构、评分规则、调用额度或正式发布要求。候选继续只保留在实验副本里，不自动改动运行程序。

Windows 主程序仍使用现有 Python。候选和 Codex 放进一次性的 Linux 容器，用完就销毁；macOS 仍使用原来的系统沙箱。Windows 没有启动 Docker 或没有准备镜像时，明确停止，不能跳过隔离直接运行。

## 隔离怎么做

| 部分 | 可以访问 | 不可以访问 |
|---|---|---|
| Windows 可信控制程序 | 已授权的独立测试目录、评分器、模型代理和记账文件 | 不把凭证、Gold 或数据库配置交给候选 |
| 候选 Python 容器 | 只读的本轮源码副本、容器内临时目录、受控 JSON 管道 | 题库、评分器、项目目录、真实数据库、宿主网络、宿主 Docker 接口 |
| Codex 容器 | CLI 自己的只读登录文件、给定的规划请求、必要的模型网络连接 | 项目目录、题库、Gold、业务文件、Windows 用户目录 |

容器不映射服务端口，不传入项目 `.env`，根文件系统只读，删除额外系统能力，以普通用户运行，限制内存、CPU 和进程数量。候选另外通过内核 seccomp 禁止网络和执行程序。Codex 允许启动固定 CLI 本身及必要线程，禁止创建子进程；工具能力仍关闭，出现工具事件仍按原规则拒绝。任何终态、异常或中断后，都对本次随机容器名执行清理；清理失败不能报成功。

候选只有源码挂载；Codex 只有单个认证文件挂载，两者绝不共用挂载。认证文件内容由 CLI 读取，程序不解析、不打印、不写入镜像。登录过期时先在本机重新登录，不尝试修改只读认证文件。

这些措施使用 Docker 的 [只读挂载和运行限制](https://docs.docker.com/reference/cli/docker/container/run/) 及 [seccomp](https://docs.docker.com/engine/security/seccomp/)。Docker Desktop 本身属于可信运行环境，应使用维护中的版本和本机 Linux engine。

## 文件兼容

- 用带上限的线程队列读取子进程管道，替代 Windows 不支持的 `selectors` 管道轮询；保留原超时、输出大小、事件与 JSON 校验。
- Windows 启动器等待实际子进程结束并传回其退出码，避免 `os.execv` 更换进程后把失败误报为成功；失败检查会阻止后续步骤。
- 实验内部相对路径统一为 `/`；源码快照统一 LF，候选按 UTF-8 原样写入。磁盘上的原项目文件不随之修改。
- 使用 Harness 自己的长路径类，文件系统调用需要时才添加 Windows 扩展路径前缀；写入清单的路径仍为普通相对路径。不更改系统注册表，也不全局替换 `pathlib`。
- 复制与清单检查在进入目录前拒绝符号链接及 Windows reparse point，防止通过目录链接读到快照之外。
- 每个宿主进程固定一个本地镜像 ID，执行时禁止自动拉取。候选的实际镜像 ID 写入各次运行目录的 `container-runtime.json`；Codex 的 Profile 明确记录容器隔离及镜像身份。
- Windows 上 `.env`、数据库配置和生产 API 不需要为本改造迁移。独立配置仍在 `runtime/evidence-test/.env`。

Windows 宿主文件权限由目录 ACL 决定，不能用 POSIX 的 `700/600` 数字判断；本轮不更改系统 ACL。测试中的 POSIX 权限位断言只在相应系统执行。当前 Windows 账号没有创建符号链接的权限时，仅相应的符号链接构造测试明确跳过，仍执行路径越界、长路径和容器隔离检查。

Windows 的读写探针分别使用镜像内不可读哨兵及临时目录内只读哨兵；宿主哨兵不挂载，并由父进程确认内容未变化。宿主私有内容不可达还依赖严格的挂载白名单，不能为了通过探针把项目或题库挂入容器。

## 主要实现位置

| 文件 | 职责 |
|---|---|
| `apps/api/app/modules/evidence_harness/improvement/docker_runtime.py` | 本地 engine 检查、固定镜像身份、挂载白名单、CLI 登录检查与容器清理 |
| `apps/api/app/modules/evidence_harness/improvement/isolation.py` | 按系统选择隔离后端、冻结源码、校验清单并执行有界 IPC |
| `apps/api/app/modules/evidence_harness/improvement/process_io.py` | Windows 和 POSIX 通用的管道读取与进程退出 |
| `apps/api/app/modules/evidence_harness/improvement/codex_backend.py` | 同一 Codex 协议、事件校验和调用预算下切换执行环境 |
| `apps/api/app/modules/evidence_harness/paths.py` | 局部长路径处理、拒绝目录链接和安全复制 |
| `integrations/evidence-harness/` | 专用镜像、固定 CLI 及平台资源、依赖与可信 seccomp 引导器 |
| `scripts/run_evidence_test.py` / `scripts/run_harness_windows.ps1` | 独立配置启动、真实退出码，以及构建、检查和验收命令 |
| Harness 的各记录与比较模块 | 统一相对路径，已知历史仍逐项独立重放，保留原评分与预算门槛 |

## 一、首次准备

打开 Docker Desktop，使用 Linux containers，等待 engine 正常。确认现有 Python 可用。在项目根目录执行：

```powershell
Set-Location E:\PycharmProject\file-agent
$Python = 'D:\anaconda\envs\myenv\python.exe'
docker info --format '{{.OSType}}'
```

应输出 `linux`。若提示连接不到 engine，先启动 Docker Desktop；只运行 FileAgent 的 API 或 worker 不能替代它。

构建专用镜像，只需首次或运行环境依赖更新后执行：

```powershell
.\scripts\run_harness_windows.ps1 -Action Build -Python $Python
```

构建需要下载基础镜像、Python 包和固定版本的 Codex CLI（默认 `0.166.0`），不调用模型。镜像的构建上下文只有 `integrations/evidence-harness/`，不会把项目数据和 `.env` 发进镜像。专用镜像不依赖 FileAgent 生产镜像，不必重新构建生产基础镜像。

镜像是规划测试专用环境，不包含 OCR 引擎或模型权重。如果后续规划组件新增导入依赖，应更新其 `requirements.txt`，重新构建并重新验收。更新镜像会改变实验执行身份，不应在正在运行的任务中切换。

## 二、不花模型费用的检查

```powershell
.\scripts\run_harness_windows.ps1 -Action Check -Python $Python
```

依次检查独立配置、实际容器隔离、Codex 版本和 ChatGPT 登录。预期隔离结果包含 `compile` 以及四项 `*_blocked: true`；Codex 检查为 `CHECK_PASSED`，`model_calls: 0`。检查有独立时间戳输出目录，不覆盖以前的实验。

Codex 登录文件默认从 `%USERPROFILE%\.codex\auth.json` 只读挂载。这里只检查文件是否存在，不查看内容。需要先在本机登录 Codex；如果当前登录仅保存在 Windows 凭据管理器中、没有该文件，本适配器会明确停止，需要改用可供 CLI 读取的文件登录方式或使用原 API Provider，不能拿 API key 冒充 ChatGPT 登录。

如测试 `.env` 设置了本机代理 `127.0.0.1` 或 `localhost`，只对 Codex 容器将地址转换为 `host.docker.internal`；Windows 主程序仍使用原地址。代理端也必须允许来自 Docker 虚拟网络的连接。连接失败不要自动重复整批模型测试。

## 三、实际隔离验收，仍不调用真实模型

```powershell
.\scripts\run_harness_windows.ps1 -Action Acceptance -Python $Python
```

这一步运行真实 Docker 和内核限制，验证编译、禁止读取与写入私有探针、禁止联网和执行程序，并对照固定规划回归；随后使用假模型执行一次真实容器中的探索和复测闭环。假模型替代的是收费接口，隔离和评分仍执行真实程序，可能需要数分钟。普通单元测试默认跳过真实容器验收，不能把“跳过”当作验收通过。

如果未准备 Docker 或镜像，命令应失败并停止。常见状态：

| 状态 | 处理 |
|---|---|
| `HARNESS_DOCKER_NOT_INSTALLED` | 安装并启动 Docker Desktop |
| `HARNESS_DOCKER_CHECK_FAILED` | 检查本机 engine、权限和专用镜像是否已经构建 |
| `HARNESS_DOCKER_LINUX_REQUIRED` | 切换到 Linux containers |
| `HARNESS_DOCKER_LOCAL_ENGINE_REQUIRED` | 使用本机 Docker engine，不能把本地材料发送到远程 Docker |
| `HARNESS_DOCKER_IMAGE_INVALID` | 重新按本文构建专用镜像，不能拿生产镜像替代 |
| `HARNESS_CODEX_AUTH_FILE_REQUIRED` | 检查当前 Windows 用户的 CLI 文件登录状态 |
| `HARNESS_CODEX_CONTAINER_LOGIN_FAILED` | 检查登录文件的容器读取权限，过期时在宿主重新登录 |
| `OS_SANDBOX_OR_WORKER_FAILED` | 查看依赖与容器专项验收，不关闭隔离来绕过 |

## 四、迁移来的历史怎么使用

原 macOS 数据和实验保留原样。已知旧执行器身份只能用于只读重建：必须重新核验全部文件、原始模型结果、来源和评分，不能仅凭旧报告声称通过。

可先检查已导入的原基线：

```powershell
& $Python -B scripts/run_evidence_test.py harness planning-compare --experiment experiments/harness-v1/adaptive-baseline-v1
& $Python -B scripts/run_evidence_test.py harness adaptive-compare --experiment experiments/harness-v1/adaptive-policy-v2
```

Windows 容器的 CLI 和镜像身份与旧 macOS Profile 不同。自动循环要求来源和本轮执行身份一致，因此不能直接拿旧 `development-retest-v4` 当成本机已经复测的基线。

使用原冻结题库和固定候选，建立一份本机验证记录。下面先检查，不发起模型调用：

```powershell
$ValidationArgs = @(
    'harness', 'adaptive-validate',
    '--dataset', 'datasets/adaptive-validation-v2/cases-v1.json',
    '--output', 'experiments/harness-v1/windows-validation-v1',
    '--repetitions', '2',
    '--max-requests', '168',
    '--max-active-seconds', '7200',
    '--allow-model-profile-change'
)
& $Python -B scripts/run_evidence_test.py @ValidationArgs
```

只有看到 `CHECK_PASSED`，才在同一参数后显式追加 `--live` 执行真实验证：

```powershell
& $Python -B scripts/run_evidence_test.py @ValidationArgs --live
```

这里沿用现有 C 阶段，它会分别记录开发与保留评分；后续改进仍只允许消费开发反馈，保留结果不进入建议器。168 为 14 题、两组、2 次、每试次最多3次规划调用的上限，不承诺费用为零；失败及回退同样记账。不要改写旧 JSON 来伪造同一执行身份。

## 五、启动新的自动循环

本机验证完成且独立核验通过后，以它为来源启动小规模完整循环。仍先检查：

```powershell
$LoopArgs = @(
    'harness', 'campaign-loop',
    '--source', 'experiments/harness-v1/windows-validation-v1',
    '--output', 'experiments/harness-v1/windows-auto-loop-v1',
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

检查通过后明确启动：

```powershell
& $Python -B scripts/run_evidence_test.py @LoopArgs --live
```

完整循环仍是自动探索、固定复测、反馈下一轮，无需人工逐轮改目录。484 次为整个任务上限，两轮共用，不自动加额度；Codex 内部请求及部分 Token 用量继续如实记未知。没有合格候选时正常报告，不强行推荐或上线。

查看进度和结束后的独立核验：

```powershell
& $Python -B scripts/run_evidence_test.py harness campaign-loop-compare --experiment experiments/harness-v1/windows-auto-loop-v1
```

已使用的输出目录不能重用。需要重跑时使用新目录和新的明确预算，不删除旧记录，不自动续费重试。

## 验证记录

本轮开发不发起真实模型请求。离线回归、已有历史只读重放、实际 Docker 专项分别记录结果；实际 Docker 未运行时必须明确标为未验收，不能以 Fake 测试结果替代内核隔离验收。

2026-11-19 本机记录：

- Windows 管道、超时清理、长路径、UTF-8/LF、CLI 协议、启动器、代理及模型配置等回归，以及原反馈、建议、执行、历史与路由回归，合计 **223 项通过、4 项跳过**。跳过项为实际系统隔离专项和当前账号无法创建的符号链接，不代表对应权限已验收。
- 已导入的 `adaptive-baseline-v1` 只读重放得到 `EVALUATED`，`adaptive-policy-v2` 得到 `IMPROVED`；均未修改历史文件或请求模型。
- 额外的两轮 Fake 运行完成四个探索/复测阶段、50 次假调用，并生成独立结果 `NO_ELIGIBLE_CANDIDATE`；pytest 末尾重复重算耗时较长，主动结束，**未计入通过项数**。该记录属于当时冻结的执行器，不能替代新环境的真实模型验收。
- 本机 Docker engine 尚未运行，当前工具环境也未能启动 Desktop。因此专用镜像构建、实际 Linux 内核隔离和真实容器闭环 **尚未验收**。`Check` 已验证会以 `HARNESS_DOCKER_CHECK_FAILED` 停止并返回失败，不会继续执行后续步骤。
- `runtime/evidence-test/.env` 与用户提供的原配置逐字节一致，未执行数据库迁移、真实模型请求或 Git 提交。
