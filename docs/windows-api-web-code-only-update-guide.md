# Windows Docker 前后端代码离线更新手册

> 最近更新：2026-09-05
> 适用范围：API Python 代码、前端 React 代码、规则、taxonomy、Alembic 迁移和部署脚本发生变化，但
> Python requirements、Linux 系统包和本地模型没有变化。
> 目标：只传输源码 ZIP，复用目标服务器已有的 API 运行时基础镜像和 Web/Caddy 镜像，不传输 27GB
> 完整镜像 TAR，不在目标服务器下载依赖或模型。

## 1. 更新原理与边界

生产 Compose 没有把宿主机源码挂载到容器的 `/app`。因此，仅把 ZIP 解压到 `E:\file-agent` 后重启
容器不会加载新代码。完整流程必须是：

```text
构建机编译前端 dist
-> 打包完整源码 ZIP
-> 复制 ZIP 和 SHA-256 到目标服务器
-> 修改生产 .env 的新代码标签与旧 Web 种子标签
-> 同步源码
-> 复用 file-agent-api-runtime-base 构建 API 代码层
-> 复用旧 file-agent-web 镜像中的 Caddy 构建 Web 静态文件层
-> 重新创建 migrate、API、gateway 和全部 worker
-> 健康检查与运行时校验
```

以下变化可以使用本手册：

- `apps/api/app/` Python 业务代码变化。
- `apps/web/` React、TypeScript、CSS 变化。
- `rules/`、`skills/`、taxonomy JSON 变化。
- Alembic 迁移变化；更新前必须备份数据库。
- `deploy/` 中不涉及系统依赖和模型的脚本或 Compose 变化。

以下变化不能只使用代码 ZIP：

- `requirements*.txt` 或 API Python 依赖发生变化。
- Docker 基础系统包、LibreOffice、Node 运行时或 Caddy 运行时版本发生变化。
- PaddleOCR、PP-StructureV3、PaddleOCR-VL、Docling 或 embedding 模型发生变化。
- `Dockerfile.api-base`、`preload_models.py` 等基础镜像构建逻辑发生变化。

遇到这些情况必须构建新的 `file-agent-api-runtime-base`，并按完整离线镜像流程导出和导入。

## 2. 本次需要填写的参数

下表示例把代码版本从旧版本更新到 `20260905-code5`。实际执行时只修改右侧值：

| 参数 | 示例值 | 说明 |
|---|---|---|
| `$ProjectRoot` | `E:\PycharmProject\file-agent` | 构建机源码根目录 |
| `$Version` | `20260905-code5` | 新代码版本标签；每次更新建议使用新标签 |
| `$PackageZip`（构建机） | `E:\file-agent-code-20260905-code5.zip` | 生成的源码 ZIP |
| `$DeployRoot` | `E:\file-agent` | 目标服务器生产部署根目录 |
| `$PackageZip`（服务器） | `E:\file-agent-code-20260905-code5.zip` | ZIP 在服务器上的实际位置 |
| `FILE_AGENT_IMAGE_TAG` | `20260905-code5` | API 和 Web 的新代码镜像标签 |
| `FILE_AGENT_BASE_IMAGE_TAG` | `20260904-v1` | 已存在的 API 基础镜像标签；代码更新时保持不变 |
| `FILE_AGENT_WEB_SEED_IMAGE` | `file-agent-web:20260904-code2` | 服务器上确实存在的旧 Web 镜像，只复用其中的 Caddy |
| `VITE_API_BASE_URL` | `/api` | 生产前端固定使用同源 API，不填写服务器 IP 或 `:8000` |

`FILE_AGENT_WEB_SEED_IMAGE` 不能填写尚未构建的新标签。先用下面的命令查看服务器实际已有标签：

```powershell
docker images file-agent-web --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
```

例如输出只有 `file-agent-web:20260904-code2`，就填写：

```dotenv
FILE_AGENT_WEB_SEED_IMAGE=file-agent-web:20260904-code2
```

## 3. 构建机：构建前端

在开发机 PowerShell 执行：

