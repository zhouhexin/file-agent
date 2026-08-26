[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ArchivePath,
    [string]$ChecksumPath,
    [string]$ImageTag = "20260826"
)

$ErrorActionPreference = "Stop"
$ResolvedArchive = (Resolve-Path -LiteralPath $ArchivePath).Path
if ([string]::IsNullOrWhiteSpace($ChecksumPath)) {
    $ChecksumPath = "$ResolvedArchive.sha256"
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}
docker info | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop is not running or is not accessible." }

if (-not (Test-Path -LiteralPath $ChecksumPath)) {
    throw "Checksum file is missing: $ChecksumPath"
}
$expectedHash = ((Get-Content -LiteralPath $ChecksumPath -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
$actualHash = (Get-FileHash -LiteralPath $ResolvedArchive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne $expectedHash) {
    throw "Offline image archive SHA-256 verification failed."
}
Write-Host "Offline image archive SHA-256 verification passed." -ForegroundColor Green

docker load --input $ResolvedArchive
if ($LASTEXITCODE -ne 0) { throw "The offline image import failed." }

$requiredImages = @(
    "file-agent-api-full-cpu:$ImageTag",
    "file-agent-web:$ImageTag",
    "pgvector/pgvector:pg16",
    "neo4j:5.26-community"
)
foreach ($image in $requiredImages) {
    docker image inspect $image | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "The offline archive is missing image: $image" }
}
Write-Host "All offline images are ready. Run deploy.ps1 -UsePrebuiltImages." -ForegroundColor Green
