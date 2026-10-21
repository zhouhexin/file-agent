# WorkBuddy 文件助手客户端安装与固定市场地址操作手册

适用日期：2026-10-20。当前附件桥接套件版本：`0.1.85`；新功能需要客户端 MCP `0.1.1`。

本文用于管理员把 File Agent 接入客户的 Windows WorkBuddy，并统一使用以下固定市场地址安装、更新套件：

```text
http://10.102.4.241/downloads/file-agent-marketplace.zip
```

该地址使用服务器现有的 80 端口，不使用临时的 `:8765` 文件服务。本文以服务器目录 `E:\file-agent`、服务器地址 `10.102.4.241` 为例；实际环境不同，应在执行前替换。旧的 [WorkBuddy MCP 与套件部署手册](2026-09-15-workbuddy-mcp-suite-end-user-deployment-guide.md) 使用 `0.1.10.zip` 示例，安装当前套件时以本文的地址和版本为准。

## 1. 先分清三个组件

| 组件 | 安装位置 | 用途 |
| --- | --- | --- |
| File Agent API/Web | 服务器 | 保存和处理文件，提供网页与接口 |
| `file-agent-mcp` | 每台客户电脑的 Python 环境 | 让 WorkBuddy 调用文件助手工具 |
| `file-agent-workbuddy-bridge` 套件 | 每台客户电脑的 WorkBuddy | 捕获本轮聊天附件并提供文件处理 Skill |

服务器代码层 ZIP 不包含 WorkBuddy 市场包，也不会替客户电脑安装 MCP 或套件。只更新服务器时，如果 MCP 契约和套件没有变化，客户电脑无需重复安装；本次要使用新版套件时，应完成本文两侧步骤。

## 2. 服务器管理员：发布固定市场地址

### 2.1 确认来源 ZIP 和服务

当前服务器上的来源 ZIP 预期为：

```text
E:\file-agent\integrations\workbuddy\file-agent-marketplace-0.1.85.zip
```

本项目构建机上的 `0.1.85` 包大小为 `83197` 字节，SHA-256 为 `1df8411c3fbe9cc07200c60e38cbda1db8a7b50456c97a5d6b3652996d036265`。如果服务器文件不同，先核对传输来源和版本，不要把网页文件改名为 ZIP。项目内可用 `python integrations/workbuddy/build_marketplace.py` 重建市场包；脚本会同时生成带版本号和不带版本号的 ZIP。

在服务器 PowerShell 执行：

```powershell
Set-Location E:\file-agent
$Version = '0.1.85'
$SourceZip = "E:\file-agent\integrations\workbuddy\file-agent-marketplace-$Version.zip"
$EnvFile = '.\deploy\.env'
$ComposeFile = '.\deploy\docker-compose.production.yml'

if (-not (Test-Path -LiteralPath $SourceZip -PathType Leaf)) {
    throw "未找到市场 ZIP：$SourceZip"
}
python -m zipfile -t $SourceZip
if ($LASTEXITCODE -ne 0) { throw '来源 ZIP 无法解压' }

$GatewayId = (docker compose --env-file $EnvFile -f $ComposeFile ps -q gateway).Trim()
if (-not $GatewayId) { throw 'gateway 容器未运行' }
```

### 2.2 发布带版本文件和固定地址文件

先完整复制临时文件，再在容器内改为固定文件名，避免客户电脑恰好下载到复制中的半个 ZIP：

```powershell
docker compose --env-file $EnvFile -f $ComposeFile exec -T gateway mkdir -p /srv/downloads
if ($LASTEXITCODE -ne 0) { throw '创建容器下载目录失败' }

docker cp $SourceZip "${GatewayId}:/srv/downloads/file-agent-marketplace-$Version.zip"
if ($LASTEXITCODE -ne 0) { throw '发布带版本市场包失败' }

docker cp $SourceZip "${GatewayId}:/srv/downloads/file-agent-marketplace.zip.next"
if ($LASTEXITCODE -ne 0) { throw '复制固定地址临时包失败' }

docker exec -u 0 $GatewayId sh -c 'mv /srv/downloads/file-agent-marketplace.zip.next /srv/downloads/file-agent-marketplace.zip && touch /srv/downloads/file-agent-marketplace.zip'
if ($LASTEXITCODE -ne 0) { throw '切换固定市场地址失败' }
```

`E:\file-agent\integrations\workbuddy` 是来源目录，并非 Caddy 的下载目录。当前 Caddy 从容器 `/srv` 提供静态文件，因此 `/downloads/...` 必须对应容器内 `/srv/downloads/...`。当前生产 Compose 没有把宿主机的 `data\downloads` 挂载到该路径；仅将 ZIP 放在宿主机目录，不会让下载地址生效。

