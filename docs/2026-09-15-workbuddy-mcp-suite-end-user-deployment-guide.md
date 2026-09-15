# WorkBuddy 使用 File Agent MCP 与附件桥接套件部署手册

## 1. 文档目标

本文用于把已经部署在局域网服务器上的 File Agent，配置到普通用户的 Windows 电脑，使用户能够在
WorkBuddy 中使用以下能力：

- 搜索已经入库的文件，并显示“文件名、依据、预览/下载”。
- 读取、总结和解释已检索文件。
- 从用户明确授权的本地目录批量导入文件。
- 上传 WorkBuddy 当前消息中的 Word、PDF、Excel、TXT 和图片附件并归档分类。
- 处理重复文件对比、分类调整、移动和受控重命名。

当前推荐组合为：

```text
File Agent 服务器 API/Caddy
+ 每台用户电脑上的 file_agent_mcp stdio 进程
+ WorkBuddy file-agent-workbuddy-bridge 0.1.10 套件
```

本手册不采用 `file-agent-connector` 标准连接器。标准连接器是没有 Hook 能力时的替代方案，会主动隐藏
`workbuddy_attachment_ingest` 和 `workbuddy_submission_ingest`，不能完成本轮聊天附件桥接。同时启用标准
连接器和下述手工 MCP 容易产生同名工具或重复 MCP，因此一台电脑只能选择其中一种方式。需要完整附件能力时，
应使用本文的“手工 MCP + 附件桥接套件”。

## 2. 组件位置与版本

服务器项目目录示例：

```text
E:\file-agent
```

开发机当前来源：

```text
MCP 源码：E:\PycharmProject\file-agent\apps\mcp
套件包：E:\PycharmProject\file-agent\integrations\workbuddy\file-agent-marketplace-0.1.10.zip
```

当前套件版本为 `0.1.10`。其 SHA-256 为：

```text
E4CDEA4B9C2236AB5BF5B212655E1C390F520BFD7D89E848D8CA515A618CDEDF
```

`file_search` 三列表格约束属于 MCP 代码，不属于套件。因此更新该功能时只需更新用户电脑上的 MCP 包并
重启 WorkBuddy，不需要把套件版本从 0.1.10 提升到新的版本。

## 3. 网络与账号规划

每台用户电脑只运行轻量 MCP 客户端；数据库、API、文件解析、分类和工作副本仍在服务器运行。

建议统一规划：

| 项目 | 示例 | 说明 |
|---|---|---|
| File Agent 服务器 IP | `10.102.4.241` | 应使用固定局域网 IP 或内网 DNS |
| 用户访问地址 | `http://10.102.4.241` | 通过 Caddy 的 80 端口访问，不直接暴露容器 8000 端口 |
| 套件市场地址 | `http://10.102.4.241/downloads/file-agent-marketplace-0.1.10.zip` | 仅用于安装/升级套件 |
| 客户端目录 | `D:\file-agent-client` | MCP Python 环境或源码位置 |
| 客户端数据目录 | `D:\file-agent-client-data` | 传输状态、附件快照和下载缓存 |
| WorkBuddy 数据目录 | `C:\Users\<当前用户>\.workbuddy` | 必须按每台电脑的真实用户名计算 |

不要给所有人共用同一个 File Agent 账号或 Token。每位用户应使用自己的账号，确保搜索权限和审计记录属于
正确用户。

## 4. 服务器管理员准备

### 4.1 检查服务配置

在服务器 PowerShell 中执行：

```powershell
Set-Location "E:\file-agent"
$EnvFile = "E:\file-agent\deploy\.env"
$ComposeFile = "E:\file-agent\deploy\docker-compose.production.yml"
docker compose --env-file $EnvFile -f $ComposeFile config --quiet
docker compose --env-file $EnvFile -f $ComposeFile up -d
docker compose --env-file $EnvFile -f $ComposeFile ps
```

至少应看到 `postgres`、`neo4j`、`api`、`gateway`、`scheduler`、`watcher` 和各 worker 为运行状态；
`migrate` 正常完成后显示退出属于预期。

