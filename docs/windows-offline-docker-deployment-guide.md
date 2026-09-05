# File Agent Windows 离线 Docker 完整部署指南

## 1. 文档目标

本文用于把已经构建完成的 File Agent 全功能 CPU 镜像，从构建机部署到新的 Windows 服务器。目标服务器
可以不安装 Python、Node.js、npm、LibreOffice 或模型工具，也不需要从 Docker Hub、PyPI、npm 或模型站点
下载依赖。

本文对应当前发布标签：

| 镜像 | 标签 | 当前本机显示大小 |
|---|---|---:|
| API 运行时基础镜像 | `file-agent-api-runtime-base:20260904-v1` | 26.9 GB |
| API 最新代码镜像 | `file-agent-api-full-cpu:20260904-code2` | 27 GB |
| Web 最新代码镜像 | `file-agent-web:20260904-code2` | 86.8 MB |
| PostgreSQL/pgvector | `pgvector/pgvector:pg16` | 以本地镜像为准 |
| Neo4j | `neo4j:5.26-community` | 以本地镜像为准 |

API 代码镜像和运行时基础镜像共享 Docker 层，实际归档不会简单地按两个镜像显示大小相加。依赖或模型
不变时，后续只需要更换代码标签，不需要重建运行时基础镜像。

## 2. 部署架构

```text
用户浏览器
    |
    | HTTP 80 / HTTPS 443
    v
Caddy gateway
    |
    | Docker 内网 /api -> api:8000
    v
FastAPI API
    |---------------- PostgreSQL/pgvector（业务事实源）
    |---------------- Neo4j（可重建图投影）
    |---------------- scheduler / watcher / 多个 filesystem worker
    |---------------- /data/uploads（上传、派生件、工作副本）
    `---------------- /managed/workdata（Windows 受管目录，默认只读）
```

只有 Caddy 的 80/443 发布到宿主机。PostgreSQL 5432、Neo4j 7474/7687 和 API 8000 不应向局域网或
公网开放。

## 3. 目标服务器要求

### 3.1 硬件和磁盘

- Windows 11 或兼容的 Windows Docker Desktop 主机。
- 宿主机建议至少 6 核 CPU、32 GB 内存。
- Docker Desktop 至少分配 4 核 CPU、20 GB 内存，建议 5 核 CPU、24 GB 内存。
- 建议为 Docker 镜像、命名卷、上传文件和导入临时空间预留至少 120 GB，长期使用建议 150 GB 以上。
- 在 BIOS/UEFI 中启用虚拟化，并启用 WSL 2。

Docker Desktop 默认把 Linux 镜像保存在当前 Windows 用户的 WSL 虚拟磁盘中，通常位于：

```text
C:\Users\<用户名>\AppData\Local\Docker\wsl\disk\docker_data.vhdx
```

不要复制该 VHDX 作为应用部署包，应使用 `docker save`/`docker load`。

### 3.2 软件和网络

- 安装并启动 Docker Desktop，切换到 Linux containers。
- 目标服务器使用预构建镜像时，不需要 Python、Conda、Node.js、npm 或 Git。
- 局域网访问需要允许 TCP 80；正式 HTTPS 部署需要允许 TCP 80 和 443。
- 如果启用外部 OpenAI-compatible LLM，服务器必须能访问配置的 `LLM_BASE_URL`；该地址可以是校内或
  内网模型服务，不要求访问公共互联网。

检查 Docker：

```powershell
docker version
docker info --format "CPU={{.NCPU}} Memory={{.MemTotal}}"
```

### 3.3 Docker Desktop 离线安装和重启恢复

应用交付包不包含 Docker Desktop 安装程序或 WSL 组件。目标服务器不能联网时，应提前从组织批准的官方
渠道准备与 Windows 版本匹配的 Docker Desktop 安装程序、WSL 2 组件及其校验值。安装后至少完成一次：

1. 重启 Windows，启动 Docker Desktop 并确认使用 Linux containers。
2. 在 Docker Desktop 设置中分配建议的 CPU、内存和磁盘空间。
3. 启用“登录 Windows 时启动 Docker Desktop”，并确认运行服务的 Windows 账号允许登录和访问 E 盘。
4. 校准 Windows 时间和时区；JWT、HTTPS 证书和日志时间都依赖正确的系统时间。
5. 实际重启服务器一次，登录运行账号后确认 `docker info` 成功，再进行应用部署。

Compose 的 `restart: unless-stopped` 只能在 Docker 引擎启动后恢复容器。当前方案基于 Docker Desktop，
并不等同于无需用户会话的原生 Windows 服务；用于无人值守服务器前必须验证组织实际采用的登录和
Docker Desktop 启动策略。

## 4. 构建机制作离线交付包

以下命令在已经构建好镜像的机器 `E:\PycharmProject\file-agent` 中执行。

### 4.1 核对五个镜像

```powershell
Set-Location E:\PycharmProject\file-agent