```powershell
$ProjectRoot = "E:\PycharmProject\file-agent"
$Version = "20260905-code5"
$PackageZip = "E:\file-agent-code-$Version.zip"

Set-Location "$ProjectRoot\apps\web"

$PreviousApiBaseUrl = $env:VITE_API_BASE_URL
try {
    $env:VITE_API_BASE_URL = "/api"
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "前端构建失败。"
    }
} finally {
    $env:VITE_API_BASE_URL = $PreviousApiBaseUrl
}

if (-not (Test-Path -LiteralPath ".\dist\index.html")) {
    throw "前端构建结果 apps/web/dist/index.html 不存在。"
}

Set-Location $ProjectRoot
```

如果 `package.json` 和锁文件没有变化，不需要重新执行 `npm install`。如果前端依赖确实变化，应在构建机
完成依赖安装和构建；目标服务器仍然只接收编译后的 `dist`，不需要 `node_modules`。

## 4. 构建机：生成源码 ZIP

继续在项目根目录执行。ZIP 必须包含完整源码和 `apps/web/dist/`，但不得包含生产 `.env`、上传数据、
日志、模型缓存或 `node_modules`：

```powershell
if (Test-Path -LiteralPath $PackageZip) {
    Remove-Item -LiteralPath $PackageZip -Force
}

tar.exe -a -c -f $PackageZip `
    --exclude=apps/web/node_modules `
    --exclude=apps/api/.pytest_cache `
    --exclude=apps/api/storage `
    --exclude=apps/api/logs `
    --exclude=apps/api/.env `
    --exclude=apps/web/.env `
    --exclude=deploy/.env `
    --exclude=.env `
    --exclude=__pycache__ `
    --exclude=*.pyc `
    apps deploy docs rules skills README.md .dockerignore

if ($LASTEXITCODE -ne 0) {
    throw "源码 ZIP 创建失败。"
}
```

不要只压缩本次改动的几个文件。更新脚本会以 ZIP 中的完整 `apps/`、`deploy/`、`docs/`、`rules/` 和 `skills/`
作为一个代码版本同步，完整源码才能正确处理新增、删除、重命名和跨模块依赖。

## 5. 构建机：校验内容并生成 SHA-256

```powershell
$Entries = tar.exe -tf $PackageZip
if ($LASTEXITCODE -ne 0) {
    throw "无法读取源码 ZIP。"
}

$RequiredEntries = @(
    "apps/web/dist/index.html",
    "deploy/update.ps1",
    "deploy/build-layered-images.ps1",
    "deploy/Dockerfile.api",
    "deploy/Dockerfile.web-reuse"
)

foreach ($RequiredEntry in $RequiredEntries) {
    if (-not ($Entries | Where-Object { $_.TrimEnd("/") -eq $RequiredEntry })) {
        throw "源码 ZIP 缺少：$RequiredEntry"
    }
}

$ForbiddenEntries = $Entries | Where-Object {
    $_ -match '(^|/)deploy/\.env$' -or
    $_ -match '(^|/)node_modules/' -or
    $_ -match '(^|/)data/'
}
if ($ForbiddenEntries) {
    $ForbiddenEntries
    throw "源码 ZIP 包含禁止发布的配置或运行数据。"
}

$Hash = Get-FileHash -LiteralPath $PackageZip -Algorithm SHA256
$ChecksumPath = "$PackageZip.sha256"
"$($Hash.Hash.ToLowerInvariant())  $([System.IO.Path]::GetFileName($PackageZip))" |
    Set-Content -LiteralPath $ChecksumPath -Encoding ASCII

Get-Item -LiteralPath $PackageZip, $ChecksumPath
$Hash
```

把以下两个文件一起复制到目标服务器：

```text
file-agent-code-20260905-code5.zip
file-agent-code-20260905-code5.zip.sha256
```

源码 ZIP 不包含 `deploy/.env`，因此不能从构建机把密码、LLM Key 或数据库配置带到生产服务器。

## 6. 目标服务器：进入正确目录并校验环境

必须在生产部署根目录 `E:\file-agent` 执行，不要在
`E:\PycharmProject\file-agent` 源码开发目录执行：

```powershell
$DeployRoot = "E:\file-agent"
$Version = "20260905-code5"
$PackageZip = "E:\file-agent-code-$Version.zip"
$ChecksumPath = "$PackageZip.sha256"