局域网部署时，`deploy/.env` 至少应满足：

```dotenv
CADDY_SITE_ADDRESS=:80
ACCESS_TOKEN_EXPIRE_MINUTES=5256000
```

`5256000` 约为 10 年，并不是真正永不过期。修改该值后，只有重新登录签发的新 Token 使用新有效期；旧 Token
不会自动延长。修改 `JWT_SECRET_KEY` 会使所有旧 Token 立即失效。

### 4.2 检查服务器健康状态

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1/api/health"
```

再从同一局域网内另一台电脑执行：

```powershell
Test-NetConnection 10.102.4.241 -Port 80
Invoke-RestMethod -Uri "http://10.102.4.241/api/health"
```

若 80 端口不通，在服务器管理员 PowerShell 中创建入站规则：

```powershell
New-NetFirewallRule -DisplayName "File Agent HTTP 80" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80
```

不要为了客户端 MCP 额外暴露 8000 端口。MCP 使用 Caddy 的 `http://服务器地址`，客户端内部会自动追加
`/api/...`。

### 4.3 为用户创建账号

可以让用户访问 `http://10.102.4.241` 后通过“申请注册”创建账号，也可以由管理员预创建：

```powershell
Set-Location "E:\file-agent"
.\deploy\create-user.ps1 -Username "user01" -DisplayName "用户一"
```

脚本会安全提示输入密码。不要把密码直接写在共享文档或 PowerShell 历史中。

### 4.4 准备 MCP 客户端离线包

联网环境推荐由管理员生成包含 MCP 本体及全部 Python 依赖的 wheelhouse。以下命令只打包客户端，不包含
服务器代码、数据库、上传文件或密钥：

```powershell
$Source = "E:\PycharmProject\file-agent\apps\mcp"
$Output = "E:\PycharmProject\file-agent\release\file-agent-mcp-client-wheelhouse"
New-Item -ItemType Directory -Force -Path $Output | Out-Null
py -3.11 -m pip wheel --wheel-dir $Output $Source
Get-ChildItem -LiteralPath $Output -File | Get-FileHash -Algorithm SHA256
```

把整个 `file-agent-mcp-client-wheelhouse` 目录提供给用户。每次 MCP 代码更新后，应重新生成并用日期或发布号
标识客户端包；如果项目包版本号未变化，客户端升级时必须使用后文的 `--force-reinstall`。

如果用户电脑可以访问 Python 软件源，也可以只把 `apps/mcp` 目录复制给用户，再执行：

```powershell
& "D:\file-agent-client\venv\Scripts\python.exe" -m pip install "D:\file-agent-client\mcp"
```

生产分发优先使用 wheelhouse，避免每台电脑分别下载不同版本的依赖。

### 4.5 发布附件桥接套件

套件只在安装和升级时下载，不是 MCP 日常运行所必需的 HTTP 服务。管理员也可以通过受控文件共享把 ZIP
发给用户；如果 WorkBuddy 图形界面只接受市场 URL，可通过现有 Caddy 发布。

当前快速发布方式如下。注意：文件复制进容器后，重建 `gateway` 容器会丢失，需要重新执行：

```powershell
Set-Location "E:\file-agent"
$EnvFile = "E:\file-agent\deploy\.env"
$ComposeFile = "E:\file-agent\deploy\docker-compose.production.yml"
$SuiteZip = "E:\file-agent\integrations\workbuddy\file-agent-marketplace-0.1.10.zip"
$GatewayId = docker compose --env-file $EnvFile -f $ComposeFile ps -q gateway
docker compose --env-file $EnvFile -f $ComposeFile exec -T gateway mkdir -p /srv/downloads
docker cp $SuiteZip "${GatewayId}:/srv/downloads/file-agent-marketplace-0.1.10.zip"
```

正式长期发布应在生产 Compose 为 `gateway` 增加只读绑定卷，例如：

