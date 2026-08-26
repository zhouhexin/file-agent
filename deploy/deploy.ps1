[CmdletBinding()]
param(
    [string]$SiteAddress,
    [switch]$OpenFirewall,
    [switch]$UsePrebuiltImages
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$ComposeFile = Join-Path $DeployDir "docker-compose.production.yml"
$EnvFile = Join-Path $DeployDir ".env"
$TemplateFile = Join-Path $DeployDir ".env.production.example"

function New-Secret([int]$Length) {
    $value = ""
    while ($value.Length -lt $Length) { $value += [Guid]::NewGuid().ToString("N") }
    return $value.Substring(0, $Length)
}

function Read-EnvValues([string]$Path) {
    $values = @{}
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
            $values[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $values
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 docker。请先安装并启动 Docker Desktop。"
}
try { docker info | Out-Null } catch { throw "Docker Desktop 未启动或无法连接 Docker。" }

$dockerCpuCount = [int](docker info --format '{{.NCPU}}')
$dockerMemoryBytes = [double](docker info --format '{{.MemTotal}}')
$dockerMemoryGb = [math]::Round($dockerMemoryBytes / 1GB, 1)
if ($dockerCpuCount -lt 4) {
    throw "Docker 当前只分配了 $dockerCpuCount 个 CPU；全功能部署至少需要 4 个，建议在 Docker Desktop 中分配 5 个。"
}
if ($dockerMemoryGb -lt 20) {
    throw "Docker 当前只分配了 ${dockerMemoryGb}GB 内存；PP-StructureV3/PaddleOCR-VL 部署至少需要 20GB，建议分配 24GB。"
}
if ($dockerCpuCount -lt 5 -or $dockerMemoryGb -lt 24) {
    Write-Warning "当前 Docker 资源为 ${dockerCpuCount} CPU / ${dockerMemoryGb}GB；可启动，但建议调整为 5 CPU / 24GB。"
}

if (-not (Test-Path $EnvFile)) {
    if ([string]::IsNullOrWhiteSpace($SiteAddress)) {
        $SiteAddress = Read-Host "请输入站点地址（公网 HTTPS 用域名；仅局域网测试可填 :80）"
    }
    if ([string]::IsNullOrWhiteSpace($SiteAddress)) { throw "站点地址不能为空。" }

    $content = Get-Content -Raw -Encoding UTF8 $TemplateFile
    $content = $content.Replace("__CADDY_SITE_ADDRESS__", $SiteAddress)
    $content = $content.Replace("__POSTGRES_PASSWORD__", (New-Secret 32))
    $content = $content.Replace("__NEO4J_PASSWORD__", (New-Secret 32))
    $content = $content.Replace("__JWT_SECRET_KEY__", (New-Secret 64))
    Set-Content -Path $EnvFile -Value $content -Encoding UTF8 -NoNewline
    Write-Host "已生成部署配置：$EnvFile" -ForegroundColor Green
} else {
    Write-Host "使用已有部署配置：$EnvFile" -ForegroundColor Yellow
}

$envValues = Read-EnvValues -Path $EnvFile
$managedRoot = $envValues["MANAGED_ROOT_HOST_PATH"]
if ([string]::IsNullOrWhiteSpace($managedRoot)) {
    throw "deploy/.env 缺少 MANAGED_ROOT_HOST_PATH。"
}
if (-not (Test-Path -LiteralPath $managedRoot -PathType Container)) {
    throw "受管目录不存在：$managedRoot"
}
if ([string]::IsNullOrWhiteSpace($envValues["LLM_BASE_URL"]) -or
    [string]::IsNullOrWhiteSpace($envValues["LLM_API_KEY"]) -or
    [string]::IsNullOrWhiteSpace($envValues["LLM_CHAT_MODEL"])) {
    throw "外部 LLM 配置尚未填写。请编辑 deploy/.env 中的 LLM_BASE_URL、LLM_API_KEY、LLM_CHAT_MODEL 后重新运行。"
}

New-Item -ItemType Directory -Force -Path `
    (Join-Path $ProjectRoot "data\uploads"), `
    (Join-Path $ProjectRoot "data\logs"), `
    (Join-Path $ProjectRoot "data\backups") | Out-Null

Push-Location $ProjectRoot
try {
    docker compose --env-file $EnvFile -f $ComposeFile config --quiet
    if ($UsePrebuiltImages) {
        docker compose --env-file $EnvFile -f $ComposeFile up -d --no-build --pull never
    } else {
        Write-Host "拉取数据库和图数据库基础镜像..." -ForegroundColor Cyan
        docker compose --env-file $EnvFile -f $ComposeFile pull postgres neo4j
        Write-Host "构建完整 CPU 镜像并预下载模型；首次构建耗时较长..." -ForegroundColor Cyan
        docker compose --env-file $EnvFile -f $ComposeFile build api gateway
        docker compose --env-file $EnvFile -f $ComposeFile up -d --no-build
    }

    Write-Host "等待 API 健康检查..." -ForegroundColor Cyan
    $healthy = $false
    for ($i = 1; $i -le 36; $i++) {
        $apiContainer = docker compose --env-file $EnvFile -f $ComposeFile ps -q api
        if ($apiContainer) {
            $health = docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $apiContainer
            if ($health -eq "healthy") { $healthy = $true; break }
        }
        Start-Sleep -Seconds 5
    }
    if (-not $healthy) {
        docker compose --env-file $EnvFile -f $ComposeFile logs --tail 120 api
        throw "API 未通过健康检查。"
    }

    docker compose --env-file $EnvFile -f $ComposeFile exec -T api `
        python /app/deploy/scripts/verify_runtime.py --managed-root
    if ($LASTEXITCODE -ne 0) { throw "容器运行依赖或模型校验失败。" }

    if ($OpenFirewall) {
        $principal = [Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
        if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            foreach ($port in 80,443) {
                $rule = "File Agent TCP $port"
                if (-not (Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue)) {
                    New-NetFirewallRule -DisplayName $rule -Direction Inbound -Action Allow -Protocol TCP -LocalPort $port | Out-Null
                }
            }
            Write-Host "已添加 Windows 防火墙 80/443 入站规则。" -ForegroundColor Green
        } else {
            Write-Warning "当前 PowerShell 不是管理员，未添加防火墙规则。"
        }
    }

    $siteLine = Get-Content $EnvFile | Where-Object { $_ -match '^CADDY_SITE_ADDRESS=' }
    $site = ($siteLine -split '=', 2)[1]
    Write-Host "部署成功。" -ForegroundColor Green
    if ($site -eq ':80') {
        Write-Host "局域网访问：http://<本机局域网 IP>/" -ForegroundColor Yellow
        Write-Host "当前为 HTTP，仅适用于临时局域网测试。" -ForegroundColor Yellow
    } else {
        Write-Host "访问地址：https://$site/" -ForegroundColor Green
        Write-Host "首次签发证书前，请确保 DNS、路由器端口转发和防火墙均已配置。" -ForegroundColor Yellow
    }
    Write-Host "公开注册已开启：用户可在登录页选择“申请注册”。" -ForegroundColor Cyan
    Write-Host "部署资源基线：Windows 11 / 6 核 / 32GB；Docker 当前 ${dockerCpuCount} CPU / ${dockerMemoryGb}GB。" -ForegroundColor Cyan
} finally {
    Pop-Location
}