Set-Location $DeployRoot

if (-not (Test-Path -LiteralPath ".\deploy\.env")) {
    throw "未找到生产配置：$DeployRoot\deploy\.env；请确认当前目录是已经部署过的生产目录。"
}
if (-not (Test-Path -LiteralPath $PackageZip)) {
    throw "未找到源码 ZIP：$PackageZip"
}
if (-not (Test-Path -LiteralPath $ChecksumPath)) {
    throw "未找到 SHA-256 文件：$ChecksumPath"
}

docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop 未启动或无法连接。"
}
```

`update.ps1` 报“请先运行 deploy.ps1”通常不是要求在开发目录新建一套部署，而是说明当前目录不是
`E:\file-agent`，或者生产 `deploy/.env` 没有复制到正确位置。

## 7. 目标服务器：验证 SHA-256 和 ZIP 内容

```powershell
$ExpectedHash = ((Get-Content -LiteralPath $ChecksumPath -Encoding ASCII | Select-Object -First 1) -split '\s+')[0]
$ActualHash = (Get-FileHash -LiteralPath $PackageZip -Algorithm SHA256).Hash
if ($ActualHash -ine $ExpectedHash) {
    throw "源码 ZIP SHA-256 校验失败。"
}

$Entries = tar.exe -tf $PackageZip
if (-not ($Entries | Where-Object { $_.TrimEnd("/") -eq "apps/web/dist/index.html" })) {
    throw "源码 ZIP 缺少 apps/web/dist/index.html；必须在构建机重新执行 npm run build 并打包。"
}

Write-Host "源码 ZIP SHA-256 和前端 dist 校验通过。" -ForegroundColor Green
```

## 8. 目标服务器：备份数据库、上传文件和生产配置

更新会执行 Alembic，因此即使本次迁移内容看似很小，也必须先备份：

```powershell
$EnvBackup = ".\deploy\.env.before-$Version"
Copy-Item -LiteralPath ".\deploy\.env" -Destination $EnvBackup -Force

.\deploy\backup.ps1 -IncludeUploads
```

确认 `E:\file-agent\data\backups\` 中已生成新的 SQL 和 uploads ZIP。备份完成后再继续。

## 9. 目标服务器：确认本地镜像并修改 `.env`

先查看当前镜像：

```powershell
docker images --format "{{.Repository}}:{{.Tag}}  {{.Size}}" |
    Select-String "file-agent-api-runtime-base|file-agent-api-full-cpu|file-agent-web|pgvector|neo4j"
```

必须至少具有：

```text
file-agent-api-runtime-base:20260904-v1
某个已有的 file-agent-web:<旧标签>
pgvector/pgvector:pg16
neo4j:5.26-community
```

打开生产配置：

```powershell
notepad ".\deploy\.env"
```

只调整以下三个参数，其他数据库密码、Neo4j 密码、JWT、LLM、OCR 和受管目录配置保持原值：

```dotenv
FILE_AGENT_IMAGE_TAG=20260905-code5
FILE_AGENT_BASE_IMAGE_TAG=20260904-v1
FILE_AGENT_WEB_SEED_IMAGE=file-agent-web:20260904-code2
```

修改规则：

- `FILE_AGENT_IMAGE_TAG`：改为本次 `$Version`。
- `FILE_AGENT_BASE_IMAGE_TAG`：代码更新时保持当前已经导入的基础镜像标签，不要改成代码标签。
- `FILE_AGENT_WEB_SEED_IMAGE`：填写 `docker images file-agent-web` 输出中确实存在的旧 Web 镜像完整名称；
  不要填写尚未生成的 `file-agent-web:20260905-code5`。
- 不要修改 `POSTGRES_PASSWORD`、`NEO4J_PASSWORD` 或 `JWT_SECRET_KEY`。
- 如果新版 `.env.production.example` 新增了必填项，应把该项人工合并到生产 `.env`，不能用示例文件整体
  覆盖生产配置。

保存后验证标签，不打印密钥：

```powershell
Select-String -LiteralPath ".\deploy\.env" `
    -Pattern "^FILE_AGENT_IMAGE_TAG=|^FILE_AGENT_BASE_IMAGE_TAG=|^FILE_AGENT_WEB_SEED_IMAGE="

docker compose `
    --env-file ".\deploy\.env" `
    -f ".\deploy\docker-compose.production.yml" `
    config --quiet
