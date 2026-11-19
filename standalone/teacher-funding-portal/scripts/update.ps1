[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Package,
    [string]$TargetRoot = 'E:\file-agent\standalone\teacher-funding-portal',
    [string]$ExpectedSha256 = '',
    [switch]$InstallOnly,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$RootNames = @('Dockerfile', 'compose.yml', 'compose.fileagent-server.yml', 'requirements.txt', 'README.md', '.env.example', '.dockerignore', '.gitignore')

function Assert-CodePath([string]$Relative) {
    if ($Relative -in $RootNames) { return }
    if ($Relative -match '^(app|scripts)/[a-zA-Z0-9_./-]+\.(py|html|css|js|ps1)$' -and
        $Relative -notmatch '(^|/)(\.\.?|__pycache__)(/|$)' -and $Relative -notmatch '//') { return }
    throw "Forbidden release path: $Relative"
}

function Safe-Path([string]$Base, [string]$Relative) {
    $baseFull = [IO.Path]::GetFullPath($Base).TrimEnd('\', '/')
    $full = [IO.Path]::GetFullPath((Join-Path $baseFull $Relative))
    if (-not $full.StartsWith($baseFull + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Path escapes the selected application directory.'
    }
    $probe = $full
    while ($probe -and $probe.Length -ge $baseFull.Length) {
        if (Test-Path -LiteralPath $probe) {
            if ((Get-Item -LiteralPath $probe -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing to follow a reparse point: $probe"
            }
        }
        $probe = [IO.Path]::GetDirectoryName($probe)
    }
    return $full
}

function Invoke-Compose([string[]]$Command) {
    & docker @script:ComposeArgs @Command
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose operation failed: $($Command[0])" }
}

function Test-Service([string]$Version) {
    if (-not $Version) { $Version = '-' }
    $health = @'
import json,sys,urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3) as response:
    value=json.load(response)
assert value.get('status')=='ok' and value.get('service')=='teacher-funding-portal'
assert sys.argv[1]=='-' or value.get('version')==sys.argv[1]
print('health=ok')
'@
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            $output = $health | & docker @script:ComposeArgs exec -T teacher-funding-portal python - $Version 2>&1
            if ($LASTEXITCODE -eq 0 -and ($output -join '') -match 'health=ok') { return }
        } catch { }
        Start-Sleep -Seconds 2
    }
    throw 'The new container did not pass the service/version health check.'
}

$Package = (Resolve-Path -LiteralPath $Package).Path
if (-not $ExpectedSha256) {
    $sidecar = "$Package.sha256"
    if (-not (Test-Path -LiteralPath $sidecar)) { throw 'Missing ZIP.sha256 sidecar. Copy both files to the server.' }
    $ExpectedSha256 = (Get-Content -LiteralPath $sidecar -Raw).Trim()
}
if ($ExpectedSha256 -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileHash -LiteralPath $Package -Algorithm SHA256).Hash -ne $ExpectedSha256) {
    throw 'ZIP SHA-256 mismatch; application files were not changed.'
}

