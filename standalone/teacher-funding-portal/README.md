# 教师资助查询独立服务

教师用工号注册、登录、改密，并查看本人资助金额和使用明细。服务有独立进程、网页和端口，**复用 FileAgent 现有 PostgreSQL 数据库与数据库账号，只新增 `teacher_funding_` 前缀的业务表**。不会复用或改写 FileAgent 的 `users` 等现有业务表，也不要求单独创建数据库、数据库账号。

## 发布包与重复更新（推荐操作）

以后不需要手工覆盖整个目录。使用“开发电脑生成 ZIP → 复制 ZIP 和校验文件到服务器 → 执行更新脚本”的流程。首次安装也使用同一个包。服务器 `.env`、名单、账号、金额和已有域名配置保留。

### 1. 开发电脑生成代码发布包

```powershell
Set-Location 'E:\PycharmProject\file-agent\standalone\teacher-funding-portal'
$Python = 'D:\anaconda\envs\myenv\python.exe'
& $Python -B .\scripts\build_release.py
if ($LASTEXITCODE -ne 0) { throw '查询服务打包失败' }
```

输出会给出实际版本、ZIP 路径和 SHA-256。默认文件保存在项目 `release\teacher-funding-portal` 下，每次自动使用时间作为版本号；也可传入 `--version 20261106-release1` 指定版本，已有同版本 ZIP 不会覆盖。

发布包只包含运行代码、静态资源、依赖、Docker/Compose 配置、更新脚本和说明文档，不包含 `.env`、SQLite、测试数据、日志、历史备份或真实教师数据。包中有逐文件校验清单，ZIP 旁有同名 `.zip.sha256` 文件。

将新生成的两个文件一起复制到服务器，例如：

```text
E:\file-agent\release\teacher-funding-portal\teacher-funding-portal-版本号.zip
E:\file-agent\release\teacher-funding-portal\teacher-funding-portal-版本号.zip.sha256
```

### 2. 第一次接入更新脚本（只做一次）

适用于服务器尚未安装，或旧版本目录还没有 `scripts\update.ps1`。在服务器 PowerShell 执行下面整段，输入实际 ZIP 路径。先验证整个包的 SHA-256，再把更新脚本解到新的临时目录运行：

```powershell
$ErrorActionPreference = 'Stop'
$Package = (Resolve-Path -LiteralPath (Read-Host '请输入服务器上查询服务 ZIP 的完整路径')).Path
$ExpectedHash = (Get-Content -LiteralPath "$Package.sha256" -Raw).Trim()
if ($ExpectedHash -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash -ne $ExpectedHash) {
    throw '发布包校验失败，不能执行更新'
}
$Bootstrap = Join-Path (Split-Path -Parent $Package) ('bootstrap-' + [guid]::NewGuid().ToString('N'))
Expand-Archive -LiteralPath $Package -DestinationPath $Bootstrap
$TargetRoot = 'E:\file-agent\standalone\teacher-funding-portal'
$OnlyInstall = -not (Test-Path -LiteralPath (Join-Path $TargetRoot '.env'))
& (Join-Path $Bootstrap 'scripts\update.ps1') -Package $Package -TargetRoot $TargetRoot -InstallOnly:$OnlyInstall
if ($LASTEXITCODE -ne 0) { throw '查询服务安装或更新失败' }
```

已有服务器 `.env` 时，这一步会完成代码更新、构建、重启和健康检查。首次新装没有 `.env` 时，只安装代码并提示配置；接着按后面的“服务器配置说明”设置 `.env`、数据库连接和 Docker 网络，之后执行第 3 步启动服务。

### 3. 后续每次更新：执行同一个命令

每次只把新 ZIP 及其 `.sha256` 复制到服务器，再执行：

```powershell
$Package = (Resolve-Path -LiteralPath (Read-Host '请输入本次查询服务 ZIP 的完整路径')).Path
& 'E:\file-agent\standalone\teacher-funding-portal\scripts\update.ps1' -Package $Package -TargetRoot 'E:\file-agent\standalone\teacher-funding-portal'
if ($LASTEXITCODE -ne 0) { throw '查询服务更新失败，请查看脚本输出的备份路径和错误信息' }
```

脚本会自动执行：校验整个 ZIP 和逐文件 SHA-256、检查文件路径、备份旧代码和旧镜像、替换程序、删除旧版本不再使用的代码文件、构建新镜像、只重启查询容器、从容器内验证健康状态和发布版本。构建失败时原容器继续运行；切换后检查失败时，尝试恢复旧代码和旧镜像并重新启动旧服务。

成功输出类似：

```text
release=ok version=20261106-release1 backup=E:\file-agent\standalone\teacher-funding-portal\.release-backups\时间及编号
```

服务器 `.env` 不被替换，数据库不被清空，不重复导入 156 人名单，也不修改原 FileAgent、MCP、WorkBuddy 或 Caddy 域名配置。新版本启动时仍可能补建所需查询服务表；回退只恢复代码和镜像，不回退 PostgreSQL 数据。首次启动没有旧镜像时不具备旧版本容器回退条件。