```yaml
services:
  gateway:
    volumes:
      - ../data/downloads:/srv/downloads:ro
```

然后把套件放入 `E:\file-agent\data\downloads`。如果使用单独 Compose override，后续每次重建服务都必须带上
同一个 override 文件；否则挂载会消失。

验证市场 URL：

```powershell
curl.exe -I "http://10.102.4.241/downloads/file-agent-marketplace-0.1.10.zip"
```

正确响应应满足：

- `HTTP/1.1 200 OK`。
- `Content-Type` 是 ZIP/二进制类型，而不是 `text/html`。
- 当前 0.1.10 文件大小为 `16256` 字节。

如果返回 `Content-Length: 410` 或 `Content-Type: text/html`，实际返回的是前端登录页，不代表套件已发布。

## 5. 每台用户电脑安装 MCP

### 5.1 前置条件

- Windows 10/11。
- WorkBuddy 已安装；当前附件桥接按 WorkBuddy 5.5.3 的宿主记录格式验证。
- 64 位 Python 3.11。
- 能访问 `http://10.102.4.241/api/health`。
- 拥有自己的 File Agent 用户名和密码。

### 5.2 创建隔离 Python 环境

推荐使用标准 venv，不要求用户安装 Conda：

```powershell
New-Item -ItemType Directory -Force -Path "D:\file-agent-client" | Out-Null
py -3.11 -m venv "D:\file-agent-client\venv"
$Python = "D:\file-agent-client\venv\Scripts\python.exe"
& $Python -m pip --version
```

使用管理员提供的离线 wheelhouse 安装：

```powershell
$Python = "D:\file-agent-client\venv\Scripts\python.exe"
$Wheelhouse = "D:\file-agent-client\wheelhouse"
& $Python -m pip install --no-index --find-links $Wheelhouse file-agent-mcp
& $Python -c "import file_agent_mcp; print(file_agent_mcp.__file__)"
```

如果该电脑已经安装过同版本包，升级时执行：

```powershell
$Python = "D:\file-agent-client\venv\Scripts\python.exe"
$Wheelhouse = "D:\file-agent-client\wheelhouse"
& $Python -m pip install --force-reinstall --no-index --find-links $Wheelhouse file-agent-mcp
```

也可以沿用现有 Conda 环境，例如 `D:\anaconda_envs\fileagent\python.exe`，但每台电脑的配置必须填写实际存在
的 Python 路径，不能照抄管理员电脑的路径。

### 5.3 创建客户端数据目录

```powershell
$WorkBuddyHome = Join-Path $env:USERPROFILE ".workbuddy"
$ClientData = "D:\file-agent-client-data"
$BridgeState = Join-Path $ClientData "bridge-state"
New-Item -ItemType Directory -Force -Path $WorkBuddyHome | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $WorkBuddyHome "blobs") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ClientData "transfer-state") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ClientData "extraction-pages") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BridgeState "document-cache") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BridgeState "downloads") | Out-Null
```

这些目录属于客户端缓存和可恢复状态，不是服务器工作副本。不要把它们设置为受管原始目录，也不要在上传、
OCR 或批次重试过程中清理。

### 5.4 登录并取得用户 Token

下面的命令不会把密码写进脚本文件：

```powershell
$ApiBase = "http://10.102.4.241"
$Username = Read-Host "File Agent 用户名"
$SecurePassword = Read-Host "File Agent 密码" -AsSecureString
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
$PlainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
$LoginBody = @{ username = $Username; password = $PlainPassword } | ConvertTo-Json
$Login = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/auth/login" -ContentType "application/json; charset=utf-8" -Body $LoginBody
$Token = $Login.access_token
} finally {
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
$PlainPassword = $null
}
$Login.user | Format-List username,display_name,role
```

验证 Token：

```powershell
Invoke-RestMethod -Uri "$ApiBase/api/auth/me" -Headers @{ Authorization = "Bearer $Token" }
```

不要把 `$Token` 输出到聊天、日志或截图中。后续把它写入本机 MCP 配置后，可以关闭当前 PowerShell 窗口。