$RequiredImages = @(
  "file-agent-api-runtime-base:20260904-v1",
  "file-agent-api-full-cpu:20260904-code2",
  "file-agent-web:20260904-code2",
  "pgvector/pgvector:pg16",
  "neo4j:5.26-community"
)

foreach ($Image in $RequiredImages) {
  docker image inspect $Image | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "本机缺少镜像：$Image" }
}

docker images --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
```

数据库镜像也必须进入离线包。只导出三个 File Agent 镜像会导致目标服务器使用 `--pull never` 时无法启动
PostgreSQL 或 Neo4j。

### 4.2 不联网直接导出当前镜像

当前镜像已经完整构建时，推荐直接执行 `docker save`，该过程不会访问镜像仓库：

```powershell
$ReleaseRoot = "E:\file-agent-release-20260904-code2"
New-Item -ItemType Directory -Force $ReleaseRoot | Out-Null

$ArchivePath = Join-Path $ReleaseRoot "file-agent-full-cpu-20260904-code2.tar"
docker save --output $ArchivePath `
  "file-agent-api-runtime-base:20260904-v1" `
  "file-agent-api-full-cpu:20260904-code2" `
  "file-agent-web:20260904-code2" `
  "pgvector/pgvector:pg16" `
  "neo4j:5.26-community"

if ($LASTEXITCODE -ne 0) { throw "Docker 镜像导出失败。" }

$Hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
$ArchiveName = [System.IO.Path]::GetFileName($ArchivePath)
Set-Content -LiteralPath "$ArchivePath.sha256" -Value "$Hash  $ArchiveName" -Encoding ASCII
Write-Host "归档：$ArchivePath"
Write-Host "SHA-256：$Hash"
```

导出期间不要退出 Docker Desktop，不要执行 `docker system prune`。归档可能很大，应确保输出磁盘空间充足。

如果构建机网络正常并希望由项目脚本拉取/核对数据库镜像后自动生成清单，也可以执行：

```powershell
.\deploy\export-offline-images.ps1 `
  -ImageTag "20260904-code2" `
  -BaseImageTag "20260904-v1" `
  -SeedApiImage "file-agent-api-full-cpu:20260826" `
  -OutputDirectory "E:\file-agent-release-20260904-code2\images"
```

该脚本会访问 PostgreSQL/Neo4j 镜像仓库；网络受限时使用前面的直接 `docker save` 方法。

### 4.3 制作部署源码包

镜像中已经包含当前 API/Web 代码，但目标服务器仍需要 Compose、部署脚本和环境模板。不要把构建机的
`deploy/.env`、上传文件、日志、数据库数据或模型缓存复制到交付源码包。

可以使用一个新的暂存目录：

