# WorkBuddy 文件助手：新用户首次安装操作手册

适用日期：2026-10-20。本文按当前 WorkBuddy 附件桥接套件 **0.1.85** 和 MCP **0.1.1** 编写。示例服务器是 `10.102.4.241`，部署目录是 `E:\file-agent`；实际地址不同须统一替换。本文只安装客户端接入，不部署 API/Web 业务代码。

## 一、先弄清要安装的三部分

| 部分 | 放在哪里 | 作用 |
| --- | --- | --- |
| File Agent API | 已部署的服务器 | 保存文件并执行业务操作 |
| `file-agent-mcp` | **每台用户电脑**的 Python 环境 | 向 WorkBuddy 提供文件助手工具 |
| `file-agent-workbuddy-bridge` 套件 | **每台用户电脑**的 WorkBuddy | 捕获本轮附件、提供技能与提交引用 |

把 MCP 文件复制到服务器，不能代替在用户电脑安装 MCP；安装 MCP，也不能代替在 WorkBuddy 中安装套件。新用户按本手册的顺序完成两项客户端安装。

当前市场固定地址：

```text
http://10.102.4.241/downloads/file-agent-marketplace.zip
```

不要使用历史地址 `.../file-agent-marketplace-0.1.10.zip`，也不要使用临时的 `:8765` 文件服务。当前 0.1.85 ZIP 应为 **83197 字节**，SHA-256 为：

```text
1df8411c3fbe9cc07200c60e38cbda1db8a7b50456c97a5d6b3652996d036265
```

## 二、管理员准备交付物

开始前确认：服务器 API 已通过 `http://10.102.4.241/api/health` 检查；用户电脑已安装 WorkBuddy、64 位 Python 3.11，且能访问服务器 80 端口。新用户需要自己的 File Agent 账号；已有账号可直接使用，尚无账号须按第五节先注册。不要共用管理员的账号或访问令牌。

管理员向用户电脑交付一个 **MCP wheelhouse 目录**。在与当前服务器代码对应的完整源码构建机上执行：

```powershell
$Source = 'E:\PycharmProject\file-agent\apps\mcp'
$Output = 'E:\PycharmProject\file-agent\release\file-agent-mcp-wheelhouse-20261020'
New-Item -ItemType Directory -Force -Path $Output | Out-Null
py -3.11 -m pip wheel --wheel-dir $Output $Source
if ($LASTEXITCODE -ne 0) { throw 'MCP 离线安装包构建失败' }
```

将**整个** `$Output` 目录交付给用户电脑，复制到该用户的 `%LOCALAPPDATA%\FileAgentClient\wheelhouse`。wheelhouse 中要包含 `file_agent_mcp` **0.1.1** 本体及其依赖；不要沿用旧版本 wheelhouse，只复制 `apps/mcp` 源码目录不算完成安装。

市场 ZIP 从完整插件源码构建机取得 `integrations/workbuddy/file-agent-marketplace-0.1.85.zip`。如果服务器上的 `build_marketplace.py` 报“市场与插件版本不一致”，说明服务器的两个清单或插件目录只同步了一部分；不要把旧插件单独改成 0.1.85。应复制完整的当前插件交付物，或直接使用上述已构建且校验通过的 0.1.85 ZIP。

## 三、管理员在服务器发布市场 ZIP

将 0.1.85 ZIP 复制到服务器 `E:\file-agent\integrations\workbuddy\file-agent-marketplace-0.1.85.zip`。以下命令在**服务器 PowerShell** 执行：

