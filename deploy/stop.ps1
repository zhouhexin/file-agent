$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$ComposeFile = Join-Path $DeployDir "docker-compose.production.yml"
$EnvFile = Join-Path $DeployDir ".env"
if (-not (Test-Path $EnvFile)) { throw "未找到部署配置。" }
Push-Location $ProjectRoot
try { docker compose --env-file $EnvFile -f $ComposeFile down }
finally { Pop-Location }