```powershell
Set-Location E:\PycharmProject\file-agent

$ReleaseRoot = "E:\file-agent-release-20260904-code2"
$ProjectStage = Join-Path $ReleaseRoot "project"
New-Item -ItemType Directory -Force $ProjectStage | Out-Null

robocopy . $ProjectStage /E `
  /XD .git .idea .vscode .venv venv node_modules data storage logs release `
      file-agent-offline-images .docker-temp `
  /XF .env *.pyc *.log *.tar *.sha256

if ($LASTEXITCODE -ge 8) { throw "源码交付目录复制失败，robocopy=$LASTEXITCODE" }

$SourceZip = Join-Path $ReleaseRoot "file-agent-source-20260904-code2.zip"
tar.exe -a -c -f $SourceZip -C $ProjectStage .
if ($LASTEXITCODE -ne 0) { throw "源码 ZIP 创建失败。" }
Get-FileHash -LiteralPath $SourceZip -Algorithm SHA256
```

这里使用 Windows 自带的 `tar.exe` 创建 ZIP，确保 `.dockerignore` 等隐藏构建文件也被保留。

确认暂存目录中不存在真实环境文件：

```powershell
Get-ChildItem $ProjectStage -Recurse -Force -File -Filter ".env"
```

命令应无输出。`.env.production.example` 是不含真实密码的模板，应保留。

还应确认 ZIP 包含隐藏的 `.dockerignore`，否则后续从源码重建时可能把不应进入上下文的文件发送给
Docker：

```powershell
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Zip = [System.IO.Compression.ZipFile]::OpenRead($SourceZip)
try {
  $Names = @($Zip.Entries | ForEach-Object {
    $Name = $_.FullName.Replace("\", "/")
    if ($Name.StartsWith("./")) { $Name = $Name.Substring(2) }
    $Name.TrimEnd("/")
  })
  if (".dockerignore" -notin $Names) { throw "源码 ZIP 缺少 .dockerignore" }
  if (@($Names | Where-Object { $_ -match '(^|/)\.env$' }).Count -ne 0) {
    throw "源码 ZIP 包含真实 .env"
  }
} finally {
  $Zip.Dispose()
}
```

### 4.4 最终交付内容

复制到移动硬盘或受控文件共享的内容至少包括：

```text
file-agent-release-20260904-code2/
├─ file-agent-full-cpu-20260904-code2.tar
├─ file-agent-full-cpu-20260904-code2.tar.sha256
└─ file-agent-source-20260904-code2.zip
```

如果使用项目导出脚本，还会包含 `offline-image-manifest.json`。传输完成后应再次核对 SHA-256。

## 5. 新 Windows 服务器首次部署

### 5.1 准备目录

本文示例使用：

```text
应用目录：E:\file-agent
交付包目录：E:\packages\file-agent-release-20260904-code2
受管原始目录：E:\workdata
```

创建目录并解压源码：

```powershell
New-Item -ItemType Directory -Force "E:\file-agent", "E:\workdata" | Out-Null

Expand-Archive `
  -LiteralPath "E:\packages\file-agent-release-20260904-code2\file-agent-source-20260904-code2.zip" `
  -DestinationPath "E:\file-agent" `
  -Force

Set-Location E:\file-agent
```

如果 ZIP 内额外包含一层 `project` 目录，应进入实际含有 `apps` 和 `deploy` 的目录后再执行后续命令。

### 5.2 校验并导入镜像

使用项目脚本校验 SHA-256、执行 `docker load` 并核对五个镜像：

```powershell
.\deploy\import-offline-images.ps1 `
  -ArchivePath "E:\packages\file-agent-release-20260904-code2\file-agent-full-cpu-20260904-code2.tar" `
  -ImageTag "20260904-code2" `
  -BaseImageTag "20260904-v1"
```

导入时间取决于磁盘性能。完成后检查：

```powershell
docker images --format "{{.Repository}}:{{.Tag}}  {{.Size}}"
```

必须能看到本文第 1 节列出的五个镜像。

### 5.3 生成部署环境文件和随机密钥

真实配置保存在 `deploy/.env`，不在 Docker 镜像中。部署脚本在该文件不存在时会基于模板生成 32 位
PostgreSQL 密码、32 位 Neo4j 密码和 64 位 JWT 密钥。

在管理员 PowerShell 中执行一次：

```powershell
Set-Location E:\file-agent
.\deploy\deploy.ps1 -SiteAddress ":80" -UsePrebuiltImages
```

对于尚未配置 LLM 的新服务器，脚本生成 `deploy/.env` 后会提示填写 LLM 地址、密钥和模型并停止，容器
不会以不完整配置继续启动。这是首次初始化的预期行为。

不要反复删除并重新生成 `deploy/.env`。确认文件已经存在：

```powershell
Test-Path .\deploy\.env
```

### 5.4 编辑 deploy/.env

```powershell
notepad .\deploy\.env
```

至少核对以下配置：

```dotenv
# 局域网 HTTP 部署；浏览器使用 http://服务器IP/
CADDY_SITE_ADDRESS=:80
UPLOAD_MAX_SIZE=1024MB

# 必须和已导入镜像标签完全一致
FILE_AGENT_IMAGE_TAG=20260904-code2
FILE_AGENT_BASE_IMAGE_TAG=20260904-v1

# 目标服务器只使用预构建镜像，不需要填写种子镜像
FILE_AGENT_BASE_SEED_IMAGE=
FILE_AGENT_WEB_SEED_IMAGE=

POSTGRES_DB=fileAgent
POSTGRES_USER=fileagent_user
POSTGRES_PASSWORD=<部署脚本生成的随机密码，不要留占位符>

# Neo4j 固定初始用户名为 neo4j
NEO4J_PASSWORD=<部署脚本生成的随机密码，不要留占位符>
NEO4J_URI=bolt://neo4j:7687
NEO4J_USERNAME=neo4j
NEO4J_DATABASE=neo4j

JWT_SECRET_KEY=<部署脚本生成的随机长密钥，不要留占位符>
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

# 当前部署脚本要求这三个值非空
LLM_ENABLED=true
LLM_PROVIDER=openai_compatible
LLM_API_KEY=<真实密钥>
LLM_BASE_URL=<OpenAI-compatible 服务地址，通常以 /v1 结尾>
LLM_CHAT_MODEL=<服务支持的真实模型名>

# 结构化抽取配置留空时复用通用 LLM 配置
STRUCTURED_EXTRACTION_LLM_BASE_URL=
STRUCTURED_EXTRACTION_LLM_API_KEY=
STRUCTURED_EXTRACTION_LLM_MODEL=

# Windows 宿主机受管目录；容器内路径不要改成 Windows 路径
MANAGED_ROOT_HOST_PATH=E:/workdata
MANAGED_ROOT_WORKDATA=/managed/workdata
MANAGED_ROOT_VOLUME_MODE=ro
```

重要说明：

- `NEO4J_URI` 必须保持 `bolt://neo4j:7687`。`neo4j` 是 Compose 内部服务名，不能改成服务器局域网 IP。
- `MANAGED_ROOT_HOST_PATH` 是 Windows 路径；`MANAGED_ROOT_WORKDATA` 是容器路径，二者不能混用。
- 默认 `MANAGED_ROOT_VOLUME_MODE=ro`，OCR、分类、预览和索引不会覆盖受管原件。
- `STRUCTURED_EXTRACTION_EXTERNAL_IMAGES_AUTHORIZED=false` 时不会把原始图片发送到外部接口。
- 不要把 `deploy/.env` 发给无关人员、提交 Git 或放入源码 ZIP。

检查是否仍有未替换的双下划线占位符：

```powershell
Select-String -LiteralPath .\deploy\.env -Pattern "__[A-Z0-9_]+__"
```

命令应无输出。

### 5.5 检查 Compose 配置

以下命令只验证配置，不打印展开后的密码：

```powershell
docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  config --quiet
```

如有错误，应先修正 `.env`、镜像标签或受管目录，不要跳过检查。

### 5.6 启动服务

管理员 PowerShell：

```powershell
Set-Location E:\file-agent
.\deploy\deploy.ps1 -UsePrebuiltImages -OpenFirewall
```

`-UsePrebuiltImages` 会使用 `--no-build --pull never`，因此不会拉取镜像或重新安装依赖。`-OpenFirewall`
只添加 TCP 80/443 入站规则；需要管理员权限。

启动顺序由 Compose 自动控制：

```text
PostgreSQL / Neo4j
-> migrate 数据库迁移
-> API
-> scheduler / watcher / workers
-> Caddy gateway
```

`migrate` 是一次性服务，执行成功后显示 `Exited (0)` 属于正常状态。

### 5.7 局域网访问

如果服务器内网 IP 是 `10.102.8.176` 且 `CADDY_SITE_ADDRESS=:80`，使用：

```text
http://10.102.8.176/
```

生产部署不要再使用 Vite 的 `:5173`，也不要让浏览器直接请求 API 的 `:8000`。Caddy 会把同源 `/api`
请求转发到内部 API，从而避免之前的 CORS/Preflight 502 问题。

建议为服务器配置固定 IP 或 DHCP 地址保留，否则 IP 变化后客户端地址也会变化。

### 5.8 创建首个用户和管理员

当前网关允许公开调用注册接口，普通用户可以在登录页选择“申请注册”。也可以在服务启动后使用脚本预创建
账号，脚本会安全提示输入密码：

```powershell
.\deploy\create-user.ps1 `
  -Username "fileadmin" `
  -DisplayName "系统管理员"
```

新注册或由该脚本创建的账号默认角色都是 `user`，不会自动成为管理员。首次部署需要由服务器管理员把一个
已确认的精确账号提升为 `admin`。用户名建议只使用字母、数字、下划线和连字符：

```powershell
$AdminUser = "fileadmin"
if ($AdminUser -notmatch '^[A-Za-z0-9_-]+$') { throw "管理员用户名格式不安全。" }

docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  exec -T postgres psql -U fileagent_user -d fileAgent `
  -v "target_user=$AdminUser" -v ON_ERROR_STOP=1 `
  -c "UPDATE users SET role='admin' WHERE username=:'target_user';"

docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  exec -T postgres psql -U fileagent_user -d fileAgent `
  -v "target_user=$AdminUser" -tAc "SELECT username || ':' || role FROM users WHERE username=:'target_user';"
```

更新结果必须恰好影响一个账号，查询应返回 `fileadmin:admin`。提升后需要退出并重新登录，因为旧 JWT 中仍
保存提升前的角色。若系统不允许公众自行注册，必须在对互联网开放前增加注册审批或关闭注册路由；当前
部署包默认明确开启公开注册，不能只靠隐藏页面来限制注册。

## 6. 部署验收

所有命令在 `E:\file-agent` 下执行。

### 6.1 容器状态

```powershell
docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  ps
```

除一次性 `migrate` 外，下列服务应为 `Up`，有健康检查的服务应为 `healthy`：

- `postgres`
- `neo4j`
- `api`
- `scheduler`
- `watcher`
- `reconcile-scan-worker`
- `lifecycle-worker`
- `source-analysis-worker`
- `structured-extraction-worker`
- `graph-worker`
- `gateway`

### 6.2 API 和页面

```powershell
curl.exe -I http://127.0.0.1/
curl.exe http://127.0.0.1/api/health
```

然后从另一台局域网机器访问：

```text
http://10.102.8.176/
```

完成注册、登录、上传小型 TXT/DOCX/PDF、发送明确任务文字，并确认前端能显示逐文件回执。

### 6.3 PostgreSQL

```powershell
docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  exec -T postgres pg_isready -U fileagent_user -d fileAgent
```

应显示 `accepting connections`。

### 6.4 Neo4j

不需要发布 7474/7687。直接在容器内部用已经注入的密码验证：

```powershell
docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  exec -T neo4j sh -lc 'cypher-shell -a bolt://127.0.0.1:7687 -u neo4j -p "${NEO4J_AUTH#*/}" "RETURN 1;"'
```

返回一行 `1` 表示认证和数据库正常。

### 6.5 API 依赖、模型和目录挂载

```powershell
docker compose `
  --env-file .\deploy\.env `
  -f .\deploy\docker-compose.production.yml `
  exec -T api python /app/deploy/scripts/verify_runtime.py --managed-root
```

应输出：

```text
runtime-verification=ok
```

## 7. 日常运维

### 7.1 查看状态和日志

```powershell
# 状态
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml ps

# 最近 200 行日志
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs --tail 200

# 持续查看 API
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs -f api

# 持续查看 Neo4j
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs -f neo4j

# 查看全部 worker
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml ps `
  scheduler watcher reconcile-scan-worker lifecycle-worker source-analysis-worker `
  structured-extraction-worker graph-worker
```

应用 JSONL 文件日志位于：

```text
E:\file-agent\data\logs
```

默认保留 7 天。

### 7.2 停止和重新启动

停止容器但保留 PostgreSQL、Neo4j 和 Caddy 命名卷：

```powershell
.\deploy\stop.ps1
```

重新启动并执行健康检查：

```powershell
.\deploy\deploy.ps1 -UsePrebuiltImages
```

不要执行以下命令，除非已经确认要永久删除数据库和 Caddy 状态：

```text
docker compose down -v
```

Windows 重启后还应检查 Docker Desktop 和容器是否自动恢复：

```powershell
docker info
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml ps
```

### 7.3 备份

同时备份 PostgreSQL 和上传/派生文件：

```powershell
.\deploy\backup.ps1 -IncludeUploads
```

备份输出：

```text
E:\file-agent\data\backups\file-agent-db-<时间>.sql
E:\file-agent\data\backups\file-agent-uploads-<时间>.zip
```

应把备份复制到服务器之外的受控介质。PostgreSQL 是业务事实源，`data/uploads` 保存上传、派生件和工作
副本，二者必须配套备份。Neo4j 当前是可从 PostgreSQL 重建的图投影，不应代替 PostgreSQL 备份。

恢复属于会覆盖现有业务状态的高风险操作，应先停止用户访问、备份当前状态，并在测试环境验证 SQL 与
uploads ZIP；项目当前没有一键恢复脚本，不要在未确认目标数据库和目录时直接覆盖生产数据。

### 7.4 密码管理

- 第一次初始化成功后，PostgreSQL 和 Neo4j 密码已经写入各自数据卷。
- 只修改 `.env` 不会自动修改已初始化数据库里的真实密码，并可能导致服务健康检查失败。
- 日常重启和代码更新必须保留原 `deploy/.env`。
- 如需轮换密码，应使用 PostgreSQL/Neo4j 官方密码修改命令同步修改数据库，再更新 `.env`，并安排维护
  窗口验证所有服务。
- JWT 密钥改变后，现有登录令牌会全部失效。

## 8. 离线更新最新代码

推荐在构建机生成新的 API/Web 镜像和匹配的源码 ZIP，再传到服务器；目标服务器不重新下载依赖和模型。

### 8.1 构建机生成下一版

依赖和模型不变时复用基础镜像：

```powershell
.\deploy\build-layered-images.ps1 `
  -ImageTag "20260905-code1" `
  -BaseImageTag "20260904-v1" `
  -SeedWebImage "file-agent-web:20260904-code2"
```

然后按第 4 节导出：

- `file-agent-api-runtime-base:20260904-v1`
- `file-agent-api-full-cpu:20260905-code1`
- `file-agent-web:20260905-code1`
- `pgvector/pgvector:pg16`
- `neo4j:5.26-community`

只有 requirements、系统包、模型版本或模型预加载逻辑变化时才更换 `BaseImageTag`。普通源码更新不要传
`-RebuildBase`。

### 8.2 目标服务器更新

1. 执行 `backup.ps1 -IncludeUploads`。
2. 导入新镜像归档，并使用新 `ImageTag`、实际 `BaseImageTag` 校验。
3. 准备与镜像同版本的源码 ZIP。
4. 把 `deploy/.env` 中 `FILE_AGENT_IMAGE_TAG` 改为新代码标签。
5. 如果基础镜像确实变化，再修改 `FILE_AGENT_BASE_IMAGE_TAG`。
6. 执行：

```powershell
.\deploy\update.ps1 `
  -PackageZip "E:\packages\file-agent-source-20260905-code1.zip" `
  -UsePrebuiltImages
```

更新脚本会保留 `deploy/.env` 和 `data/`，重新创建一次性迁移容器，再检查 API 和模型运行时。不要用新
源码配旧代码镜像，也不要只改镜像标签而不导入对应镜像。

### 8.3 回滚边界

更新前必须保留旧镜像标签、旧源码 ZIP、数据库 SQL、uploads ZIP 和受控保存的旧 `.env`。代码/镜像回滚
的一般顺序是停止访问、恢复匹配版本的源码、把 `FILE_AGENT_IMAGE_TAG` 改回旧标签，再用
`-UsePrebuiltImages` 启动。

但是数据库迁移默认只保证向前升级，切换回旧镜像不等于数据库可以自动降级。如果新版已经执行了不兼容
迁移，应恢复更新前 PostgreSQL 与 uploads 配套备份，而不是尝试让旧代码直接读取新版数据库。当前项目
没有自动数据库降级或一键回滚脚本；生产回滚必须先在测试环境演练，并核对恢复目标、备份时间和用户停机
窗口。

## 9. 常见故障

### 9.1 `pull access denied`、403 或尝试访问 Docker Hub

原因通常是缺少本地镜像、标签不一致，或者没有使用 `-UsePrebuiltImages`。

```powershell
docker images
Select-String -LiteralPath .\deploy\.env -Pattern "^FILE_AGENT_.*IMAGE_TAG="
```

确认五个镜像都已导入，并使用：

```powershell
.\deploy\deploy.ps1 -UsePrebuiltImages
```

### 9.2 API 不健康或浏览器返回 502

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml ps
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs --tail 200 api
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs --tail 100 gateway
```

常见原因是数据库迁移失败、数据库密码不匹配、模型验证失败、受管目录不存在或 API 尚未完成启动。不要
用 `http://服务器IP:5173` 作为生产入口。

### 9.3 Neo4j 一直 unhealthy

- 确认 `NEO4J_PASSWORD` 非空且不含占位符。
- 如果 `neo4j_data` 已初始化，不要只改 `.env` 密码。
- 查看 `neo4j` 日志并在容器内执行第 6.4 节的认证检查。
- 不要把应用的 `NEO4J_URI` 改成 `localhost`；API 容器内应使用 `bolt://neo4j:7687`。

### 9.4 PostgreSQL 一直 unhealthy

- 核对 `POSTGRES_DB`、`POSTGRES_USER`、`POSTGRES_PASSWORD`。
- 已存在 `postgres_data` 时，环境变量密码必须和数据库实际密码一致。
- 检查日志：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml logs --tail 200 postgres
```

### 9.5 80/443 端口被占用

```powershell
Get-NetTCPConnection -State Listen -LocalPort 80,443 -ErrorAction SilentlyContinue
```

停止冲突服务或由管理员统一调整入口端口/反向代理。不要为规避冲突直接发布数据库和 API 内部端口。

### 9.6 受管目录挂载失败

- 确认 `MANAGED_ROOT_HOST_PATH` 对应目录在 Windows 上真实存在。
- Docker Desktop 必须有权访问该磁盘。
- Windows 路径在 `.env` 中推荐使用 `E:/workdata` 形式。
- 保持默认只读，除非已经明确授权受控文件操作并完成 OperationPlan 安全配置。

### 9.7 Docker C 盘空间不足

先检查：

```powershell
Get-PSDrive C,E | Select-Object Name,Used,Free
docker system df
```

不要直接执行广泛的 `docker system prune -a`，它可能删除仍用于快速更新的基础层和本地镜像。应先备份
需要保留的镜像，再精确处理无用构建缓存或把 Docker 数据迁移到空间更充足的磁盘。

### 9.8 migrate 报 `entrypoint.api.sh: no such file or directory`

如果 PostgreSQL 和 Neo4j 已健康，但 `migrate` 日志显示：

```text
exec /app/deploy/entrypoint.api.sh: no such file or directory
```

说明旧代码镜像中的 Linux 入口脚本被 Windows CRLF 行尾破坏，并不是脚本路径真的不存在。修正版
`Dockerfile.api` 会强制转换 LF，仓库 `.gitattributes` 也固定所有 `*.sh` 使用 LF。

目标服务器已经导入大模型基础镜像时，不需要重新传输或下载模型。把修正后的源码 ZIP 复制到服务器，先
备份环境文件，再覆盖解压源码：

```powershell
Copy-Item .\deploy\.env "$env:TEMP\file-agent-deploy.env" -Force
Expand-Archive `
  -LiteralPath "E:\packages\file-agent-source-20260904-code2.zip" `
  -DestinationPath "E:\file-agent" `
  -Force
if (-not (Test-Path .\deploy\.env)) {
  Copy-Item "$env:TEMP\file-agent-deploy.env" .\deploy\.env -Force
}
```

然后只重建 API 代码层：

```powershell
.\deploy\build-layered-images.ps1 `
  -ImageTag "20260904-code2" `
  -BaseImageTag "20260904-v1" `
  -SkipWeb
```

该命令复用本地 `file-agent-api-runtime-base:20260904-v1`，不运行 APT、pip 或模型下载。验证修复：

```powershell
docker run --rm --entrypoint python `
  file-agent-api-full-cpu:20260904-code2 `
  -c "from pathlib import Path; b=Path('/app/deploy/entrypoint.api.sh').read_bytes(); print(b.count(bytes([13,10])))"
```

必须输出 `0`。最后移除失败容器但保留命名卷，再重新部署：

```powershell
docker compose --env-file .\deploy\.env -f .\deploy\docker-compose.production.yml down --remove-orphans
.\deploy\deploy.ps1 -UsePrebuiltImages -OpenFirewall
```

不要添加 `-v`，否则会删除数据库命名卷。

## 10. 上线检查清单

- [ ] Docker Desktop 使用 Linux containers，资源至少 4 CPU/20 GB。
- [ ] 五个离线镜像已导入，API/Web 标签与 `.env` 完全一致。
- [ ] `deploy/.env` 不含任何 `__PLACEHOLDER__`。
- [ ] PostgreSQL、Neo4j 和 JWT 使用不同的随机强密钥。
- [ ] `NEO4J_URI=bolt://neo4j:7687`，没有改成宿主机 IP。
- [ ] `MANAGED_ROOT_HOST_PATH` 存在，默认以 `ro` 挂载。
- [ ] LLM 地址、密钥和模型已经验证可访问。
- [ ] 只开放 80/443，没有开放 5432、7474、7687、8000。
- [ ] `migrate` 正常 `Exited (0)`，其余服务运行，API/PostgreSQL/Neo4j 健康。
- [ ] `verify_runtime.py --managed-root` 输出 `runtime-verification=ok`。
- [ ] 另一台局域网机器可以打开页面、注册、登录、上传并执行明确文件任务。
- [ ] 已完成 PostgreSQL 与 `data/uploads` 首次备份，并把副本移出服务器。