```powershell
Set-Location E:\file-agent
$SourceZip = 'E:\file-agent\integrations\workbuddy\file-agent-marketplace-0.1.85.zip'
$EnvFile = '.\deploy\.env'
$ComposeFile = '.\deploy\docker-compose.production.yml'
$ExpectedHash = '1DF8411C3FBE9CC07200C60E38CBDA1DB8A7B50456C97A5D6B3652996D036265'

if (-not (Test-Path -LiteralPath $SourceZip -PathType Leaf)) { throw '缺少 0.1.85 市场 ZIP' }
if ((Get-FileHash -LiteralPath $SourceZip -Algorithm SHA256).Hash -ne $ExpectedHash) {
    throw '市场 ZIP 与本版交付包校验值不符'
}
python -m zipfile -t $SourceZip
if ($LASTEXITCODE -ne 0) { throw '市场 ZIP 无法解压' }

$GatewayId = (docker compose --env-file $EnvFile -f $ComposeFile ps -q gateway).Trim()
if (-not $GatewayId) { throw 'gateway 容器未运行' }
docker compose --env-file $EnvFile -f $ComposeFile exec -T gateway mkdir -p /srv/downloads
if ($LASTEXITCODE -ne 0) { throw '无法创建 gateway 下载目录' }
docker cp $SourceZip "${GatewayId}:/srv/downloads/file-agent-marketplace-0.1.85.zip"
if ($LASTEXITCODE -ne 0) { throw '发布带版本市场 ZIP 失败' }
docker cp $SourceZip "${GatewayId}:/srv/downloads/file-agent-marketplace.zip.next"
if ($LASTEXITCODE -ne 0) { throw '复制固定地址临时 ZIP 失败' }
docker exec -u 0 $GatewayId sh -c 'mv /srv/downloads/file-agent-marketplace.zip.next /srv/downloads/file-agent-marketplace.zip && touch /srv/downloads/file-agent-marketplace.zip'
if ($LASTEXITCODE -ne 0) { throw '切换固定市场 ZIP 失败' }
```

还要从服务器的 HTTP 地址**实际下载**核对。把文件放在 `E:\file-agent\integrations\workbuddy` 并不会自动让 Caddy 提供下载；当前静态目录是 gateway 容器内的 `/srv/downloads`。

```powershell
$Url = 'http://10.102.4.241/downloads/file-agent-marketplace.zip'
$CheckZip = Join-Path $env:TEMP 'file-agent-marketplace-server-check.zip'
curl.exe -L --fail --show-error -o $CheckZip $Url
if ($LASTEXITCODE -ne 0) { throw '服务器无法通过固定地址下载市场 ZIP' }
if ((Get-FileHash -LiteralPath $CheckZip -Algorithm SHA256).Hash -ne $ExpectedHash) {
    throw '固定地址返回的不是本版 ZIP'
}
python -m zipfile -t $CheckZip
if ($LASTEXITCODE -ne 0) { throw '固定地址返回的 ZIP 不完整' }
```

**重建 gateway 容器后要重新发布这份 ZIP。**当前部署未把宿主下载目录持久挂载到 `/srv/downloads`；容器重建会丢失之前复制进去的文件。HTTP 200 也可能只是登录页，必须核对哈希和 ZIP 完整性。

## 四、在新用户电脑安装 MCP

以下命令全部在**新用户自己的 Windows 电脑**执行。先把管理员交付的 wheelhouse 完整复制到当前用户的 `%LOCALAPPDATA%\FileAgentClient\wheelhouse`，再打开 PowerShell：

```powershell
Test-NetConnection 10.102.4.241 -Port 80
Invoke-RestMethod 'http://10.102.4.241/api/health'
py -3.11 --version

$ClientRoot = Join-Path $env:LOCALAPPDATA 'FileAgentClient'
$ClientData = Join-Path $env:LOCALAPPDATA 'FileAgentClientData'
$BridgeState = Join-Path $ClientData 'bridge-state'
$WorkBuddyHome = Join-Path $env:USERPROFILE '.workbuddy'
$Python = Join-Path $ClientRoot 'venv\Scripts\python.exe'
$Wheelhouse = Join-Path $ClientRoot 'wheelhouse'

New-Item -ItemType Directory -Force -Path $ClientRoot,$ClientData,$BridgeState | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $WorkBuddyHome 'blobs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BridgeState 'document-cache') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $BridgeState 'downloads') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ClientData 'transfer-state') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $ClientData 'extraction-pages') | Out-Null

if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) {
    throw "请先复制管理员交付的 wheelhouse 到：$Wheelhouse"
}
if (-not (Test-Path -LiteralPath $Python)) {
    py -3.11 -m venv (Join-Path $ClientRoot 'venv')
    if ($LASTEXITCODE -ne 0) { throw '创建 MCP Python 环境失败' }
}
& $Python -m pip install --force-reinstall --no-index --find-links $Wheelhouse file-agent-mcp
if ($LASTEXITCODE -ne 0) { throw 'MCP 安装失败' }
& $Python -c 'import file_agent_mcp; print(file_agent_mcp.__file__)'
if ($LASTEXITCODE -ne 0) { throw 'MCP 安装后无法导入' }
& $Python -c 'from importlib.metadata import version; print(version("file-agent-mcp"))'
# 本版应输出 0.1.1。
```