### 2.3 用实际下载内容验收

只看浏览器页面或 `HTTP 200` 不够：Caddy 找不到 ZIP 时也可能返回 200 和登录页。必须下载并比较哈希：

```powershell
$MarketUrl = 'http://10.102.4.241/downloads/file-agent-marketplace.zip'
$DownloadedZip = Join-Path $env:TEMP 'file-agent-marketplace-http-check.zip'
curl.exe -L --fail --show-error -o $DownloadedZip $MarketUrl
if ($LASTEXITCODE -ne 0) { throw '固定市场地址下载失败' }

$SourceHash = (Get-FileHash -LiteralPath $SourceZip -Algorithm SHA256).Hash
$DownloadHash = (Get-FileHash -LiteralPath $DownloadedZip -Algorithm SHA256).Hash
if ($SourceHash -ne $DownloadHash) { throw '下载内容与来源 ZIP 不一致' }

python -m zipfile -t $DownloadedZip
if ($LASTEXITCODE -ne 0) { throw '下载到的文件不是完整 ZIP' }
Get-Item -LiteralPath $DownloadedZip | Select-Object FullName,Length
```

在客户电脑上也应从同一 URL 下载验证一次，排除客户网络或代理返回不同内容。当前 `0.1.85` 应为 `83197` 字节。

上述 `docker cp` 是当前环境的快速发布方式。重建 `gateway` 容器后，容器内文件会消失，应重新执行本节发布与验收。长期使用可按旧部署手册为 `gateway` 配置 `../data/downloads:/srv/downloads:ro` 只读绑定卷，再由宿主机持久化管理市场 ZIP；修改 Compose 并重建容器前应先安排维护窗口。

## 3. 管理员：准备客户电脑需要的 MCP 安装包

每台客户电脑需要 `file-agent-mcp`，不能仅安装 WorkBuddy 套件。在具备 Python 3.11 和 Python 包下载能力的构建机上，从本次服务器代码对应的 `apps/mcp` 生成 wheelhouse：

```powershell
$Source = 'E:\PycharmProject\file-agent\apps\mcp'
$Output = 'E:\PycharmProject\file-agent\release\file-agent-mcp-client-wheelhouse'
New-Item -ItemType Directory -Force -Path $Output | Out-Null
py -3.11 -m pip wheel --wheel-dir $Output $Source
if ($LASTEXITCODE -ne 0) { throw 'MCP 客户端离线包构建失败' }
```

把整个 wheelhouse 目录复制给客户电脑，例如放在当前 Windows 用户的 `%LOCALAPPDATA%\FileAgentClient\wheelhouse`。若本轮只变更服务器代码、MCP 代码没有变更，可沿用客户电脑已有的 MCP 安装。

## 4. 每台客户电脑：安装或升级 MCP

以下命令在**客户电脑**执行。先确认 WorkBuddy 已安装、客户有自己的 File Agent 账号、电脑安装了 64 位 Python 3.11，且能访问服务器：

```powershell
Test-NetConnection 10.102.4.241 -Port 80
Invoke-RestMethod 'http://10.102.4.241/api/health'
py -3.11 --version
```

在当前 Windows 用户目录创建 MCP 环境与桥接缓存，避免假定每台客户电脑都有 `D:` 盘：

```powershell
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

if (-not (Test-Path -LiteralPath $Python)) { py -3.11 -m venv (Join-Path $ClientRoot 'venv') }
if (-not (Test-Path -LiteralPath $Wheelhouse -PathType Container)) {
    throw "请先把管理员提供的 wheelhouse 放到：$Wheelhouse"
}
& $Python -m pip install --force-reinstall --no-index --find-links $Wheelhouse file-agent-mcp
if ($LASTEXITCODE -ne 0) { throw 'MCP 安装失败' }
& $Python -c 'import file_agent_mcp; print(file_agent_mcp.__file__)'
```

若客户已有经过验证的 MCP 环境，也可继续使用原 Python 路径；后面的 `mcp.json` 与套件选项必须填写同一实际路径。不要同时启用 `file-agent-connector` 标准连接器和本文的手工 MCP。

## 5. 每台客户电脑：登录并配置 MCP

继续使用第 4 节的同一个 PowerShell 窗口，并用客户自己的账号获取 Token。下面的密码只在本次 PowerShell 内存中使用，不写入脚本：

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

不要把 Token 或整个 `mcp.json` 发到聊天、工单和截图。继续在**同一个 PowerShell 窗口**写入配置；如已关闭窗口，重新登录取得 `$Token`。

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
$McpConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $McpPath -Encoding utf8

