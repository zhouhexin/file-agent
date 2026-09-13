[CmdletBinding()]
param(
    [string]$Version = "20260912-code1",
    [string]$OutputDirectory = "release"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ReleaseRoot = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputDirectory))
$StageRoot = Join-Path $ReleaseRoot ("package-staging-" + $Version)
$PackageZip = Join-Path $ReleaseRoot ("file-agent-code-" + $Version + ".zip")
$ChecksumPath = "$PackageZip.sha256"

if (-not $ReleaseRoot.StartsWith($ProjectRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Release directory must be within the project: $ReleaseRoot"
}

New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null
if (Test-Path -LiteralPath $StageRoot) {
    $ResolvedStageRoot = (Resolve-Path -LiteralPath $StageRoot).Path
    if (-not $ResolvedStageRoot.StartsWith($ReleaseRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw "Refusing to remove staging directory outside the release directory: $ResolvedStageRoot"
    }
    Remove-Item -LiteralPath $ResolvedStageRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $StageRoot | Out-Null

function Copy-ReleaseDirectory([string]$Name) {
    $Source = Join-Path $ProjectRoot $Name
    $Destination = Join-Path $StageRoot $Name
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Release source directory does not exist: $Source"
    }

    # robocopy applies /XD before traversal, avoiding tar.exe stat calls on protected cache directories.
    robocopy $Source $Destination /E /XD node_modules .pytest_cache __pycache__ storage logs /XF .env *.pyc *.log /R:0 /W:0 /NJH /NJS /NDL /NFL | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "Release directory copy failed: $Name (robocopy exit code: $LASTEXITCODE)"
    }
}

try {
    foreach ($Directory in @("apps", "deploy", "docs", "rules", "skills")) {
        Copy-ReleaseDirectory $Directory
    }
    foreach ($File in @("README.md", ".dockerignore")) {
        Copy-Item -LiteralPath (Join-Path $ProjectRoot $File) -Destination (Join-Path $StageRoot $File) -Force
    }

    if (Test-Path -LiteralPath $PackageZip) { Remove-Item -LiteralPath $PackageZip -Force }
    if (Test-Path -LiteralPath $ChecksumPath) { Remove-Item -LiteralPath $ChecksumPath -Force }
    tar.exe -a -c -f $PackageZip -C $StageRoot apps deploy docs rules skills README.md .dockerignore
    if ($LASTEXITCODE -ne 0) { throw "Code ZIP creation failed." }

    $Entries = tar.exe -tf $PackageZip
    if ($LASTEXITCODE -ne 0) { throw "Could not read code ZIP." }
    $RequiredEntries = @(
        "apps/web/dist/index.html",
        "deploy/update.ps1",
        "deploy/build-layered-images.ps1",
        "deploy/Dockerfile.api",
        "deploy/Dockerfile.web-reuse",
        "deploy/.env.production.example"
    )
    foreach ($RequiredEntry in $RequiredEntries) {
        if (-not ($Entries | Where-Object { $_.TrimEnd("/") -eq $RequiredEntry })) {
            throw "Code ZIP is missing: $RequiredEntry"
        }
    }
    $ForbiddenEntries = $Entries | Where-Object {
        $_ -match '(^|/)deploy/\.env$' -or
        $_ -match '(^|/)(node_modules|data|storage|logs|\.pytest_cache|__pycache__)/' -or
        $_ -match '\.(pyc|log)$'
    }
    if ($ForbiddenEntries) {
        throw "Code ZIP contains prohibited release content: $($ForbiddenEntries -join ', ')"
    }

    $Hash = (Get-FileHash -LiteralPath $PackageZip -Algorithm SHA256).Hash.ToLowerInvariant()
    "$Hash  $([IO.Path]::GetFileName($PackageZip))" | Set-Content -LiteralPath $ChecksumPath -Encoding ASCII
    Get-Item -LiteralPath $PackageZip, $ChecksumPath | Select-Object FullName, Length
    Write-Host "SHA256=$Hash" -ForegroundColor Green
} finally {
    if (Test-Path -LiteralPath $StageRoot) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force
    }
}