### 4. 更新后验证和可选检查

```powershell
Invoke-RestMethod 'http://127.0.0.1:8766/health'
```

应返回 `status=ok`、`service=teacher-funding-portal` 和本次 `version`。再用手机或电脑通过原有 IP/域名访问，刷新页面，核对两个标签和明细。

只检查包、不替换文件也不调用 Docker：

```powershell
& 'E:\file-agent\standalone\teacher-funding-portal\scripts\update.ps1' -Package $Package -ValidateOnly
```

脚本是 Windows PowerShell 脚本，不要求服务器另外安装 Python；Docker 镜像中会包含 Python。构建镜像仍需要基础镜像和依赖可用，这不是离线镜像交付包。保留 `.release-backups` 中旧代码和 `teacher-funding-portal:rollback-*` 镜像用于故障排查，未确认更新稳定前不要清理它们。

## 服务器配置说明（与发布包安装配合）

本节按现有 Windows 服务器项目目录 `E:\file-agent`、FileAgent 已通过 `deploy/docker-compose.production.yml` 正常运行编写。查询服务仍使用单独 Compose 项目，只连接现有 Docker 网络，不加入 FileAgent 的服务启动列表。

### 1. 首次安装后准备配置

先按“发布包与重复更新”完成代码安装，不再手工复制整个目录。不要覆盖服务器已有 `.env`，也不要把本机的 `127.0.0.1:8000` 同步配置直接用于容器。

在服务器 PowerShell 执行：

```powershell
Set-Location 'E:\file-agent\standalone\teacher-funding-portal'
if (-not (Test-Path -LiteralPath '.\.env')) {
    Copy-Item -LiteralPath '.\.env.example' -Destination '.\.env'
}
notepad .\.env
```

设置管理员账号、密码、独立签名密钥、有效 FileAgent API 访问令牌和两份 Excel 的完整文件名。数据库连接使用服务器 `E:\file-agent\deploy\.env` 中原有的 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`，填写格式如下，不创建新账号或新数据库：

```dotenv
PORTAL_DATABASE_URL=postgresql://现有数据库用户:现有数据库密码@postgres:5432/现有数据库名
PORTAL_HOST_PORT=8766
FILE_AGENT_DOCKER_NETWORK=file-agent_internal
```

数据库用户名或密码含 `@`、`:`、`/`、`#` 等 URL 特殊字符时，需要采用 URL 编码，不能原样拼入地址。不要把示例文字当作实际值。

`compose.fileagent-server.yml` 将查询容器加入现有 `file-agent_internal` 网络。这样可直接访问已有 `postgres:5432` 和 `api:8000`，无需对外开放 PostgreSQL 端口。同步 API 地址会由该配置设为 `http://api:8000`。

检查网络存在：

```powershell
docker network inspect file-agent_internal --format '{{.Name}}'
if ($LASTEXITCODE -ne 0) { throw '未找到 FileAgent 网络，请确认原服务已启动并核对实际网络名称' }
```

若现有 FileAgent 使用了其他 Compose 项目名，网络名称也可能不同，运行 `docker network ls` 查看后，把 `.env` 的 `FILE_AGENT_DOCKER_NETWORK` 改为对应名称。

### 2. 构建并启动查询服务

使用前面更新脚本的第 3 步即可完成构建和启动。以下是需要手动运维时可用的原始 Compose 命令；它不会执行发布包校验和自动回退，正常发布请优先使用更新脚本。

```powershell
docker compose -p teacher-funding-portal --env-file .\.env -f .\compose.yml -f .\compose.fileagent-server.yml up -d --build
if ($LASTEXITCODE -ne 0) { throw '教师查询服务部署失败' }
docker compose -p teacher-funding-portal --env-file .\.env -f .\compose.yml -f .\compose.fileagent-server.yml ps
```

首次构建需要服务器能够获取 Python 基础镜像和依赖；离线服务器需预先准备本服务镜像。启动时补建查询服务需要的前缀表，不要求重新构建 FileAgent 基础镜像、更新 MCP 或更新 WorkBuddy 插件。

### 3. 检查并访问

```powershell
Invoke-RestMethod 'http://127.0.0.1:8766/health'
docker compose -p teacher-funding-portal --env-file .\.env -f .\compose.yml -f .\compose.fileagent-server.yml logs --tail=100
```

健康检查应显示 `status=ok`、`service=teacher-funding-portal`。浏览器访问 `http://服务器IP:8766`，使用本服务管理员登录，确认教师名单、余额表同步状态和资助总额明细同步状态。手机通过可访问服务器的网络打开同一个地址；无法从其他电脑访问时检查服务器防火墙或网络策略是否允许 8766 端口。

名单是否已经存在取决于连接的数据库。此前 156 人已迁入的数据库可以直接复用，不要重复执行 SQLite 迁移；如果服务器使用的是另一套 PostgreSQL，复制代码并不会自动带过去名单和账号，需要先迁移相应 `teacher_funding_` 数据或在管理页维护名单。

