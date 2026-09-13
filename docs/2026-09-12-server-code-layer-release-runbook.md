# File Agent 服务器代码层发布操作手册

发布日期：2026-09-12
建议发布标签：`20260912-code1`
适用对象：已部署 File Agent，且已具备 `file-agent-api-runtime-base:20260904-v1` 与可复用 `file-agent-web:<旧标签>` 的 Windows Docker 服务器。

## 1. 本次发布边界

本手册只重建 API 与 Web 的**代码层镜像**，不重建 Python 依赖、Linux 系统包、LibreOffice、OCR/Embedding 模型或 PostgreSQL/Neo4j 基础设施镜像。

本次发布会：

- 使用完整代码 ZIP 同步 `apps/`、`deploy/`、`docs/`、`rules/`、`skills/`；
- 使用现有 `file-agent-api-runtime-base:20260904-v1` 构建新的 `file-agent-api-full-cpu:20260912-code1`；
- 使用 ZIP 中已编译的 `apps/web/dist` 与旧 Web 镜像中的 Caddy 构建新的 `file-agent-web:20260912-code1`；
- 重新创建一次性 `migrate` 容器，执行 Alembic 升级，再重建 API、gateway、scheduler、watcher 和 workers；
- 保留生产 `deploy/.env`、数据库卷、`data/uploads`、`data/logs` 与受管目录。

不包含 WorkBuddy 本地连接器/插件的安装或升级；如本次需要更新该客户端，应另行按其交付文档在 WorkBuddy 所在电脑安装。

## 2. 发布前提与禁止事项

发布前确认：

1. 服务器 Docker Desktop/Engine 正常运行，且未暂停。
2. 当前目录是已部署的生产目录，例如 `E:\file-agent`，其中存在 `deploy\.env`。
3. 本机已有 `file-agent-api-runtime-base:20260904-v1`、一个旧 `file-agent-web:<tag>`、`pgvector/pgvector:pg16` 和 `neo4j:5.26-community`。
4. 已完成数据库与上传文件备份。
5. 发布包经 SHA-256 校验，且不含 `.env`、`data/`、`storage/`、日志、模型缓存、`node_modules` 或旧 ZIP。

禁止：

- 不要执行 `-RebuildBase`；
- 不要使用 `-UsePrebuiltImages`，它会跳过新代码镜像构建；
- 不要使用 `-SkipWeb`，本次前端也要更新；
- 不要用开发机的 `.env` 覆盖服务器 `deploy/.env`；
- 不要只压缩本次改动文件。`update.ps1` 按顶层目录同步，增量小包可能删掉服务器上未包含的代码。

## 3. 构建机：生成前端和完整代码包

在源代码根目录执行。若本次标签已存在，先人工确认该同名 ZIP 可以被覆盖。

```powershell
$ProjectRoot = "E:\PycharmProject\file-agent"
$Version = "20260912-code1"
$PackageZip = "E:\file-agent-code-$Version.zip"

Set-Location "$ProjectRoot\apps\web"
$PreviousApiBaseUrl = $env:VITE_API_BASE_URL
try {
    $env:VITE_API_BASE_URL = "/api"
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "前端构建失败。" }
} finally {
    $env:VITE_API_BASE_URL = $PreviousApiBaseUrl
}

if (-not (Test-Path -LiteralPath ".\dist\index.html")) {
    throw "未生成 apps/web/dist/index.html。"
}

Set-Location $ProjectRoot
.\scripts\package-code-release.ps1 -Version $Version
```

将 ZIP 和同名 `.sha256` 文件复制到服务器，例如 `E:\` 根目录。

## 4. 服务器：发布前检查与备份

在服务器 PowerShell 中执行。以下命令不输出数据库、JWT 或模型密钥。

```powershell
$DeployRoot = "E:\file-agent"
$Version = "20260912-code1"
$PackageZip = "E:\file-agent-code-$Version.zip"
$ChecksumPath = "$PackageZip.sha256"