```

## 10. 目标服务器：首次更新脚本引导

如果目标服务器上的旧 `update.ps1` 不认识 `-SkipInfrastructurePull` 或 `-UsePrebuiltWebDist`，先从已
校验的 ZIP 中只提取新版更新入口：

```powershell
tar.exe -xf $PackageZip -C $DeployRoot deploy/update.ps1
if ($LASTEXITCODE -ne 0) {
    throw "新版 update.ps1 提取失败。"
}
```

该步骤不会提取或覆盖 `deploy/.env`。正式更新时，新版 `update.ps1` 会同步 ZIP 中其余部署脚本，并在
同步期间备份和恢复生产 `.env`。

## 11. 目标服务器：执行前后端更新

执行唯一正式更新命令：

```powershell
Set-Location $DeployRoot

.\deploy\update.ps1 `
    -PackageZip $PackageZip `
    -SkipInfrastructurePull `
    -UsePrebuiltWebDist
```

参数含义：

| 参数 | 必须 | 作用 |
|---|---:|---|
| `-PackageZip` | 是 | 同步完整代码 ZIP，同时保留生产 `deploy/.env` 和 `data/` |
| `-SkipInfrastructurePull` | 离线更新必须 | 不访问镜像仓库，不拉取 PostgreSQL/Neo4j |
| `-UsePrebuiltWebDist` | 前端更新必须 | 使用 ZIP 中的 `apps/web/dist`，复用本地旧 Web/Caddy 镜像 |
| `-SkipWeb` | 否 | 只更新后端时使用；本流程前端也更新，因此禁止使用 |
| `-UsePrebuiltImages` | 禁止 | 会跳过源码代码层构建，导致新源码不进入容器 |

脚本会自动执行：

1. 保存生产 `deploy/.env`。
2. 同步完整源码，保留 `data/`。
3. 复用 `file-agent-api-runtime-base:<BaseImageTag>` 构建
   `file-agent-api-full-cpu:<ImageTag>`。
4. 复用 `FILE_AGENT_WEB_SEED_IMAGE` 中的 Caddy，将 `apps/web/dist` 构建为
   `file-agent-web:<ImageTag>`。
5. 删除并重新创建一次性 `migrate` 容器。
6. 执行 Alembic 迁移并重新创建 API、gateway、scheduler、watcher 和全部 worker。
7. 等待 API 健康检查。
8. 执行 `/app/deploy/scripts/verify_runtime.py --managed-root`。

不需要在成功后再运行 `deploy.ps1` 或手工 `docker compose restart`。

## 12. 更新成功后的验收

### 12.1 检查镜像

```powershell
docker images --format "{{.Repository}}:{{.Tag}}  {{.Size}}" |
    Select-String "file-agent-api-full-cpu:$Version|file-agent-web:$Version"
```

必须同时看到：

```text
file-agent-api-full-cpu:20260905-code5
file-agent-web:20260905-code5
```

### 12.2 检查容器

```powershell
docker compose `
    --env-file ".\deploy\.env" `
    -f ".\deploy\docker-compose.production.yml" `
    ps -a
```

`migrate` 显示 `Exited (0)` 正常；API、gateway 和 worker 应为 `Up`，有健康检查的服务应为
`healthy`。

### 12.3 检查 API 和网关日志

```powershell
docker compose `
    --env-file ".\deploy\.env" `
    -f ".\deploy\docker-compose.production.yml" `
    logs --no-color --tail 150 api gateway migrate
```