### 5.5 配置授权目录

如果允许 WorkBuddy 批量导入用户电脑上的 `D:\FileAgentInput`，先创建或确认目录：

```powershell
New-Item -ItemType Directory -Force -Path "D:\FileAgentInput" | Out-Null
```

MCP 中使用逻辑根名映射真实目录：

```json
{"local-materials":"D:\\FileAgentInput"}
```

`FILE_AGENT_LOCAL_ROOTS` 必须是 JSON 对象，不是数组。Tool 只接收逻辑根名和相对路径，不能在对话中临时授权
任意绝对路径。如果该用户不需要本地目录批量导入，可配置为 `{}`；聊天附件桥接不依赖该目录。

### 5.6 写入 WorkBuddy MCP 配置

如果 `C:\Users\<用户>\.workbuddy\mcp.json` 已存在，必须先备份并合并 `file-agent` 节点，不能覆盖其他 MCP。
下面命令适用于新文件或结构正常、没有重复 JSON 键的既有文件：

```powershell
$WorkBuddyHome = Join-Path $env:USERPROFILE ".workbuddy"
$McpPath = Join-Path $WorkBuddyHome "mcp.json"
$Python = "D:\file-agent-client\venv\Scripts\python.exe"
$ApiBase = "http://10.102.4.241"
$WebBase = "http://10.102.4.241"
$ClientData = "D:\file-agent-client-data"
$BridgeState = Join-Path $ClientData "bridge-state"
$LocalRootsJson = @{ "local-materials" = "D:\FileAgentInput" } | ConvertTo-Json -Compress
$AttachmentRootsJson = @(
(Join-Path $WorkBuddyHome "blobs"),
(Join-Path $BridgeState "document-cache")
) | ConvertTo-Json -Compress
if (Test-Path -LiteralPath $McpPath) {
Copy-Item -LiteralPath $McpPath -Destination "$McpPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
$McpConfig = Get-Content -LiteralPath $McpPath -Raw -Encoding utf8 | ConvertFrom-Json
} else {
$McpConfig = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
}
if (-not $McpConfig.PSObject.Properties["mcpServers"]) {
$McpConfig | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
$ServerConfig = [pscustomobject]@{
type = "stdio"
alwaysLoad = $true
command = $Python
args = @("-m", "file_agent_mcp")
env = [pscustomobject]@{
FILE_AGENT_API_BASE_URL = $ApiBase
FILE_AGENT_WEB_BASE_URL = $WebBase
FILE_AGENT_ACCESS_TOKEN = $Token
FILE_AGENT_LOCAL_ROOTS = $LocalRootsJson
LOCAL_TRANSFER_STATE_DIR = (Join-Path $ClientData "transfer-state")
LOCAL_UPLOAD_CONCURRENCY = "2"
LOCAL_EXTRACTION_PAGE_DIR = (Join-Path $ClientData "extraction-pages")
LOCAL_FILE_DOWNLOAD_DIR = (Join-Path $BridgeState "downloads")
FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS = $AttachmentRootsJson
FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR = $BridgeState
FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS = "600"
NO_PROXY = "10.102.4.241,127.0.0.1,localhost"
}
disabled = $false
}
$McpConfig.mcpServers | Add-Member -NotePropertyName "file-agent" -NotePropertyValue $ServerConfig -Force
$McpConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $McpPath -Encoding utf8
```

注意：

- `FILE_AGENT_API_BASE_URL` 应填写用户浏览器可以访问的局域网地址，不能填服务器自身的 `127.0.0.1`。
- `FILE_AGENT_WEB_BASE_URL` 应填写 File Agent 前端入口；如果 API 直连 `:8000` 而前端经 Caddy 使用 80
  端口，这两个值必须分别填写。