Set-Location $DeployRoot
if (-not (Test-Path -LiteralPath ".\deploy\.env")) { throw "未找到生产 deploy/.env。" }
if (-not (Test-Path -LiteralPath $PackageZip)) { throw "未找到代码包。" }
if (-not (Test-Path -LiteralPath $ChecksumPath)) { throw "未找到校验文件。" }

docker info | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker 未启动、不可访问或处于暂停状态。" }

docker image inspect "file-agent-api-runtime-base:20260904-v1" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "缺少 API 基础镜像。" }

docker images "file-agent-web" --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
Copy-Item -LiteralPath ".\deploy\.env" -Destination ".\deploy\.env.before-$Version" -Force
.\deploy\backup.ps1 -IncludeUploads
```

确认 `data\backups` 产生新的备份文件后继续。

校验包并在 `deploy/.env` 中只更新下列三项。第三项必须填写上一条 `docker images file-agent-web` 输出中真实存在的镜像名称。

```powershell
$ExpectedHash = ((Get-Content -LiteralPath $ChecksumPath -Encoding ASCII | Select-Object -First 1) -split '\s+')[0]
$ActualHash = (Get-FileHash -LiteralPath $PackageZip -Algorithm SHA256).Hash
if ($ActualHash -ine $ExpectedHash) { throw "SHA-256 校验失败。" }

notepad ".\deploy\.env"
```

```dotenv
FILE_AGENT_IMAGE_TAG=20260912-code1
FILE_AGENT_BASE_IMAGE_TAG=20260904-v1
FILE_AGENT_WEB_SEED_IMAGE=file-agent-web:<服务器上已有的旧标签>
```

不要改动数据库密码、Neo4j 密码、JWT、LLM、OCR 或受管目录相关配置。

## 5. 服务器：构建代码镜像并重建服务

执行唯一正式发布命令：

```powershell
Set-Location $DeployRoot
.\deploy\update.ps1 `
    -PackageZip $PackageZip `
    -SkipInfrastructurePull `
    -UsePrebuiltWebDist
```

该命令会保留 `deploy/.env` 和 `data/`，同步完整代码包，构建新 API/Web 代码镜像，重新执行迁移，并重建服务。发布期间 API 与 worker 会有短暂不可用窗口；避免同时触发导入、移动或分类任务。

## 6. 发布后验收

```powershell
Set-Location $DeployRoot
docker compose --env-file ".\deploy\.env" -f ".\deploy\docker-compose.production.yml" ps
docker images --format "{{.Repository}}:{{.Tag}}  {{.Size}}" |
    Select-String "file-agent-api-full-cpu:20260912-code1|file-agent-web:20260912-code1"
curl.exe http://127.0.0.1/api/health
docker compose --env-file ".\deploy\.env" -f ".\deploy\docker-compose.production.yml" logs --tail=200 migrate api scheduler watcher
```

验收标准：

- `migrate` 为成功结束状态；
- API 为 `healthy`；
- 所有 worker、scheduler、watcher 为运行状态；
- `/api/health` 返回成功；
- 日志中没有 migration、模型校验或受管目录挂载错误；
- 使用一份非生产测试文件验证：导入、解析、分类、工作副本落位、MCP 搜索/预览（如本次同时发布 MCP 服务端接口）。

## 7. 异常处理与回退

若构建失败，旧容器未被替换时先保留现场，收集 `docker compose ... logs`，不要清空卷。

若新镜像已构建但服务验收失败，可将 `deploy/.env` 的 `FILE_AGENT_IMAGE_TAG` 改回发布前的代码镜像标签，再执行：

```powershell
Set-Location $DeployRoot
docker compose --env-file ".\deploy\.env" -f ".\deploy\docker-compose.production.yml" up -d --no-build
```

如果 Alembic 已经执行了新迁移，不要直接回退数据库；先停止在应用层代码标签回退，保留数据库迁移状态并根据迁移内容制定恢复方案。

## 8. 本次环境核对结果

开发工作机上的 Docker Desktop 当前处于暂停状态，且未配置生产 `deploy/.env`，不能在该机器安全地代替服务器执行服务重建。本手册及代码包可在目标服务器完成上述步骤。
