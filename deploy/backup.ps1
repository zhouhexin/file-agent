[CmdletBinding()]
param([switch]$IncludeUploads)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$ComposeFile = Join-Path $DeployDir "docker-compose.production.yml"
$EnvFile = Join-Path $DeployDir ".env"
$BackupDir = Join-Path $ProjectRoot "data\backups"
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
if (-not (Test-Path $EnvFile)) { throw "未找到部署配置。" }
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null

$values = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#][^=]*)=(.*)$') { $values[$matches[1].Trim()] = $matches[2].Trim() }
}
$sqlFile = Join-Path $BackupDir "file-agent-db-$Timestamp.sql"
Push-Location $ProjectRoot
try {
    & docker compose --env-file $EnvFile -f $ComposeFile exec -T postgres pg_dump -U $values["POSTGRES_USER"] -d $values["POSTGRES_DB"] | Out-File -FilePath $sqlFile -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw "数据库备份失败。" }
    Write-Host "数据库备份：$sqlFile" -ForegroundColor Green
    if ($IncludeUploads) {
        $zipFile = Join-Path $BackupDir "file-agent-uploads-$Timestamp.zip"
        Compress-Archive -Path (Join-Path $ProjectRoot "data\uploads\*") -DestinationPath $zipFile -Force
        Write-Host "上传文件备份：$zipFile" -ForegroundColor Green
    }
} finally { Pop-Location }