- 地址不要写成 `http://10.102.4.241/api`；MCP 会自行追加 `/api`。
- JSON 中不能同时出现 `NO_PROXY` 和 `no_proxy` 两个仅大小写不同的键，否则部分解析工具会认为是重复键。
- 如果使用的是复制源码而不是 wheel 安装，需要额外增加
  `"PYTHONPATH": "D:\\file-agent-client\\mcp"`；wheel 安装方式不需要 `PYTHONPATH`。
- 配置文件含用户 Token，只允许当前 Windows 用户读取，不得共享整份 `mcp.json`。

检查配置是否能被解析：

```powershell
$McpPath = Join-Path $env:USERPROFILE ".workbuddy\mcp.json"
$Config = Get-Content -LiteralPath $McpPath -Raw -Encoding utf8 | ConvertFrom-Json
$Config.mcpServers."file-agent" | Select-Object type,command,alwaysLoad,disabled
$Config.mcpServers."file-agent".env.PSObject.Properties.Name
```

不要输出整个 `env`，否则会把 Token 打印到终端或截图中。

## 6. 每台用户电脑安装附件桥接套件

### 6.1 添加套件市场并安装

在 WorkBuddy 中进入：

```text
技能 -> 套件 -> 添加市场
```

填写管理员发布的地址：

```text
http://10.102.4.241/downloads/file-agent-marketplace-0.1.10.zip
```

市场加载后安装并启用：

```text
file-agent-workbuddy-bridge
```

不要同时安装或启用 `file-agent-connector` 标准连接器。

### 6.2 配置套件选项

套件需要三个本机选项：

| 选项 | 示例 |
|---|---|
| `python_executable` | `D:/file-agent-client/venv/Scripts/python.exe` |
| `workbuddy_home` | `C:/Users/<当前用户>/.workbuddy` |
| `bridge_state_dir` | `D:/file-agent-client-data/bridge-state` |

如果 WorkBuddy 图形界面没有显示选项，可在完全退出 WorkBuddy 后，备份并修改
`C:\Users\<用户>\.workbuddy\settings.json`。以下命令只合并本套件配置：

```powershell
$SettingsPath = Join-Path $env:USERPROFILE ".workbuddy\settings.json"
$PluginKey = "file-agent-workbuddy-bridge@file-agent-local-marketplace"
$Python = "D:/file-agent-client/venv/Scripts/python.exe"
$WorkBuddyHome = (Join-Path $env:USERPROFILE ".workbuddy").Replace("\", "/")
$BridgeState = "D:/file-agent-client-data/bridge-state"
Copy-Item -LiteralPath $SettingsPath -Destination "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
$Settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $Settings.PSObject.Properties["enabledPlugins"]) {
$Settings | Add-Member -NotePropertyName enabledPlugins -NotePropertyValue ([pscustomobject]@{})
}
if (-not $Settings.PSObject.Properties["pluginConfigs"]) {
$Settings | Add-Member -NotePropertyName pluginConfigs -NotePropertyValue ([pscustomobject]@{})
}
$Settings.enabledPlugins | Add-Member -NotePropertyName $PluginKey -NotePropertyValue $true -Force
$PluginConfig = [pscustomobject]@{
options = [pscustomobject]@{
python_executable = $Python
workbuddy_home = $WorkBuddyHome
bridge_state_dir = $BridgeState
}
}
$Settings.pluginConfigs | Add-Member -NotePropertyName $PluginKey -NotePropertyValue $PluginConfig -Force
$Settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $SettingsPath -Encoding utf8
```

验证套件配置：

```powershell
$SettingsPath = Join-Path $env:USERPROFILE ".workbuddy\settings.json"
$PluginKey = "file-agent-workbuddy-bridge@file-agent-local-marketplace"
$Settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8 | ConvertFrom-Json
$Settings.enabledPlugins.$PluginKey
$Settings.pluginConfigs.$PluginKey.options | Format-List
```