### 4. 后续更新

按“发布包与重复更新”生成新包并执行更新脚本，保留服务器 `.env`。只更新本服务，数据仍保存在现有 PostgreSQL 中，随现有数据库备份一起保护。也可以按后文 Python 启动方式部署，但现有 Docker 内部的 `postgres` 地址不能直接用于宿主机 Python；宿主机运行时必须使用实际可达的数据库地址。

## 配置独立域名访问（现有服务器 Caddy 网关）

本节适用于上文的服务器 Docker 部署：FileAgent 和查询服务已经运行、同属 `file-agent_internal` 网络。以 `funding.example.edu.cn` 为示例，实际操作需使用学校分配或你有管理权的域名。访问入口使用独立子域名，不使用 `/funding` 子路径，页面的 `/api`、`/static` 等请求都转发给查询服务。

现有 FileAgent 已占用 80/443 端口，所以复用它的 Caddy 网关，不再启动一个争用相同端口的网关。FileAgent 原站点配置保留，查询服务添加为另一个站点。以下操作不改数据库、教师账号、原有 FileAgent 域名或 WorkBuddy 安装地址。

### 1. 选择访问范围和 HTTPS 方式

| 实际情况 | 域名解析和证书方式 | 后面模式选择 |
|---|---|---|
| 先验证校内域名能连通 | 校内 DNS 把域名解析到 `10.102.4.241`，暂用 HTTP | `http` |
| 仅校内或 VPN 使用，学校能提供域名证书 | 校内 DNS 指向内网服务器，使用学校提供的匹配域名证书 | `certificate` |
| 有公网 IP 或学校已转发公网 80/443 到本机 | 公网 DNS 指向公网入口，Caddy 自动申请和续期证书 | `public` |

`10.102.4.241` 是内网 IP。DNS 指向它只能让可访问学校内网的设备访问，不能让移动数据网络直接访问。需要校外直接访问时，请学校网络管理员提供公网入口、80/443 转发或统一网关；域名本身不能代替网络连通配置。

HTTP 模式用于检查连通性，正式使用账号密码前应完成 HTTPS。学校已有统一 HTTPS 网关时，也可以由学校网关终止 TLS，再将该域名转发到本服务，但要明确 80/443 由谁接入，不能让两个网关在同一地址争用端口。

### 2. 设置 DNS 解析

请域名管理员新增记录：

| 字段 | 校内示例 | 公网示例 |
|---|---|---|
| 类型 | A | A |
| 主机记录 | `funding`（在学校实际域名下） | `funding`（在实际域名下） |
| 记录值 | `10.102.4.241` | 学校提供的公网入口 IP |
| TTL | 使用 DNS 平台默认值 | 使用 DNS 平台默认值 |

校内 DNS 需要让教师电脑和手机实际使用该 DNS 或学校 VPN。公网模式下，公网入口的 TCP 80/443 要能到达现有 Caddy，Caddy 也需要访问证书服务；如配置 AAAA 记录，IPv6 路由也必须正确。无需为本功能把 PostgreSQL 5432 或查询服务 8766 端口转发到公网。

在服务器 PowerShell 设置域名变量并检查解析：

```powershell
Set-Location 'E:\file-agent'
$ErrorActionPreference = 'Stop'
$PortalDomain = (Read-Host '请输入查询服务的完整域名，不带 http:// 或路径').Trim().ToLowerInvariant()
if ($PortalDomain -notmatch '^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$') {
    throw '请填写完整域名，例如 funding.example.edu.cn'
}
Resolve-DnsName $PortalDomain
```

解析结果应符合选定访问范围。不要把现有 FileAgent 的 `CADDY_SITE_ADDRESS` 改成查询域名，也不要使用与现有 FileAgent 完全相同的域名创建第二个站点。

### 3. 确认后端和网关互通

以下命令均在服务器 `E:\file-agent` 执行。保留本 PowerShell 窗口，以便后续继续使用变量：

```powershell
$EnvFile = '.\deploy\.env'
$BaseCompose = '.\deploy\docker-compose.production.yml'
$PortalOverride = '.\deploy\gateway-portal.override.yml'
$GatewayIds = @(docker compose --env-file $EnvFile -f $BaseCompose ps -q gateway)
if ($LASTEXITCODE -ne 0 -or $GatewayIds.Count -ne 1) { throw '未找到唯一运行中的 gateway 容器' }
$GatewayId = $GatewayIds[0].Trim()
docker exec $GatewayId wget -qO- http://teacher-funding-portal:8080/health
if ($LASTEXITCODE -ne 0) { throw '网关无法访问教师查询服务，请先检查查询服务及共享 Docker 网络' }
```

应返回 `status: ok` 和 `service: teacher-funding-portal`。查询服务运行在宿主机 Python 而不是 Docker 时，需要先让它监听 `0.0.0.0:8766`，并将后文站点中的上游改成 `host.docker.internal:8766`；当前文档默认仍是 Docker 的 `teacher-funding-portal:8080`。

### 4. 准备证书（仅 certificate 模式需要）