$Check = Get-Content -LiteralPath $McpPath -Raw -Encoding utf8 | ConvertFrom-Json
$Check.mcpServers.'file-agent' | Select-Object type,command,alwaysLoad,disabled
```

默认 `FILE_AGENT_LOCAL_ROOTS='{}'`，即不授权任意本地目录批量导入；聊天附件功能不依赖它。若客户明确需要批量导入，按旧部署手册配置受控逻辑根名和实际目录，不要在聊天中临时授权绝对路径。

## 6. 每台客户电脑：安装或更新 WorkBuddy 套件

先在客户电脑验证固定 URL 下载的是 ZIP：

```powershell
$MarketUrl = 'http://10.102.4.241/downloads/file-agent-marketplace.zip'
$MarketCheck = Join-Path $env:TEMP 'file-agent-marketplace-client-check.zip'
curl.exe -L --fail --show-error -o $MarketCheck $MarketUrl
if ($LASTEXITCODE -ne 0) { throw '无法从客户电脑下载市场包' }
& $Python -m zipfile -t $MarketCheck
if ($LASTEXITCODE -ne 0) { throw '下载内容不是完整 ZIP' }
Get-Item -LiteralPath $MarketCheck | Select-Object FullName,Length
```

当前 `0.1.85` 下载文件应为 `83197` 字节。若只有约 `410` 字节，或浏览器显示登录页，说明地址返回的是网页，应回到第 2 节修复发布，不能继续点击 WorkBuddy“更新”。

在 WorkBuddy 中进入“技能 → 套件 → 市场”：

1. 新客户添加固定市场地址 `$MarketUrl`，安装并启用 `file-agent-workbuddy-bridge`。
2. 旧客户若市场仍指向 `...0.1.10.zip` 或其他带版本地址，先通过 WorkBuddy 界面把市场地址改为固定地址；如果界面不能编辑，记录现有套件配置后，通过界面移除旧市场、重新添加固定地址并安装套件。不要只手工修改 `.zip-metadata.json`。
3. 填写套件的三个本机选项：`python_executable` 为 `$Python`，`workbuddy_home` 为 `$WorkBuddyHome`，`bridge_state_dir` 为 `$BridgeState`。在 PowerShell 中运行下列命令可得到实际值，不要照抄其他电脑的用户名和盘符：

```powershell
[pscustomobject]@{
    python_executable = $Python.Replace('\','/')
    workbuddy_home = $WorkBuddyHome.Replace('\','/')
    bridge_state_dir = $BridgeState.Replace('\','/')
} | Format-List
```

`bridge_state_dir` 必须与 `mcp.json` 的 `FILE_AGENT_WORKBUDDY_BRIDGE_STATE_DIR` 完全一致；该目录下的 `document-cache` 已包含在 `FILE_AGENT_WORKBUDDY_ATTACHMENT_ROOTS` 中。安装后完全退出 WorkBuddy，包括系统托盘中的后台进程，再启动 WorkBuddy。

如果 WorkBuddy 界面没有显示这三个选项，可在**完全退出 WorkBuddy 后**运行以下备选命令。它会先备份 `settings.json`，再只合并本套件的配置，不覆盖其他套件：

```powershell
$WorkBuddyHome = Join-Path $env:USERPROFILE '.workbuddy'
$Python = Join-Path (Join-Path $env:LOCALAPPDATA 'FileAgentClient') 'venv\Scripts\python.exe'
$BridgeState = Join-Path (Join-Path $env:LOCALAPPDATA 'FileAgentClientData') 'bridge-state'
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
$Settings | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $SettingsPath -Encoding utf8
```

## 7. 每台客户电脑：验收

### 7.1 安装记录与启用状态

```powershell
$MarketMetaPath = Join-Path $env:USERPROFILE '.workbuddy\plugins\marketplaces\file-agent-local-marketplace\.zip-metadata.json'
$MarketMeta = Get-Content -LiteralPath $MarketMetaPath -Raw -Encoding utf8 | ConvertFrom-Json
$MarketMeta | Select-Object url,downloadedAt,installComplete

$SettingsPath = Join-Path $env:USERPROFILE '.workbuddy\settings.json'
$PluginKey = 'file-agent-workbuddy-bridge@file-agent-local-marketplace'
$Settings = Get-Content -LiteralPath $SettingsPath -Raw -Encoding utf8 | ConvertFrom-Json
$Settings.enabledPlugins.$PluginKey
$Settings.pluginConfigs.$PluginKey.options | Format-List
```

`url` 应为固定地址，`installComplete` 与启用状态应为 `True`，WorkBuddy 界面应显示套件 `0.1.85`。历史缓存目录里存在 `0.1.85`，只能说明曾下载过，不能单独证明本次更新成功或 Hook 已运行。

### 7.2 实际功能测试

1. 在 WorkBuddy 确认 `file-agent` MCP 已连接，能列出文件助手工具；若 MCP 不可用，先检查 `mcp.json`、Python 路径和 `/api/health`。
2. 在**新消息**中附上一份非敏感测试文件，并输入：

   ```text
   请用文件助手归档并分类本轮附件。
   ```

3. 确认 WorkBuddy 生成本轮附件提交引用，调用 `workbuddy_submission_ingest`，并返回逐文件处理结果。旧消息不会因为更新套件而自动补发附件。
4. 如需检查 Hook，可查看 `$BridgeState\capture-diagnostics.jsonl` 的最后 30 行；出现 `PENDING_MANIFEST_CREATED` 表示已建立提交引用，还需继续确认 MCP 调用与服务器结果：

   ```powershell
   Get-Content -LiteralPath (Join-Path $BridgeState 'capture-diagnostics.jsonl') -Tail 30 -Encoding utf8
   ```

## 8. 后续更新的固定流程

每次发布新版套件时：

1. 先在源码的 `integrations/workbuddy/file-agent-plugin/.codebuddy-plugin/plugin.json` 和 `integrations/workbuddy/.codebuddy-plugin/marketplace.json` 中递增为同一个版本，使用 `python integrations/workbuddy/build_marketplace.py` 生成并校验新版本 ZIP。
2. 把新版本 ZIP 放到服务器 `E:\file-agent\integrations\workbuddy`，按第 2 节将它同时发布成带版本文件和固定地址的 `file-agent-marketplace.zip`。保留旧的带版本 ZIP 以便核对和回退，不要把新内容伪装成旧版本文件。
3. 在客户电脑按第 6 节下载固定地址，确认是新版完整 ZIP，再在 WorkBuddy 中同步市场并更新套件。固定市场 URL 无需再次修改。
4. 完全退出并重新打开 WorkBuddy，按第 7 节检查版本与新附件流程。如果新版同时修改了 MCP 工具代码，也要按第 3、4 节升级客户电脑的 MCP wheel，并重启 WorkBuddy。

## 9. 常见问题

| 现象 | 原因与处理 |
| --- | --- |
| WorkBuddy 报 `End of central directory record signature not found` | 它实际下载到的不是完整 ZIP。先读 `.zip-metadata.json` 的 `url`，再从**客户电脑**下载该 URL，核对字节数、哈希与 ZIP 完整性。 |
| 下载只有约 `410` 字节，浏览器显示登录页 | Caddy 找不到指定文件，回退返回前端页面。重新执行第 2 节发布；只看到 `HTTP 200` 不能算成功。 |
| 已发布 `0.1.85`，WorkBuddy 仍请求 `0.1.10.zip` | 旧市场地址仍留在客户电脑。通过 WorkBuddy 市场管理把它更换成固定 URL；发布新文件不会自动改旧地址。 |
| `:8765` 无法连接 | 8765 是临时 Python 文件服务端口，不随服务器代码部署自动启动。本文固定地址使用现有 80 端口，不依赖 8765。 |
| 更新成功后附件仍无调用 | 核对套件已启用、三个本机选项、MCP 已连接、`bridge_state_dir` 一致，并完全退出重启 WorkBuddy；必须上传新附件发新消息测试。 |
| 重建 gateway 后下载又变成登录页 | 当前使用容器内复制方式，容器重建会丢失 ZIP。重新执行第 2 节，或配置持久只读下载目录挂载。 |

## 10. 客户交付核对

- [ ] 客户电脑可访问 `http://10.102.4.241/api/health`。
- [ ] 固定市场 URL 下载到有效 ZIP；当前 `0.1.85` 为 `83197` 字节。
- [ ] 客户自己的 File Agent 账号登录成功，未共享 Token、密码或整个 `mcp.json`。
- [ ] 客户电脑 `file-agent-mcp` 可导入，WorkBuddy 显示 `file-agent` MCP 已连接。
- [ ] WorkBuddy 市场元数据指向固定 URL，套件显示 `0.1.85` 并启用。
- [ ] 三个套件选项使用本机真实路径，`bridge_state_dir` 与 MCP 配置一致。
- [ ] 新测试附件触发提交引用、MCP 工具调用和逐文件回执。
- [ ] 未同时启用 `file-agent-connector` 标准连接器。