`bridge_state_dir` 必须与 MCP 环境变量 `FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR` 完全相同；其
`document-cache` 子目录也必须出现在 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS` 中。

### 6.3 完全重启 WorkBuddy

只关闭对话窗口可能不会重启 MCP。保存配置后：

1. 完全退出 WorkBuddy，包括系统托盘中的后台进程。
2. 在任务管理器确认旧 WorkBuddy/MCP 子进程已经退出。
3. 重新启动 WorkBuddy。
4. 确认“文件助手”或底层 `file-agent` MCP 已连接并能列出工具。

旧消息不会因为安装套件而自动生成可信附件提交单。附件桥接测试必须重新上传文件并发送一条新消息。

## 7. 功能验收步骤

### 7.1 搜索及预览/下载

在 WorkBuddy 输入：

```text
请用文件助手查找人才推荐相关文件，并说明文件名、文件类型和依据。
```

即使用户文字要求“文件类型”，最新版 MCP 的 `file_search` 用户表格也应保持：

```markdown
| 文件名 | 依据 | 操作 |
|---|---|---|
| 示例.docx | 正文命中依据 | 预览 / 下载 |
```

验收要点：

- 不展示路径、相关度、分类、文件类型或内部 ID。
- “预览”和“下载”为可点击链接。
- 链接主机应为 `10.102.4.241` 或内网域名，不能是 `127.0.0.1`。
- 在没有登录 File Agent Web 的浏览器中也能打开链接。
- 只有受管源但工作副本尚未就绪时，操作列显示“工作副本生成中”。

### 7.2 读取和证据回答

先搜索，再在同一对话输入：

```text
请用文件助手读取刚才结果中的第一个文件并总结，所有结论都给出页码或 Sheet 依据。
```

WorkBuddy 必须使用搜索结果中的稳定 ID 调用 `file_read` 或 `evidence_answer`，不能根据文件名猜测对象。

### 7.3 已授权本地目录批量导入

把测试文件放入 `D:\FileAgentInput\test-001`，然后输入：

```text
请用文件助手递归导入已授权根 local-materials 下的 test-001 目录，自动归档并分类。
```

不得在聊天中传入 `D:\FileAgentInput` 绝对路径作为 Tool 参数。最终回执应逐文件显示状态和主分类。

### 7.4 WorkBuddy 本轮附件导入

在 WorkBuddy 当前消息中选择或拖入一个新的 DOCX、PDF、XLS/XLSX、TXT 或图片，同时输入：

```text
请用文件助手归档并分类本轮附件。
```

正确流程应出现：

```text
套件 UserPromptSubmit Hook
-> FILE_AGENT_HOST_SUBMISSION ref=...
-> workbuddy_submission_ingest
-> batch_get/job_get
-> 查重、解析、分类和逐文件回执
```

仅上传附件但没有明确归档/导入/分类任务时，不应自动入库。

图片进入 `WAITING_EXTERNAL_EXTRACTION` 时，需要 WorkBuddy 中已经启用可返回逐页文本的腾讯文档 OCR 工具；
套件会指导 `extraction_claim -> ocr.extract -> extraction_submit`。只重复查询 `batch_get` 不会完成 OCR。

### 7.5 重复文件

上传一个可能重复的文件。出现 `WAITING_DUPLICATE_CONFIRMATION` 时，WorkBuddy 应先调用
`duplicate_review_get` 和每个候选的 `duplicate_comparison_get`，展示可点击对比页，然后才询问：

- 继续上传。
- 使用已有文件。
- 取消上传。

用户没有明确选择前不得调用 `duplicate_decide`。

### 7.6 明确分类或移动

用户明确指定稳定对象和受控目标后，可以测试：

```text
请把刚才找到的第一个文件主分类改为“学院—人才工作”。
```

或：

```text
请把刚才找到的第一个工作副本移动到指定受控分类目录。
```

明确的 `SET_PRIMARY` 或 `MOVE` 请求本身构成本次授权，不再二次确认；对象或目标有歧义时必须先让用户选择。
独立重命名、删除、覆盖等其他高风险动作仍按对应 OperationPlan 规则执行。

## 8. 诊断与排错

### 8.1 MCP 无法连接

先在用户电脑执行：

```powershell
Test-NetConnection 10.102.4.241 -Port 80
Invoke-RestMethod -Uri "http://10.102.4.241/api/health"
& "D:\file-agent-client\venv\Scripts\python.exe" -c "import file_agent_mcp; print(file_agent_mcp.__file__)"
```

如果网络正常但 WorkBuddy 仍无法加载，检查：

- `mcp.json` 的 `command` 是否为真实 Python 文件。
- 是否安装了 `file-agent-mcp`。
- JSON 是否存在尾逗号、注释或重复键。
- 企业代理是否绕过服务器 IP；`NO_PROXY` 应包含服务器 IP。
- 修改配置后是否完全重启 WorkBuddy。

### 8.2 Token 失效或登录失败

`Invalid username or password` 表示账号或密码错误，不是 MCP 故障。管理员应确认账号存在，用户重新登录获取新
Token，再只替换 `FILE_AGENT_ACCESS_TOKEN`。服务重启和普通代码升级不会自动使 Token 失效；Token 到期或
`JWT_SECRET_KEY` 改变时才需要重新签发。

### 8.3 套件已安装但附件没有调用工具

检查套件状态：

```powershell
$SettingsPath = Join-Path $env:USERPROFILE ".workbuddy\settings.json"
$PluginKey = "file-agent-workbuddy-bridge@file-agent-local-marketplace"
$Settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8 | ConvertFrom-Json
$Settings.enabledPlugins.$PluginKey
$Settings.pluginConfigs.$PluginKey.options | Format-List
```

检查有限诊断日志：

```powershell
Get-Content -LiteralPath "D:\file-agent-client-data\bridge-state\capture-diagnostics.jsonl" -Tail 30 -Encoding utf8
```

`PENDING_MANIFEST_CREATED` 表示 Hook 已建立待解析引用，不代表服务器已完成归档。没有该状态时，重点检查套件
是否启用、三个选项是否正确，以及是否发送了包含明确文件任务的新消息。

### 8.4 查找 WorkBuddy 日志

```powershell
$LogRoot = Join-Path $env:USERPROFILE ".workbuddy\logs"
Get-ChildItem -LiteralPath $LogRoot -File -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 20 FullName,LastWriteTime,Length
```

搜索错误时不要搜索或输出完整 Token：

```powershell
Get-ChildItem -LiteralPath $LogRoot -File -Recurse | Select-String -Pattern "file-agent|MCP|workbuddy_submission_ingest|Traceback|ERROR" -Encoding utf8
```

### 8.5 搜索仍没有三列表格或链接

依次确认：

1. 用户电脑安装的是包含最新 `file_search` 展示契约的 MCP wheel，而不是旧源码。
2. WorkBuddy 已完全重启，旧 MCP 子进程没有继续驻留。
3. 后端已经部署公开预览/下载接口的最新代码。
4. 目标文件已经存在 `ACTIVE` 工作副本；否则只能显示“工作副本生成中”。
5. `FILE_AGENT_API_BASE_URL` 是局域网地址，避免生成 `127.0.0.1` 链接。

MCP 已通过 Tool 描述、用户 `TextContent` 标注和结构化 `VERBATIM_USER_DISPLAY` 契约要求宿主原样展示三列
表格。该约束能显著降低二次改写，但 MCP 协议本身不能强制第三方宿主模型逐字复制；诊断时应先查看原始 Tool
结果。如果原始 Tool 结果已有三列表格而最终聊天回答被改写，应归为 WorkBuddy 宿主展示问题。

### 8.6 套件市场 URL 返回登录页

执行：

```powershell
curl.exe -I "http://10.102.4.241/downloads/file-agent-marketplace-0.1.10.zip"
```

若返回 `text/html` 或约 410 字节，说明 Caddy 没找到 ZIP 后回退到了 `index.html`。重新把 ZIP 放入
`/srv/downloads`，或恢复生产环境的只读下载目录挂载。启动 `python -m http.server` 只是一种临时市场文件
分发手段，与 MCP 日常连接、附件桥接和 File Agent API 运行无关。

## 9. 升级顺序

### 9.1 只更新服务器代码

1. 按服务器发布手册备份并更新 API/worker/Web。
2. 确认 migrate 正常完成和 API 健康。
3. 如果 API 契约未改变，用户 MCP 和套件不需要更新。

### 9.2 更新 MCP Tool

1. 管理员从最新 `apps/mcp` 重新构建 wheelhouse。
2. 用户电脑执行 `pip install --force-reinstall`。
3. 完全重启 WorkBuddy。
4. 重新执行搜索、读取、目录导入和附件导入验收。

### 9.3 更新附件桥接套件

1. 管理员发布新版本 marketplace ZIP，不能覆盖旧版本文件后仍沿用旧校验值。
2. 用户在“技能 -> 套件”中更新或重新安装。
3. 确认 `settings.json` 中 PluginKey 和三个选项仍存在。
4. 完全重启 WorkBuddy，并使用新上传附件测试；旧消息不能复用。

`file_search` 三列表格这类 MCP 变化不要求套件同步升版；Hook、附件捕获、Skill 编排或重复对比流程变化才需要
发布新套件。

## 10. 交付验收清单

每台用户电脑完成后应逐项签字确认：

- [ ] 用户电脑可访问 `http://服务器/api/health`。
- [ ] 使用用户自己的账号取得 Token，`/api/auth/me` 验证成功。
- [ ] 独立 Python 环境可以导入 `file_agent_mcp`。
- [ ] `mcp.json` 只有一个启用的 `file-agent` MCP，且没有重复键。
- [ ] 未同时启用 `file-agent-connector` 标准连接器。
- [ ] 0.1.10 附件桥接套件已安装并启用。
- [ ] 套件 `bridge_state_dir` 与 MCP 环境变量完全一致。
- [ ] 搜索返回“文件名、依据、操作”三列表格。
- [ ] 预览和下载链接在未登录浏览器中可打开。
- [ ] 已授权目录可通过逻辑根名批量导入。
- [ ] 新上传的 WorkBuddy 附件产生可信提交单并进入 File Agent 批次。
- [ ] 重复文件先展示对比链接，再接收用户决定。
- [ ] 原始文件未被重命名、移动、覆盖或删除。
- [ ] 用户 Token、密码、绝对本机路径和文件正文未出现在共享日志或截图中。