下面步骤继续使用**同一个 PowerShell 窗口**，以便保留 `$Python`、`$BridgeState` 等变量。不要同时启用旧的 `file-agent-connector` 标准连接器与这里的手工 MCP。

## 五、用新用户账号配置 WorkBuddy MCP

**先确认账号是否存在。**新用户还没有 File Agent 账号时，先打开 `http://10.102.4.241/login`，选择“申请注册”，填写工号、密码（至少 6 位），按需填写显示名称和邮箱。页面提示“注册成功，请登录”后，再用刚注册的账号登录。当前系统的注册接口会直接创建账号及默认工作区；下面的 PowerShell 命令只负责登录取 Token，不能创建账号。已有账号直接使用，无需重复注册。

用户使用**自己的** File Agent 账号登录；密码仅在当前 PowerShell 进程中用于取得 Token。不要把 Token、密码或整个 `mcp.json` 发到聊天、工单或截图中。

```powershell
$ApiBase = 'http://10.102.4.241'
$Username = Read-Host 'File Agent 用户名'
$SecurePassword = Read-Host 'File Agent 密码' -AsSecureString
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $PlainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
    $LoginBody = @{ username = $Username; password = $PlainPassword } | ConvertTo-Json
    $Login = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/auth/login" -ContentType 'application/json; charset=utf-8' -Body $LoginBody
    $Token = $Login.access_token
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr)
    $PlainPassword = $null
}
Invoke-RestMethod -Uri "$ApiBase/api/auth/me" -Headers @{ Authorization = "Bearer $Token" } |
    Select-Object username,display_name,role
```

保存 MCP 配置。若 `mcp.json` 原来存在，先备份并只添加 `file-agent`，不覆盖其他 MCP：

```powershell
$McpPath = Join-Path $WorkBuddyHome 'mcp.json'
$AttachmentRootsJson = @(
    (Join-Path $WorkBuddyHome 'blobs'),
    (Join-Path $BridgeState 'document-cache')
) | ConvertTo-Json -Compress

if (Test-Path -LiteralPath $McpPath) {
    Copy-Item -LiteralPath $McpPath -Destination "$McpPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    $McpConfig = Get-Content -LiteralPath $McpPath -Raw -Encoding utf8 | ConvertFrom-Json
} else {
    $McpConfig = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
}
if (-not $McpConfig.PSObject.Properties['mcpServers']) {
    $McpConfig | Add-Member -NotePropertyName mcpServers -NotePropertyValue ([pscustomobject]@{})
}
$ServerConfig = [pscustomobject]@{
    type = 'stdio'
    alwaysLoad = $true
    command = $Python
    args = @('-m', 'file_agent_mcp')
    env = [pscustomobject]@{
        FILE_AGENT_API_BASE_URL = $ApiBase
        FILE_AGENT_WEB_BASE_URL = $ApiBase
        FILE_AGENT_ACCESS_TOKEN = $Token
        FILE_AGENT_API_TIMEOUT_SECONDS = '30'
        FILE_AGENT_LOCAL_ROOTS = '{}'
        LOCAL_TRANSFER_STATE_DIR = (Join-Path $ClientData 'transfer-state')
        LOCAL_UPLOAD_CONCURRENCY = '2'
        LOCAL_EXTRACTION_PAGE_DIR = (Join-Path $ClientData 'extraction-pages')
        LOCAL_FILE_DOWNLOAD_DIR = (Join-Path $BridgeState 'downloads')
        FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS = $AttachmentRootsJson
        FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR = $BridgeState
        FILE_AGENT_WORKBUDDY_SUBMISSION_TTL_SECONDS = '600'
        NO_PROXY = '10.102.4.241,127.0.0.1,localhost'
    }
    disabled = $false
}
$McpConfig.mcpServers | Add-Member -NotePropertyName 'file-agent' -NotePropertyValue $ServerConfig -Force
[IO.File]::WriteAllText(
    $McpPath,
    ($McpConfig | ConvertTo-Json -Depth 20),
    [Text.UTF8Encoding]::new($false)
)
$Check = Get-Content -LiteralPath $McpPath -Raw -Encoding utf8 | ConvertFrom-Json
$Check.mcpServers.'file-agent' | Select-Object type,command,alwaysLoad,disabled
```