由学校或证书服务提供匹配查询域名的完整证书链和私钥。在服务器上分别存为：

```text
E:\file-agent-secrets\teacher-funding-certs\fullchain.pem
E:\file-agent-secrets\teacher-funding-certs\privkey.pem
```

目录位于代码仓库外，只允许服务器管理人员访问，私钥不应提交到 Git。证书需要覆盖完整查询域名，并能被教师电脑和手机信任。使用学校内部 CA 时，终端也需信任学校 CA。

后面的脚本会把该目录只读挂载到 Caddy。`public` 模式无需提供这两个文件，Caddy 在 DNS 和端口满足条件后自动申请证书；`http` 模式暂不使用证书。

### 5. 首次生成查询域名配置，并备份网关文件

这一段只在首次增加域名时执行。后续修改域名、模式或证书，直接编辑 `deploy\Caddyfile.portal`，不要反复追加新站点。

```powershell
$PortalMode = (Read-Host '请选择模式：http / certificate / public').Trim().ToLowerInvariant()
if ($PortalMode -notin @('http', 'certificate', 'public')) { throw '模式填写错误' }
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$LiveBackup = Join-Path (Get-Location) "deploy\Caddyfile.before-portal-$Stamp"
$PortalCaddy = Join-Path (Get-Location) 'deploy\Caddyfile.portal'
if (Test-Path -LiteralPath $PortalCaddy) { throw 'Caddyfile.portal 已存在，请直接编辑现有文件，跳过首次生成步骤' }
docker cp "${GatewayId}:/etc/caddy/Caddyfile" $LiveBackup
if ($LASTEXITCODE -ne 0) { throw '备份运行中的 Caddy 配置失败，已停止' }
$CurrentCaddy = [IO.File]::ReadAllText($LiveBackup)
$CertDir = 'E:\file-agent-secrets\teacher-funding-certs'
New-Item -ItemType Directory -Force -Path $CertDir | Out-Null

switch ($PortalMode) {
    'http' {
        $PortalBlock = @"
# teacher-funding-portal-domain
http://$PortalDomain {
    encode zstd gzip
    reverse_proxy teacher-funding-portal:8080
}
"@
    }
    'public' {
        $PortalBlock = @"
# teacher-funding-portal-domain
$PortalDomain {
    encode zstd gzip
    reverse_proxy teacher-funding-portal:8080
}
"@
    }
    'certificate' {
        if (-not (Test-Path -LiteralPath (Join-Path $CertDir 'fullchain.pem'))) { throw '缺少 fullchain.pem' }
        if (-not (Test-Path -LiteralPath (Join-Path $CertDir 'privkey.pem'))) { throw '缺少 privkey.pem' }
        $PortalBlock = @"
# teacher-funding-portal-domain
$PortalDomain {
    tls /etc/caddy/teacher-funding-certs/fullchain.pem /etc/caddy/teacher-funding-certs/privkey.pem
    encode zstd gzip
    reverse_proxy teacher-funding-portal:8080
}
"@
    }
}
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($PortalCaddy, $CurrentCaddy + "`r`n" + $PortalBlock + "`r`n", $Utf8NoBom)
```

配置包含了网关当前运行的原站点以及新增查询站点，整个查询域名的请求都转给查询服务，包括首页、静态样式和注册/登录接口，避免误转到 FileAgent 登录页。

当前市场下载包可能是直接复制在网关容器的 `/srv/downloads` 中。为了避免下一步重建网关时丢失，先保存到宿主机，随后一并挂载：

```powershell
$DownloadDir = Join-Path (Get-Location) 'data\gateway-downloads'
New-Item -ItemType Directory -Force -Path $DownloadDir | Out-Null
docker exec $GatewayId test -d /srv/downloads
if ($LASTEXITCODE -eq 0) {
    docker cp "${GatewayId}:/srv/downloads/." $DownloadDir
    if ($LASTEXITCODE -ne 0) { throw '下载目录备份失败，不能继续重建网关' }
}
if (Test-Path -LiteralPath $PortalOverride) {
    Copy-Item -LiteralPath $PortalOverride -Destination "$PortalOverride.before-$Stamp"
}
$OverrideContent = @'
services:
  gateway:
    volumes:
      - type: bind
        source: ./Caddyfile.portal
        target: /etc/caddy/Caddyfile
        read_only: true
      - type: bind
        source: E:/file-agent-secrets/teacher-funding-certs
        target: /etc/caddy/teacher-funding-certs
        read_only: true
      - type: bind
        source: ../data/gateway-downloads
        target: /srv/downloads
'@
[IO.File]::WriteAllText((Join-Path (Get-Location) 'deploy\gateway-portal.override.yml'), $OverrideContent, $Utf8NoBom)
```

上面只新增覆盖文件，原 `deploy/docker-compose.production.yml` 不修改；原有 `caddy_data`、`caddy_config` 卷仍保留。这里的目录按 `E:\file-agent` 编写，服务器安装在其他盘符时要同步改证书挂载路径。

### 6. 校验并使网关生效