### 12.4 再次执行运行时校验

```powershell
docker compose `
    --env-file ".\deploy\.env" `
    -f ".\deploy\docker-compose.production.yml" `
    exec -T api python /app/deploy/scripts/verify_runtime.py --managed-root
```

### 12.5 浏览器验证

局域网地址保持：

```text
http://10.102.8.176/
```

浏览器不要访问 Vite 的 `:5173` 或直接请求 API 的 `:8000`。如果页面仍显示旧前端，先强制刷新
`Ctrl+F5`，再检查 gateway 使用的镜像标签。

## 13. 常见错误

### 13.1 `请先运行 deploy.ps1`

原因通常是命令在 `E:\PycharmProject\file-agent` 执行，或者生产 `deploy/.env` 不存在。切换到：

```powershell
Set-Location E:\file-agent
Test-Path ".\deploy\.env"
```

已经成功部署过的服务器不应在错误的源码目录再初始化第二套部署。

### 13.2 `The reusable Web runtime image does not exist: file-agent-web:<新标签>`

说明 `FILE_AGENT_WEB_SEED_IMAGE` 为空或错误地填写成新标签。查找旧镜像：

```powershell
docker images file-agent-web --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
```

把真实存在的旧标签写入 `.env`，例如：

```dotenv
FILE_AGENT_WEB_SEED_IMAGE=file-agent-web:20260904-code2
```

然后重新执行第 11 节命令。已经成功构建的 API 代码层会命中缓存，不需要重新传 ZIP。

新版构建脚本在未显式填写种子且新标签尚不存在时，也会自动选择本机已有的最新
`file-agent-web:*`；生产环境仍建议显式填写，以便审计和复现。

### 13.3 `source package does not contain the prebuilt Web dist directory`

源码 ZIP 缺少 `apps/web/dist/index.html`。回到构建机重新执行第 3～5 节；不要在目标服务器运行
`npm install` 或联网构建。

### 13.4 API 构建显示 `CACHED`

BuildKit 对未变化层显示 `CACHED` 属于正常行为。应结合新镜像标签、源码 ZIP SHA-256 和最终容器镜像
检查是否使用正确版本。如果本次 API 源码确实变化但构建上下文完全没有变化，应检查打包时是否使用了
正确的 `$ProjectRoot`。

### 13.5 前端更新后页面仍旧

依次检查：

```powershell
tar.exe -tf $PackageZip | Select-String "apps/web/dist/index.html"
docker images file-agent-web --format "{{.Repository}}:{{.Tag}}  {{.CreatedAt}}"
docker inspect file-agent-gateway-1 --format '{{.Config.Image}} {{.Image}}'
```

确认 gateway 使用新标签后，在浏览器执行 `Ctrl+F5`。

## 14. 失败与回滚边界

- 如果失败发生在构建阶段、尚未执行 Compose 重建，旧容器通常仍在运行；修正参数后直接重跑更新命令。
- 不要删除 PostgreSQL、Neo4j 或 uploads 数据卷。
- 如果需要恢复更新前标签，先恢复 `.env` 备份，再使用匹配的旧源码和旧镜像启动。
- 如果新版 Alembic 已执行不兼容迁移，不能只切换旧镜像；必须恢复更新前 PostgreSQL SQL 与 uploads
  配套备份，并在维护窗口执行。
- 不要执行 `docker system prune -a`，它可能删除仍用于快速更新和回滚的基础镜像及旧 Web 种子镜像。

生产 `.env` 回退示例：

```powershell
Copy-Item -LiteralPath ".\deploy\.env.before-$Version" `
    -Destination ".\deploy\.env" `
    -Force
```

恢复后应先核对 `FILE_AGENT_IMAGE_TAG`、`FILE_AGENT_BASE_IMAGE_TAG` 和数据库备份时间，再决定是否启动；
不要在不确认数据库迁移兼容性的情况下直接回滚生产代码。