其中 `FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR` 必须与下一节套件选项 `bridge_state_dir` 使用同一路径。默认 `FILE_AGENT_LOCAL_ROOTS='{}'`，聊天附件不依赖本地批量目录授权。

## 六、在新用户电脑安装 WorkBuddy 套件

先在**用户电脑**验证市场地址，避免把登录页当成 ZIP：

```powershell
$MarketUrl = 'http://10.102.4.241/downloads/file-agent-marketplace.zip'
$MarketCheck = Join-Path $env:TEMP 'file-agent-marketplace-client-check.zip'
$ExpectedHash = '1DF8411C3FBE9CC07200C60E38CBDA1DB8A7B50456C97A5D6B3652996D036265'
curl.exe -L --fail --show-error -o $MarketCheck $MarketUrl
if ($LASTEXITCODE -ne 0) { throw '用户电脑无法下载市场 ZIP' }
if ((Get-FileHash -LiteralPath $MarketCheck -Algorithm SHA256).Hash -ne $ExpectedHash) {
    throw '用户电脑下载到的不是 0.1.85 市场 ZIP'
}
& $Python -m zipfile -t $MarketCheck
if ($LASTEXITCODE -ne 0) { throw '用户电脑下载到的 ZIP 不完整' }
(Get-Item -LiteralPath $MarketCheck).Length  # 当前应为 83197
```

在 WorkBuddy 中依次操作：

1. 打开“技能 → 套件 → 市场”，**新增**市场地址 `$MarketUrl`。新用户不需要 `0.1.10.zip` 历史地址。
2. 在该市场安装并启用 `file-agent-workbuddy-bridge`；界面应显示版本 **0.1.85**。
3. 填写套件本机选项：`python_executable` 为 `$Python`，`workbuddy_home` 为 `$WorkBuddyHome`，`bridge_state_dir` 为 `$BridgeState`。如需复制路径，可在 PowerShell 查看：

   ```powershell
   [pscustomobject]@{
       python_executable = $Python.Replace('\','/')
       workbuddy_home = $WorkBuddyHome.Replace('\','/')
       bridge_state_dir = $BridgeState.Replace('\','/')
   } | Format-List
   ```

4. 完全退出 WorkBuddy，包括系统托盘中的后台进程，再启动 WorkBuddy。

如果 WorkBuddy 界面没有显示本机选项，先**完全退出 WorkBuddy**，再在同一个 PowerShell 窗口运行以下备选命令。命令会先备份 `settings.json`，只合并本套件的配置：

