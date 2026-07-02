[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateLength(1,100)][string]$Username,
    [string]$DisplayName = "",
    [string]$Email = "",
    [string]$Password
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$ComposeFile = Join-Path $DeployDir "docker-compose.production.yml"
$EnvFile = Join-Path $DeployDir ".env"
if (-not (Test-Path $EnvFile)) { throw "未找到 $EnvFile。请先运行 deploy.ps1。" }

if ([string]::IsNullOrWhiteSpace($Password)) {
    $secure = Read-Host "请输入用户密码（至少 6 位）" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $Password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
if ($Password.Length -lt 6) { throw "密码至少需要 6 位。" }

Push-Location $ProjectRoot
try {
    $args = @("compose", "--env-file", $EnvFile, "-f", $ComposeFile,
              "exec", "-T", "api", "python", "/app/deploy/scripts/create_user.py",
              "--username", $Username, "--password", $Password,
              "--display-name", $DisplayName)
    if (-not [string]::IsNullOrWhiteSpace($Email)) { $args += @("--email", $Email) }
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "创建用户失败。" }
} finally {
    Pop-Location
}
