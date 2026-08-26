[CmdletBinding()]
param(
    [string]$PackageZip,
    [switch]$SkipGitPull,
    [switch]$UsePrebuiltImages
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$ComposeFile = Join-Path $DeployDir "docker-compose.production.yml"
$EnvFile = Join-Path $DeployDir ".env"

function Sync-PackageToProject {
    param(
        [Parameter(Mandatory = $true)][string]$SourceRoot,
        [Parameter(Mandatory = $true)][string]$TargetRoot
    )

    $envBackupPath = $null
    $existingEnv = Join-Path $TargetRoot "deploy\.env"
    if (Test-Path $existingEnv) {
        $envBackupPath = Join-Path ([System.IO.Path]::GetTempPath()) ("file-agent-env-" + [guid]::NewGuid().ToString("N") + ".env")
        Copy-Item -Path $existingEnv -Destination $envBackupPath -Force
    }

    # 仅同步代码与部署文件，保留运行时数据目录和现有 deploy/.env。
    Get-ChildItem -Path $SourceRoot -Force | ForEach-Object {
        $name = $_.Name
        if ($name -in @(".git", "data")) { return }

        $sourcePath = $_.FullName
        $targetPath = Join-Path $TargetRoot $name

        if (Test-Path $targetPath) {
            if ((Get-Item $targetPath).PSIsContainer) {
                Remove-Item -Path $targetPath -Recurse -Force
            } elseif ($targetPath -ne (Join-Path $TargetRoot "deploy\.env")) {
                Remove-Item -Path $targetPath -Force
            }
        }

        if ($_.PSIsContainer) {
            Copy-Item -Path $sourcePath -Destination $targetPath -Recurse -Force
        } else {
            Copy-Item -Path $sourcePath -Destination $targetPath -Force
        }
    }

    if ($null -ne $envBackupPath -and (Test-Path $envBackupPath)) {
        $deployDir = Join-Path $TargetRoot "deploy"
        if (-not (Test-Path $deployDir)) {
            New-Item -ItemType Directory -Path $deployDir | Out-Null
        }
        Copy-Item -Path $envBackupPath -Destination (Join-Path $deployDir ".env") -Force
        Remove-Item -Path $envBackupPath -Force -ErrorAction SilentlyContinue
    }

    $incomingEnv = Join-Path $SourceRoot "deploy\.env.production.example"
    if (-not (Test-Path $incomingEnv)) {
        throw "离线包缺少 deploy/.env.production.example，无法继续。"
    }
}

function Resolve-PackageProjectRoot {
    param(
        [Parameter(Mandatory = $true)][string]$ExpandedRoot
    )

    if ((Test-Path (Join-Path $ExpandedRoot "apps")) -and (Test-Path (Join-Path $ExpandedRoot "deploy"))) {
        return $ExpandedRoot
    }

    $candidate = Get-ChildItem -Path $ExpandedRoot -Directory | Where-Object {
        (Test-Path (Join-Path $_.FullName "apps")) -and (Test-Path (Join-Path $_.FullName "deploy"))
    } | Select-Object -First 1

    if ($null -eq $candidate) {
        throw "离线 zip 包中未找到包含 apps/ 和 deploy/ 的项目根目录。"
    }
    return $candidate.FullName
}

if (-not (Test-Path $EnvFile)) { throw "请先运行 deploy.ps1。" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未找到 docker。请先安装并启动 Docker Desktop。"
}
try { docker info | Out-Null } catch { throw "Docker Desktop 未启动或无法连接 Docker。" }

Push-Location $ProjectRoot
try {
    if (-not [string]::IsNullOrWhiteSpace($PackageZip)) {
        $resolvedZip = (Resolve-Path $PackageZip).Path
        $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("file-agent-update-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $tempRoot | Out-Null
        try {
            Expand-Archive -Path $resolvedZip -DestinationPath $tempRoot -Force
            $packageRoot = Resolve-PackageProjectRoot -ExpandedRoot $tempRoot
            Sync-PackageToProject -SourceRoot $packageRoot -TargetRoot $ProjectRoot
        } finally {
            Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    } elseif (-not $SkipGitPull) {
        git pull
    }

    docker compose --env-file $EnvFile -f $ComposeFile config --quiet

    if ($UsePrebuiltImages) {
        Write-Host "使用已导入的完整离线镜像。" -ForegroundColor Cyan
    } else {
        Write-Host "重新构建应用镜像并核对预下载模型；首次执行耗时较长。" -ForegroundColor Cyan
        docker compose --env-file $EnvFile -f $ComposeFile pull postgres neo4j
        docker compose --env-file $EnvFile -f $ComposeFile build api gateway
    }

    # migrate 是一次性容器；更新时必须重新创建，不能复用上一次的已完成状态。
    docker compose --env-file $EnvFile -f $ComposeFile rm -f migrate 2>$null
    if ($UsePrebuiltImages) {
        docker compose --env-file $EnvFile -f $ComposeFile up -d --no-build --pull never
    } else {
        docker compose --env-file $EnvFile -f $ComposeFile up -d --no-build
    }

    Write-Host "等待更新后的 API 健康检查..." -ForegroundColor Cyan
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
        throw "更新后的 API 未通过健康检查。"
    }
    docker compose --env-file $EnvFile -f $ComposeFile exec -T api `
        python /app/deploy/scripts/verify_runtime.py --managed-root
    if ($LASTEXITCODE -ne 0) { throw "更新后的容器依赖、模型或受管目录校验失败。" }
    docker compose --env-file $EnvFile -f $ComposeFile ps
} finally { Pop-Location }
