param(
    [ValidateSet('Check', 'Acceptance')]
    [string]$Action = 'Check',
    [string]$Python = 'python',
    [switch]$CheckModelLogin
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$PreviousPythonUtf8 = $env:PYTHONUTF8
Push-Location $ProjectRoot
try {
    $env:PYTHONUTF8 = '1'
    if ($Action -eq 'Acceptance') {
        $PreviousAcceptance = $env:HARNESS_NATIVE_ACCEPTANCE
        $PreviousOsAcceptance = $env:HARNESS_OS_ACCEPTANCE
        $PreviousDatabaseUrl = $env:DATABASE_URL
        try {
            $env:HARNESS_NATIVE_ACCEPTANCE = '1'
            $env:HARNESS_OS_ACCEPTANCE = '1'
            $env:DATABASE_URL = 'postgresql://offline:offline@127.0.0.1/file_agent_unit_tests'
            & $Python -B -m pytest apps/api/app/tests/test_harness_local_runtime.py -q
            if ($LASTEXITCODE -ne 0) { throw 'Windows harness acceptance failed.' }
        } finally {
            $env:HARNESS_NATIVE_ACCEPTANCE = $PreviousAcceptance
            $env:HARNESS_OS_ACCEPTANCE = $PreviousOsAcceptance
            $env:DATABASE_URL = $PreviousDatabaseUrl
        }
        return
    }
    & $Python -B scripts/run_evidence_test.py check
    if ($LASTEXITCODE -ne 0) { throw 'Isolated test configuration check failed.' }
    $ProbeOutput = 'experiments/harness-v1/windows-sandbox-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
    & $Python -B scripts/run_evidence_test.py harness self-sandbox-check --output $ProbeOutput
    if ($LASTEXITCODE -ne 0) { throw 'Windows sandbox probe failed.' }
    if ($CheckModelLogin) {
        & $Python -B scripts/run_evidence_test.py harness codex-probe
        if ($LASTEXITCODE -ne 0) { throw 'Codex login check failed (no model calls).' }
    }
} finally {
    $env:PYTHONUTF8 = $PreviousPythonUtf8
    Pop-Location
}
