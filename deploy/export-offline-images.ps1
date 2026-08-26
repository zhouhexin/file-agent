[CmdletBinding()]
param(
    [string]$OutputDirectory = ".\file-agent-offline-images",
    [string]$ImageTag = "20260826",
    [string]$AptDebianMirror = "https://mirrors.tuna.tsinghua.edu.cn/debian",
    [string]$AptSecurityMirror = "https://mirrors.tuna.tsinghua.edu.cn/debian-security",
    [string]$NpmRegistry = "https://registry.npmmirror.com",
    [string]$PipIndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$HfEndpoint = "https://hf-mirror.com",
    [string]$DoclingModelGitBase = "https://hf-mirror.com",
    [string]$LocalModelCacheContext
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$OutputRoot = [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputDirectory))
$ArchivePath = Join-Path $OutputRoot "file-agent-full-cpu-$ImageTag.tar"
$ChecksumPath = "$ArchivePath.sha256"
$ManifestPath = Join-Path $OutputRoot "offline-image-manifest.json"
if ([string]::IsNullOrWhiteSpace($LocalModelCacheContext)) {
    $LocalModelCacheContext = Join-Path $DeployDir "empty-model-cache"
}
$ResolvedModelCacheContext = (Resolve-Path -LiteralPath $LocalModelCacheContext).Path

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
    Write-Host "Building the full CPU image and preloading all models. The first build can take hours." -ForegroundColor Cyan
    docker build `
        --build-context "local-model-cache=$ResolvedModelCacheContext" `
        --build-arg MODEL_PRELOAD=true `
        --build-arg "APT_DEBIAN_MIRROR=$AptDebianMirror" `
        --build-arg "APT_SECURITY_MIRROR=$AptSecurityMirror" `
        --build-arg "NPM_REGISTRY=$NpmRegistry" `
        --build-arg "PIP_INDEX_URL=$PipIndexUrl" `
        --build-arg "HF_ENDPOINT=$HfEndpoint" `
        --build-arg "DOCLING_MODEL_GIT_BASE=$DoclingModelGitBase" `
        --tag "file-agent-api-full-cpu:$ImageTag" `
        --file deploy/Dockerfile.api .
    Assert-LastExitCode "The full CPU API image build failed."

    docker build `
        --build-arg VITE_API_BASE_URL=/api `
        --build-arg "NPM_REGISTRY=$NpmRegistry" `
        --tag "file-agent-web:$ImageTag" `
        --file deploy/Dockerfile.web .
    Assert-LastExitCode "The web image build failed."

    docker pull pgvector/pgvector:pg16
    Assert-LastExitCode "The PostgreSQL/pgvector image pull failed."
    docker pull neo4j:5.26-community
    Assert-LastExitCode "The Neo4j image pull failed."

    docker save --output $ArchivePath `
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
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        archive = $archiveName
        sha256 = $hash
        images = @(
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