```powershell
$SettingsPath = Join-Path $WorkBuddyHome 'settings.json'
$PluginKey = 'file-agent-workbuddy-bridge@file-agent-local-marketplace'
if (-not (Test-Path -LiteralPath $SettingsPath -PathType Leaf)) {
    throw "WorkBuddy 配置不存在：$SettingsPath"
}
Copy-Item -LiteralPath $SettingsPath -Destination "$SettingsPath.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
$Settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8 | ConvertFrom-Json
if (-not $Settings.PSObject.Properties['enabledPlugins']) {
    $Settings | Add-Member -NotePropertyName enabledPlugins -NotePropertyValue ([pscustomobject]@{})
}
if (-not $Settings.PSObject.Properties['pluginConfigs']) {
    $Settings | Add-Member -NotePropertyName pluginConfigs -NotePropertyValue ([pscustomobject]@{})
}
$Settings.enabledPlugins | Add-Member -NotePropertyName $PluginKey -NotePropertyValue $true -Force
$PluginConfig = [pscustomobject]@{
    options = [pscustomobject]@{
        python_executable = $Python.Replace('\','/')
        workbuddy_home = $WorkBuddyHome.Replace('\','/')
        bridge_state_dir = $BridgeState.Replace('\','/')
    }
}
$Settings.pluginConfigs | Add-Member -NotePropertyName $PluginKey -NotePropertyValue $PluginConfig -Force
[IO.File]::WriteAllText(
    $SettingsPath,
    ($Settings | ConvertTo-Json -Depth 20),
    [Text.UTF8Encoding]::new($false)
)
```

随后重新启动 WorkBuddy。

## 七、安装验收

在用户电脑 PowerShell 查看实际市场来源，不要仅凭历史缓存目录猜测版本：

```powershell
$MetaPath = Join-Path $env:USERPROFILE '.workbuddy\plugins\marketplaces\file-agent-local-marketplace\.zip-metadata.json'
$Meta = Get-Content -LiteralPath $MetaPath -Raw -Encoding utf8 | ConvertFrom-Json
$Meta | Select-Object url,downloadedAt,installComplete
```

应看到固定 URL `http://10.102.4.241/downloads/file-agent-marketplace.zip`，`installComplete=True`，WorkBuddy 界面显示并启用套件 **0.1.85**。再检查 WorkBuddy 的 `file-agent` MCP 已连接，能列出文件助手工具。

最后在 WorkBuddy 发**一条新消息**，附上一份非敏感测试文件，并输入：

```text
请用文件助手归档并分类本轮附件。
```

验收结果要同时包含：本轮附件提交引用、`workbuddy_submission_ingest` 工具调用，以及逐文件处理回执。旧消息不会因新安装而自动补发附件。

## 八、常见故障按此顺序排查

| 现象 | 先检查 |
| --- | --- |
| WorkBuddy 仍显示 0.1.10 | 市场是否误加 `...0.1.10.zip`；通过 WorkBuddy 界面改为固定 URL 后同步市场、安装 0.1.85，再完全退出重启。 |
| 更新报 ZIP 尾部签名不存在 | 从用户电脑下载固定 URL，核对 83197 字节、SHA-256 和 ZIP 测试；约 410 字节通常是登录页。 |
| 服务器上有 ZIP，但客户端下载不到 | ZIP 必须发布到 gateway 的 `/srv/downloads`；重建 gateway 后需重发，宿主机 `integrations/workbuddy` 不是静态下载目录。 |
| 打包时报“市场与插件版本不一致” | 构建机两个清单及插件源码未同步；优先使用已校验的完整 0.1.85 ZIP，不把旧插件仅改版本号。 |
| 套件显示 0.1.85，但 MCP 工具不存在 | 检查用户电脑 `mcp.json` 的 Python 路径、虚拟环境中的包、Token 和 WorkBuddy MCP 连接状态；复制源码到服务器不能代替安装。 |
| 附件未形成提交引用 | 确认发的是新消息、套件已启用、三个本机选项正确，`bridge_state_dir` 与 MCP 配置完全一致；完全退出并重启 WorkBuddy。 |

Token 过期或用户账号变更时，重新执行第五节登录并更新该用户电脑的 `FILE_AGENT_ACCESS_TOKEN`，不要复制其他用户的 `mcp.json`。

完成上述验收后，再让该用户处理真实业务文件。详细的市场发布及升级说明见[客户端安装与固定市场地址操作手册](2026-10-16-workbuddy-client-install-and-fixed-marketplace-guide.md)。
