$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$ComposeFile = Join-Path $DeployDir "docker-compose.production.yml"
$EnvFile = Join-Path $DeployDir ".env"
if (-not (Test-Path $EnvFile)) { throw "请先运行 deploy.ps1。" }
Push-Location $ProjectRoot
try {
    git pull
    docker compose --env-file $EnvFile -f $ComposeFile up -d --build
    docker compose --env-file $EnvFile -f $ComposeFile ps
} finally { Pop-Location }