## 11. 卸载或停用

需要临时停用时，优先在 WorkBuddy 中禁用 `file-agent` MCP 和
`file-agent-workbuddy-bridge` 套件。若要彻底移除：

1. 先备份 `mcp.json` 和 `settings.json`。
2. 从 `mcp.json.mcpServers` 中仅删除 `file-agent` 节点。
3. 从 `enabledPlugins` 和 `pluginConfigs` 中仅删除
   `file-agent-workbuddy-bridge@file-agent-local-marketplace`。
4. 完全重启 WorkBuddy。
5. 确认不再需要本地可恢复任务后，再由用户决定是否删除客户端缓存。

停用或卸载客户端不会删除服务器中的已归档文件、工作副本、解析结果或审计记录。
# 分类浏览能力补充（2026-09-15）

最新 MCP 代码提供两个不依赖 WorkBuddy 套件的只读工具：

- `classification_overview`：查看分类统计、一级分类入口和完整分类页面链接。
- `classification_files`：按 `category_id` 分页查看文件，并显示预览/下载链接。

用户端 MCP 环境变量需要配置 Web 入口；API 使用 8000 端口而 Caddy 使用 80 端口时尤其不能省略：

```json
"FILE_AGENT_WEB_BASE_URL": "http://10.102.4.241"
```

服务器 Web 页面支持以下深链接：

```text
http://<服务器地址>/files?category_id=<稳定分类ID>&page=1
```

如果浏览器尚未登录，会先显示 File Agent 登录页，登录成功后返回原分类节点和页码。该能力需要同时部署最新 API、Web 和 MCP 代码，但不要求升级 WorkBuddy 套件压缩包。