先校验 YAML 和 Caddy 配置，任何一步失败都先处理错误，不要继续重建：

```powershell
docker compose --env-file $EnvFile -f $BaseCompose -f $PortalOverride config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Compose 配置校验失败' }
docker compose --env-file $EnvFile -f $BaseCompose -f $PortalOverride run --rm --no-deps gateway caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw 'Caddy 配置校验失败，原运行网关尚未替换' }
docker compose --env-file $EnvFile -f $BaseCompose -f $PortalOverride up -d --no-deps --force-recreate gateway
if ($LASTEXITCODE -ne 0) { throw '网关配置应用失败，请查看日志' }
docker compose --env-file $EnvFile -f $BaseCompose -f $PortalOverride logs --tail=100 gateway
```

应用覆盖文件时会重建网关，网页访问可能短暂中断；数据库、API 和 worker 不会因此重启。`public` 模式需要等待证书申请完成，若失败，日志会说明 DNS、端口或证书服务方面的问题。不要删除现有 Caddy 数据卷，它保存自动管理证书的状态。

### 7. 在电脑和手机验证

先在服务器检查：

```powershell
Resolve-DnsName $PortalDomain
if ($PortalMode -eq 'http') {
    Invoke-RestMethod "http://$PortalDomain/health"
} else {
    Invoke-RestMethod "https://$PortalDomain/health"
}
```

应返回教师查询服务的健康状态。再用教师电脑和手机打开相同域名，验证：登录/注册、两个标签页、金额明细、日期展示和样式均正常；同时检查原 FileAgent 地址仍能访问、WorkBuddy 市场包固定下载地址仍能下载。

DNS 尚未生效时，可用下列命令临时验证域名与服务器绑定，不修改终端 DNS。HTTP 和 HTTPS 按模式二选一；HTTPS 命令保留证书校验，不使用 `-k`：

```powershell
curl.exe --resolve "${PortalDomain}:80:10.102.4.241" --fail --show-error "http://$PortalDomain/health"
curl.exe --resolve "${PortalDomain}:443:10.102.4.241" --fail --show-error "https://$PortalDomain/health"
```

手机在校内 Wi-Fi 成功、切换移动数据失败，通常说明当前只配置了内网访问范围，需学校公网入口或 VPN，并不是页面手机适配问题。

### 8. 后续更新、证书续期与恢复

- 查询服务代码更新：仍使用前面的独立查询服务 Compose 命令，不用重做域名步骤。
- 以后更新或重建 FileAgent 网关时，必须继续带 `-f .\deploy\gateway-portal.override.yml`；否则网关会恢复镜像里的原配置，域名和下载目录挂载可能失效。
- `public` 模式由 Caddy 管理证书续期；学校提供的证书由管理员在到期前替换宿主机证书文件，然后重新校验并重建网关。域名和证书匹配、终端信任关系都要保持有效。
- 修改已有 `Caddyfile.portal` 或替换证书后，重复第 6 步校验和 `--force-recreate gateway` 命令；仅保存文件、仅刷新浏览器不会让正在运行的 Caddy 重新加载配置。
- 备份 `deploy\Caddyfile.portal`、`deploy\gateway-portal.override.yml`、下载目录、证书目录及现有 Caddy 数据卷。私钥不进入代码交付包。

如果要撤回新增域名，恢复第 5 步备份的 Caddy 配置到 `deploy\Caddyfile.portal`，保留下载目录挂载，再校验和重建。不要通过删除数据卷来回退：

```powershell
# 在同一窗口中，$LiveBackup 指向第 5 步创建的实际备份文件。
Copy-Item -LiteralPath $LiveBackup -Destination '.\deploy\Caddyfile.portal' -Force
docker compose --env-file $EnvFile -f $BaseCompose -f $PortalOverride run --rm --no-deps gateway caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
if ($LASTEXITCODE -ne 0) { throw '恢复配置校验失败' }
docker compose --env-file $EnvFile -f $BaseCompose -f $PortalOverride up -d --no-deps --force-recreate gateway
```

### 常见现象与检查位置

| 现象 | 优先检查 |
|---|---|
| 域名找不到 | DNS 记录、终端使用的 DNS、校内网络或 VPN |
| 页面是 FileAgent 原登录页 | 新站点是否生效、域名是否写错、是否遗漏覆盖文件 |
| 502 Bad Gateway | 查询容器是否运行、是否加入相同 Docker 网络、网关内能否访问健康接口 |
| 没有样式或登录请求 404 | 是否把整个域名转发到查询服务，而不是只转发首页 |
| HTTPS 证书错误 | 证书域名、证书链、有效期和终端信任；公网自动签发则检查 DNS 与 80/443 |
| 重启后域名失效 | 是否仍带网关覆盖文件，是否恢复了镜像中的旧 Caddy 配置 |
| WorkBuddy 下载失效 | 是否保留 `/srv/downloads` 文件及其宿主机挂载 |

