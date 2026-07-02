[CmdletBinding()]
param(
    [string]$SiteAddress,
    [switch]$OpenFirewall
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

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 docker。请先安装并启动 Docker Desktop。"
}
try { docker info | Out-Null } catch { throw "Docker Desktop 未启动或无法连接 Docker。" }

if (-not (Test-Path $EnvFile)) {
    if ([string]::IsNullOrWhiteSpace($SiteAddress)) {
        $SiteAddress = Read-Host "请输入站点地址（公网 HTTPS 用域名；仅局域网测试可填 :80）"
    }
    if ([string]::IsNullOrWhiteSpace($SiteAddress)) { throw "站点地址不能为空。" }

    $content = Get-Content -Raw -Encoding UTF8 $TemplateFile
    $content = $content.Replace("__CADDY_SITE_ADDRESS__", $SiteAddress)
    $content = $content.Replace("__POSTGRES_PASSWORD__", (New-Secret 32))
    $content = $content.Replace("__JWT_SECRET_KEY__", (New-Secret 64))
    Set-Content -Path $EnvFile -Value $content -Encoding UTF8 -NoNewline
    Write-Host "已生成部署配置：$EnvFile" -ForegroundColor Green
} else {
    Write-Host "使用已有部署配置：$EnvFile" -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force -Path `
    (Join-Path $ProjectRoot "data\uploads"), `
    (Join-Path $ProjectRoot "data\logs"), `
    (Join-Path $ProjectRoot "data\backups") | Out-Null

Push-Location $ProjectRoot
try {
    docker compose --env-file $EnvFile -f $ComposeFile up -d --build

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
} finally {
    Pop-Location
}
