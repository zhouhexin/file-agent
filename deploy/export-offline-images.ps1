[CmdletBinding()]
param(
    [string]$OutputDirectory = ".\file-agent-offline-images",
    [string]$ImageTag = "20260826",
    [string]$BaseImageTag = "20260904-v1",
    [string]$AptDebianMirror = "https://mirrors.aliyun.com/debian",
    [string]$AptSecurityMirror = "https://mirrors.aliyun.com/debian-security",
    [string]$NpmRegistry = "https://registry.npmmirror.com",
    [string]$PipIndexUrl = "https://mirrors.aliyun.com/pypi/simple",
    [string]$HfEndpoint = "https://hf-mirror.com",
    [string]$DoclingModelGitBase = "https://hf-mirror.com",
    [string]$LocalModelCacheContext,
    [string]$SeedApiImage,
    [switch]$RebuildBase
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$OutputRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputDirectory))
$ArchivePath = Join-Path $OutputRoot "file-agent-full-cpu-$ImageTag.tar"
$ChecksumPath = "$ArchivePath.sha256"
$ManifestPath = Join-Path $OutputRoot "offline-image-manifest.json"

function Assert-LastExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}
docker info | Out-Null
Assert-LastExitCode "Docker Desktop is not running or is not accessible."
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

Push-Location $ProjectRoot
try {
    # 基础镜像存在时只重建代码层；显式 -RebuildBase 才强制更新依赖和模型。
    $buildArguments = @{
        ImageTag = $ImageTag
        BaseImageTag = $BaseImageTag
        AptDebianMirror = $AptDebianMirror
        AptSecurityMirror = $AptSecurityMirror
        NpmRegistry = $NpmRegistry
        PipIndexUrl = $PipIndexUrl
        HfEndpoint = $HfEndpoint
        DoclingModelGitBase = $DoclingModelGitBase
        LocalModelCacheContext = $LocalModelCacheContext
        SeedApiImage = $SeedApiImage
    }
    if ($RebuildBase) { $buildArguments["RebuildBase"] = $true }
    & (Join-Path $DeployDir "build-layered-images.ps1") @buildArguments

    docker pull pgvector/pgvector:pg16
    Assert-LastExitCode "The PostgreSQL/pgvector image pull failed."
    docker pull neo4j:5.26-community
    Assert-LastExitCode "The Neo4j image pull failed."

    docker save --output $ArchivePath `
        "file-agent-api-runtime-base:$BaseImageTag" `
        "file-agent-api-full-cpu:$ImageTag" `
        "file-agent-web:$ImageTag" `
        "pgvector/pgvector:pg16" `
        "neo4j:5.26-community"
    Assert-LastExitCode "The offline image archive export failed."

    $hash = (Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $archiveName = [System.IO.Path]::GetFileName($ArchivePath)
    Set-Content -LiteralPath $ChecksumPath -Value "$hash  $archiveName" -Encoding ASCII
    @{
        schema_version = 1
        profile = "windows11-full-cpu"
        hardware_baseline = @{ cpu_cores = 6; memory_gb = 32 }
        image_tag = $ImageTag
        base_image_tag = $BaseImageTag
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        archive = $archiveName
        sha256 = $hash
        images = @(
            "file-agent-api-runtime-base:$BaseImageTag",
            "file-agent-api-full-cpu:$ImageTag",
            "file-agent-web:$ImageTag",
            "pgvector/pgvector:pg16",
            "neo4j:5.26-community"
        )
    } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8

    Write-Host "Offline image archive: $ArchivePath" -ForegroundColor Green
    Write-Host "SHA-256: $hash" -ForegroundColor Green
} finally {
    Pop-Location
}