配置依据已核对 [Caddy 自动 HTTPS 文档](https://caddyserver.com/docs/automatic-https)、[证书配置文档](https://caddyserver.com/docs/caddyfile/directives/tls) 和 [配置校验命令文档](https://caddyserver.com/docs/command-line#caddy-validate)。本节是配置操作说明，尚未在实际服务器申请域名或执行网关变更。

## 通过本机 Python 直接启动

以下是本机实际项目路径和已存在的 Python 环境，无需新建虚拟环境，也不需要 Docker。

### 1. 安装依赖并准备配置

在 PowerShell 中执行：

```powershell
Set-Location 'E:\PycharmProject\file-agent\standalone\teacher-funding-portal'
$Python = 'D:\anaconda\envs\myenv\python.exe'
& $Python -m pip install -r .\requirements.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
if (-not (Test-Path -LiteralPath '.\.env')) {
    Copy-Item -LiteralPath '.\.env.example' -Destination '.\.env'
}
notepad .\.env
```

在 `.env` 中设置本服务管理员账号、至少 12 位的管理员密码、至少 32 字符的独立签名密钥，以及 FileAgent API 地址和有效访问令牌。访问令牌只保存在服务端，不传给教师浏览器。使用专用 FileAgent 用户的访问令牌即可，**无需创建新的数据库账号**。

后面的本机命令传入 `--fileagent-env ..\..\.env`，程序会读取 FileAgent 配置中的 `DATABASE_URL` 作为本服务数据库地址，不需要重复手填数据库账号密码，也不会打印数据库凭证。这个选项优先于本服务 `.env` 中的 `PORTAL_DATABASE_URL`。

如需独立部署、无法读取 FileAgent 的配置文件，就在本服务 `.env` 的 `PORTAL_DATABASE_URL` 中填写同一个数据库连接地址。

### 2. 将已录入的 156 位教师迁入 PostgreSQL（首次执行）

此前已维护的名单位于本目录 `data/portal.sqlite3`。执行以下命令，将旧查询服务的数据迁入 PostgreSQL 的新表：

```powershell
& $Python -B -m app.manage migrate-sqlite --file .\data\portal.sqlite3 --env-file .\.env --fileagent-env ..\..\.env
if ($LASTEXITCODE -ne 0) { throw '查询服务数据迁移失败，请检查数据库连接' }
```

命令只创建或写入 `teacher_funding_` 前缀的表，不修改 FileAgent 原表，不创建教师账号或替换密码。目标查询表已有业务记录时停止；同一数据库已经完成迁移时返回 `ALREADY_MIGRATED`。旧 SQLite 文件保持原样供核对，运行服务不再使用它。

没有旧 SQLite 文件的新部署，可跳过这一步，启动后在管理页面录入名单。也可直接将原工号表写入 PostgreSQL：

```powershell
& $Python -B -m app.manage import-roster --file 'C:\Users\zhouhexin\Downloads\工号表2026.xlsx' --env-file .\.env --fileagent-env ..\..\.env
```

本地名单维护命令只新增或更新文件中的教师，不停用未列出的教师；同一文件重复执行返回 `ALREADY_IMPORTED`。

### 3. 启动独立服务

```powershell
& $Python -B -m app.run --env-file .\.env --fileagent-env ..\..\.env --host 127.0.0.1 --port 8766
```

浏览器打开：`http://127.0.0.1:8766`。这个命令在当前窗口前台运行，按 `Ctrl+C` 停止。其他电脑需要访问时，把 `--host 127.0.0.1` 改为 `--host 0.0.0.0`，并使用本机局域网 IP 访问。端口不与 FileAgent 合并。

启动时会检查或创建所需前缀表，首次创建管理员。数据库账号需要具有在现有数据库中新建表的权限。服务不会自动把 SQLite 当作数据库连接失败时的后备。

## 可选：Docker 启动

在本服务 `.env` 中填写 `PORTAL_DATABASE_URL`，内容与 FileAgent 使用的现有数据库和账号一致。容器中 `localhost` 指容器自身；数据库在宿主机时使用宿主机可达地址和对应 PostgreSQL 端口。

```powershell
docker compose --env-file .\.env -f .\compose.yml up -d --build
```

默认端口为 `8766`，可用 `PORTAL_HOST_PORT` 修改。数据直接保存到现有 PostgreSQL，Compose 不启动新数据库，也不使用 SQLite 数据卷。正式使用建议通过 HTTPS 访问。日志可用 `docker compose -f .\compose.yml logs --tail=100` 查看。

## 教师名单维护

管理员登录后，使用“教师工号—姓名表”新增教师、修改姓名、停用或重新启用，表格显示注册情况并支持分页。工号按文本保存，前导零保留；教师自行注册并设置密码。

修改姓名或启停状态后，旧登录失效。重名教师无法自动按姓名唯一关联余额表，需要管理员核对后处理。已提供的名单中有两位“张彤”，工号分别是 `102921`、`104653`，保留独立记录，不合并金额。

网页的“可选：批量导入工号名单”仍可接收首张表包含“姓名”和“教师工号/工号/职工号”的 XLSX。这个网页批量入口按完整名单替换，未在文件中出现的教师会停用；日常修改少数教师，直接编辑网页表格即可。

## 余额表自动同步

```dotenv
PORTAL_SYNC_ENABLED=true
PORTAL_FILEAGENT_BASE_URL=http://10.102.4.241
PORTAL_FILEAGENT_ACCESS_TOKEN=填写有效的FileAgent访问令牌
PORTAL_SYNC_FILENAME=教师成果资助使用登记余额表.xlsx
PORTAL_RESEARCH_FILENAME=教师成果明细汇总（2019-2026）.xlsx
PORTAL_SYNC_INTERVAL_SECONDS=60
```

FileAgent 地址填服务根地址，不带 `/api`。本服务启动后立即检查一次，之后默认每 60 秒检查；发现正式发布的新版本后同步总额、余额和明细。下载前后校验版本和 SHA-256，校验通过后在单个数据库事务中保存并切换快照。FileAgent 无需为此修改代码。

在本机使用 Python 同时运行两个服务时，FileAgent 后端默认地址为 `http://127.0.0.1:8000`，因此本服务 `.env` 的 `PORTAL_FILEAGENT_BASE_URL` 应包含 `:8000`。只有网关实际监听 80 端口时才能省略端口。启动查询服务前先确保 FileAgent 已运行；可在 PowerShell 执行 `Invoke-RestMethod 'http://127.0.0.1:8000/api/health'` 检查。如果 FileAgent 使用了其他端口，以实际后端地址为准。修改配置后需重启查询服务，已打开的进程不会自动重新读取 `.env`。

同名文件不唯一、发布未完成、网络断开或访问令牌失效时，保留旧快照并定时重试。管理员页显示同步状态；教师页在同步失败时提示当前显示最近一次成功同步的数据。访问令牌失效后需更新 `.env` 并重启本服务。

开启同步时禁止手动覆盖余额表。需要离线导入时，设置 `PORTAL_SYNC_ENABLED=false` 并重启，即可通过管理员页上传当前 XLSX。资助金额待核对或明细合计不一致时，不返回可信余额；明细表头损坏时停止导入并保留旧数据。

## 资助总额明细与支出明细

教师登录后可以分别展开“资助总额明细”和“使用登记明细”。前者从成果总表的本人工作表读取每项资助，包括年份、成果类型、期刊/会议、论文/成果名称、论文类型、资助类型、发表/召开时间、资助类别、资助金额、地点和备注；后者展示逐笔支出。

成果总表默认完整文件名为 `教师成果明细汇总（2019-2026）.xlsx`，可通过 `PORTAL_RESEARCH_FILENAME` 修改。服务使用 FileAgent 已有的工作副本列表、元数据和下载 GET 接口，按完整文件名筛选活动文件；有多份时，按创建时间选择最新上传的一份，再检查发布状态、版本和 SHA-256。它不是余额表，因此单独同步、单独保存快照；即使余额表版本未变化，成果总表新版本也会被同步。管理员页会分别显示两种同步状态。

明细页显示成果总表来源、更新时间和资助总额。逐项合计、个人页合计、来源汇总金额或余额表科研资助总额不一致时提示核对，不自动改动余额表。只返回当前登录教师的成果，重名仍停止自动关联。其他资助目前在余额表中只有合计金额，未记录逐项来源的部分不生成额外明细。

该功能新增 `teacher_funding_research_snapshots`、`teacher_funding_research_people`、`teacher_funding_research_items` 三张表。更新代码并重启本服务会自动补建，**不需要重新执行 156 人名单迁移**。原有 `.env` 不填新字段时使用上述默认成果总表文件名；实际文件名不同请配置后重启。

### 同名成果总表有多份时，自动跟随最新上传文件

从 `20261106-release3` 起，成果总表统一按完整文件名选择，不再固定文件 ID。服务器实际文件名为《教师成果明细汇总.xlsx》时，在本服务 `.env` 中设置：

```dotenv
PORTAL_RESEARCH_FILENAME=教师成果明细汇总.xlsx
```

筛选条件是文件名逐字一致且状态为 `ACTIVE`。有多份时按 `created_at` 创建时间选择最新的一份，不按列表顺序、`updated_at` 更新时间或文件大小判断；不同时间格式先统一为 UTC 比较。创建时间缺失、没有时区或最新两份创建时间相同，则报告无法确定来源并保留上次有效数据，不任意选择。

先选择最新上传文件，再检查正式发布状态。最新一份仍在发布或需要修复时，不降级读取另一份旧表，也不提前同步候选内容；保留上一份有效快照，默认 60 秒后重试。FileAgent 返回的 `publication_status=null` 表示没有未完成的发布任务，仍须有有效当前版本，下载后还要校验 SHA-256，并再次解析来源身份；下载期间出现新上传文件、当前版本变化或发布任务时，本轮不保存快照。解析或保存失败也保留旧数据。

因此，提示词向原总表追加成果时，会跟随原文件的新版本；上传完整新版总表替换旧表时，会自动切换到新的文件 ID，待其正式发布后同步，无需每次改配置。不会合并、归档或删除其他同名文件。这里按创建时间选择来源是本服务的明确规则，不能证明文件内容在业务上一定最新，上传时应确保使用正确的完整文件名。

旧版 `PORTAL_RESEARCH_WORKING_COPY_ID` 已停用，建议从服务器 `.env` 删除；即使遗留，也不会覆盖文件名选择。此规则只调整成果总表读取，余额表原有同步与其他技能不变，不需数据库迁移或重复迁移名单。

部署时用新的 `20261106-release3` 包执行前文更新脚本，填写或确认服务器 `.env` 的完整文件名，再重建查询容器；仅修改 `.env` 或重启旧版镜像不会获得新规则。如果先更新了服务后再修改 `.env`，在服务器本服务目录执行：

```powershell
Set-Location E:\file-agent\standalone\teacher-funding-portal
docker compose -p teacher-funding-portal --env-file .\.env -f .\compose.yml -f .\compose.fileagent-server.yml up -d --no-deps --force-recreate teacher-funding-portal
if ($LASTEXITCODE -ne 0) { throw '查询服务配置加载失败' }
docker compose -p teacher-funding-portal --env-file .\.env -f .\compose.yml -f .\compose.fileagent-server.yml logs --tail=100 teacher-funding-portal
```

## 页面更新后提示元素不存在

如果出现 `Cannot read properties of null (reading 'classList')`，可能是服务尚未重启，旧版 HTML 与更新后的脚本混用了。页面已改为读取 `app/static/index.html`，脚本在 DOM 就绪后启动，并检查所需元素；发现缺失时提示更新页面，不再继续调用接口或访问空元素。

更新时请同时保留 `app/main.py`、`app/static/index.html` 和 `app/static/portal.js`，停止旧查询服务并重新启动，再在浏览器按 `Ctrl+F5` 刷新。无需重做教师名单迁移。

## 页面展示

登录后以“资助总额”和“使用与余额”两个标签页展示。前者展示科研成果资助、其他资助、总资助金额及逐项成果明细；后者展示剩余资助金额、累计支出、总资助金额及逐笔使用登记。支持点击和键盘左右方向键切换，窄屏金额卡片改为纵向排列。

登录后不显示修改密码入口，不展示来源文件名。更新时间在当前标签页顶部显示，只保留年月日；总额页使用成果明细同步时间，余额页使用余额表同步时间。后台改密接口和管理员重置密码功能仍保留。

页面样式保存在 `app/static/portal.css`，更新时应与 HTML、JS 和 `app/main.py` 一起更新。重启本服务后按 `Ctrl+F5` 刷新，无需数据库迁移。

手机宽度下，成果资助和使用登记改为逐条卡片，每个字段带中文标签，长标题自动换行，不必横向拖动宽表格。金额卡片上下排列，两个标签等宽并吸顶，按钮触摸高度至少 44px，输入框字号为 16px。适配规则覆盖 720px 及以下宽度，对 360px 及以下屏幕进一步收紧留白；电脑端仍显示表格。

手机访问本机服务时，启动命令加 `--host 0.0.0.0`，手机和电脑连接同一可互通的网络，通过 `http://电脑局域网IP:8766` 访问；不能在手机输入 `127.0.0.1` 来访问电脑。已打开的手机页面更新后请重新加载；桌面浏览器可 Ctrl+F5 刷新。

如果页面显示内容但完全没有样式，可直接打开 `http://127.0.0.1:8766/static/portal.css`：404 表示仍在使用未加载新路由的旧进程，应先重启查询服务。CSS 能打开但浏览器提示安全策略拦截时，需确认使用修正后的页面响应头，`style-src` 包含 `'self'`，允许同一服务的外部样式文件。普通刷新不能替代服务重启。

## 数据保存和维护

所有表都使用 `teacher_funding_` 前缀，包括教师名单、教师账号、导入记录、金额快照、逐笔明细、状态、审计及限流记录。FileAgent 原有账号和教师查询账号相互独立；只是数据库连接账号共用。

数据随现有 PostgreSQL 一起备份。删除旧 SQLite 文件或停止服务不会删除 PostgreSQL 中已迁移的数据。更换签名密钥会使旧登录失效；修改管理员密码应在登录后的改密页面操作，环境变量中的初始化密码不会覆盖现有管理员密码。

## 开发检查

```powershell
& $Python -m pip install -r .\requirements-test.txt
& $Python -B -m unittest discover -s tests -v
```

API 回归使用临时 SQLite 测试库；PostgreSQL 专用检查验证表名前缀、ID 返回、DDL 和 SQL 转换，不会访问生产数据。页面检查可用 `node tests/test_portal_ui.cjs`；Node.js 仅用于开发检查，不是运行依赖。

用户已执行迁移并返回 `MIGRATED`，156 位教师已写入当时连接的 PostgreSQL。部署时连接同一数据库即可复用；若数据库地址不同，需单独迁移名单或维护数据，不把复制代码等同于复制数据库。