$updateLock = $null
try {
    $zip = [IO.Compression.ZipFile]::OpenRead($Package)
    try {
        if ($zip.Entries.Count -gt 1000 -or ($zip.Entries | Measure-Object -Property Length -Sum).Sum -gt 104857600) {
            throw 'Release ZIP exceeds the allowed size.'
        }
        $seen = @{}
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ($seen.ContainsKey($name)) { throw "Duplicate ZIP entry: $name" }
            $seen[$name] = $entry
            if ($name -ne 'release-manifest.json') { Assert-CodePath $name }
            if ((($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'ZIP symlinks are not supported.' }
        }
        if (-not $seen.ContainsKey('release-manifest.json')) { throw 'Missing release manifest.' }
        $reader = New-Object IO.StreamReader($seen['release-manifest.json'].Open())
        try { $manifest = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
        if ($manifest.format -ne 1 -or $manifest.service -ne 'teacher-funding-portal' -or
            $manifest.version -cnotmatch '^[a-z0-9][a-z0-9._-]{0,63}$') { throw 'Invalid release identity/version.' }
        $declared = @{}
        foreach ($file in $manifest.files) {
            Assert-CodePath $file.path
            if ($declared.ContainsKey($file.path) -or -not $seen.ContainsKey($file.path)) { throw 'Invalid manifest file list.' }
            $declared[$file.path] = $file
            $entry = $seen[$file.path]
            if ($entry.Length -ne $file.size) { throw 'Manifest size mismatch.' }
            $stream = $entry.Open()
            $sha = [Security.Cryptography.SHA256]::Create()
            try { $digest = ([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
            finally { $stream.Dispose(); $sha.Dispose() }
            if ($digest -ne $file.sha256) { throw "Manifest hash mismatch: $($file.path)" }
        }
        if ($seen.Count -ne $declared.Count + 1) { throw 'Undeclared content in release ZIP.' }
        foreach ($required in ($RootNames + @('app/main.py', 'app/database.py', 'app/static/index.html', 'app/static/portal.css', 'app/static/portal.js', 'scripts/update.ps1'))) {
            if (-not $declared.ContainsKey($required)) { throw "Required release file missing: $required" }
        }
        if ($ValidateOnly) {
            Write-Output "package=ok version=$($manifest.version) files=$($declared.Count)"
            exit 0
        }

        $TargetRoot = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\', '/')
        if ([IO.Path]::GetFileName($TargetRoot) -ne 'teacher-funding-portal') { throw 'Target directory must end with teacher-funding-portal.' }
        $lockPath = Safe-Path $TargetRoot '.release-lock'
        New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
        $updateLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        $RunId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
        $stage = Safe-Path $TargetRoot ".release-work/$RunId"
        $backup = Safe-Path $TargetRoot ".release-backups/$RunId"
        New-Item -ItemType Directory -Force -Path $stage,$backup | Out-Null
        foreach ($entry in $zip.Entries) {
            $path = Safe-Path $stage $entry.FullName
            New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($path)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $path, $false)
        }
    } finally { $zip.Dispose() }

    $oldFiles = @{}
    foreach ($name in ($RootNames + @('release-manifest.json'))) {
        if (Test-Path -LiteralPath (Safe-Path $TargetRoot $name) -PathType Leaf) { $oldFiles[$name] = $true }
    }
    foreach ($folder in @('app', 'scripts')) {
        $path = Safe-Path $TargetRoot $folder
        if (Test-Path -LiteralPath $path) {
            foreach ($item in (Get-ChildItem -LiteralPath $path -File -Recurse)) {
                $relative = $item.FullName.Substring($TargetRoot.Length + 1).Replace('\', '/')
                if ($relative -match '__pycache__') { continue }
                Assert-CodePath $relative
                $null = Safe-Path $TargetRoot $relative
                $oldFiles[$relative] = $true
            }
        }
    }
    foreach ($name in $oldFiles.Keys) {
        $destination = Safe-Path $backup $name
        New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($destination)) | Out-Null
        Copy-Item -LiteralPath (Safe-Path $TargetRoot $name) -Destination $destination
    }
    @{ version=$manifest.version; package=$Package; files=@($oldFiles.Keys) } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $backup 'backup.json') -Encoding UTF8

    $envFile = Safe-Path $TargetRoot '.env'
    if (-not $InstallOnly -and -not (Test-Path -LiteralPath $envFile)) { throw 'Server .env missing. Use -InstallOnly for first installation, then configure .env.' }
    $script:ComposeArgs = @('compose', '-p', 'teacher-funding-portal', '--project-directory', $TargetRoot, '--env-file', $envFile,
        '-f', (Safe-Path $TargetRoot 'compose.yml'), '-f', (Safe-Path $TargetRoot 'compose.fileagent-server.yml'))
    $oldImage = ''
    $rollbackTag = "teacher-funding-portal:rollback-$RunId"
    if (-not $InstallOnly -and (Test-Path -LiteralPath (Safe-Path $TargetRoot 'compose.yml'))) {
        $container = @(& docker @script:ComposeArgs ps -a -q teacher-funding-portal)
        if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the existing service; stopped before replacing code.' }
        if ($container.Count -gt 1) { throw 'Multiple existing service containers; stopped before replacing code.' }
        if ($container.Count -eq 1) {
            $oldImage = (& docker inspect --format '{{.Image}}' $container[0]).Trim()
            if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the existing image.' }
            & docker tag $oldImage $rollbackTag
            if ($LASTEXITCODE -ne 0) { throw 'Unable to retain the rollback image.' }
        }
    }

    $newFiles = @($declared.Keys) + @('release-manifest.json')
    $switched = $false
    try {
        foreach ($name in $newFiles) {
            $destination = Safe-Path $TargetRoot $name
            New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($destination)) | Out-Null
            Copy-Item -LiteralPath (Safe-Path $stage $name) -Destination $destination -Force
        }
        foreach ($name in $oldFiles.Keys) {
            if ($name -notin $newFiles) { Remove-Item -LiteralPath (Safe-Path $TargetRoot $name) -Force }
        }
        if (-not $InstallOnly) {
            Invoke-Compose -Command @('config', '--quiet')
            Invoke-Compose -Command @('build', '--build-arg', "PORTAL_RELEASE_VERSION=$($manifest.version)", 'teacher-funding-portal')
            $switched = $true
            Invoke-Compose -Command @('up', '-d', '--no-deps', '--no-build', '--force-recreate', 'teacher-funding-portal')
            Test-Service $manifest.version
        }
        Write-Output "release=ok version=$($manifest.version) backup=$backup"
        if ($InstallOnly) { Write-Output 'Code installed only. Configure server .env, then run this updater without -InstallOnly.' }
    } catch {
        $reason = $_.Exception.Message
        foreach ($name in $newFiles) {
            $path = Safe-Path $TargetRoot $name
            if (-not $oldFiles.ContainsKey($name) -and (Test-Path -LiteralPath $path)) { Remove-Item -LiteralPath $path -Force }
        }
        foreach ($name in $oldFiles.Keys) {
            $destination = Safe-Path $TargetRoot $name
            New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($destination)) | Out-Null
            Copy-Item -LiteralPath (Safe-Path $backup $name) -Destination $destination -Force
        }
        if ($oldImage) {
            & docker tag $rollbackTag 'teacher-funding-portal:0.1.0'
            if ($LASTEXITCODE -ne 0) { throw "Update failed and image restore failed. Backup: $backup" }
            if ($switched) {
                Invoke-Compose -Command @('up', '-d', '--no-deps', '--no-build', '--force-recreate', 'teacher-funding-portal')
                Test-Service ''
            }
        }
        throw "Update failed; previous code restored. Database unchanged by rollback. Backup: $backup. Reason: $reason"
    }
} finally {
    if ($updateLock) { $updateLock.Dispose() }
}
exit 0
