[CmdletBinding()]
param(
    [string]$EnvFile,
    [string]$ImageTag,
    [string]$BaseImageTag,
    [string]$AptDebianMirror,
    [string]$AptSecurityMirror,
    [string]$NpmRegistry,
    [string]$PipIndexUrl,
    [string]$HfEndpoint,
    [string]$DoclingModelGitBase,
    [string]$LocalModelCacheContext,
    [string]$SeedApiImage,
    [string]$SeedWebImage,
    [switch]$RebuildBase,
    [switch]$SkipWeb
)

Write-Verbose ("Bound parameters: " + ($PSBoundParameters | ConvertTo-Json -Compress))

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$EnvValues = @{}
$ResolvedEnvFile = $null

function Read-EnvValues([string]$Path) {
    # 只读取普通 KEY=VALUE，避免在构建脚本中执行环境文件内容。
    $values = @{}
    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]*)=(.*)$') {
            $values[$matches[1].Trim()] = $matches[2].Trim()
        }
    }
    return $values
}

function Resolve-Setting([string]$ExplicitValue, [string]$Name, [string]$DefaultValue) {
    # 命令行显式值优先，其次使用部署环境，最后使用经过验证的默认值。
    Write-Verbose "Resolving $Name with explicit value <$ExplicitValue>"
    if (-not [string]::IsNullOrWhiteSpace($ExplicitValue)) { return $ExplicitValue }
    if ($EnvValues.ContainsKey($Name) -and -not [string]::IsNullOrWhiteSpace($EnvValues[$Name])) {
        return $EnvValues[$Name]
    }
    return $DefaultValue
}

function Assert-LastExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

function Test-DockerImage([string]$Image) {
    # image ls 在镜像不存在时正常返回空结果，避免 Docker 29 的 inspect 输出兼容差异。
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        $listOutput = docker image ls --quiet --filter "reference=$Image" 2>&1
        $listExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $listMessage = ($listOutput | Out-String).Trim()
    if ($listExitCode -ne 0) { throw "Docker could not list image '$Image': $listMessage" }
    return -not [string]::IsNullOrWhiteSpace($listMessage)
}

function Assert-SeedImageHasReusableRuntime([string]$Image) {
    # 种子只提供系统依赖和模型；业务目录会在平铺前整体删除。
    $pythonCheck = "import json,shutil; from pathlib import Path; p=Path('/opt/file-agent/models/model-manifest.json'); d=json.loads(p.read_text()) if p.is_file() else {}; required={'docling','paddleocr','pp_structure_v3','paddleocr_vl','document_embedding'}; components=d.get('components',{}); ready=all((components.get(name) or {}).get('status') == 'ready' for name in required); raise SystemExit(0 if ready and shutil.which('soffice') else 1)"
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "SilentlyContinue"
        docker run --rm --entrypoint python $Image -c $pythonCheck *> $null
        $checkExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($checkExitCode -ne 0) {
        throw "The seed API image does not contain the verified runtime and model manifest required for reuse: $Image"
    }
}

function Assert-ImageHasNoRuntimeData([string]$Image) {
    # 平铺后的基础镜像必须真正不包含旧业务文件，而不是只通过父层 whiteout 隐藏。
    $pythonCheck = "from pathlib import Path; roots=[Path('/app'),Path('/data')]; raise SystemExit(1 if any(p.exists() and any(x.is_file() for x in p.rglob('*')) for p in roots) else 0)"
    docker run --rm --entrypoint python $Image -c $pythonCheck *> $null
    Assert-LastExitCode "The flattened runtime base still contains application or runtime data: $Image"
}

function Assert-SeedWebImageHasCaddy([string]$Image) {
    # 复用镜像只负责提供 Caddy，前端静态文件会在新镜像层中整体替换。
    docker run --rm --entrypoint caddy $Image version *> $null
    Assert-LastExitCode "The seed Web image does not contain a working Caddy runtime: $Image"
}

if (-not [string]::IsNullOrWhiteSpace($EnvFile)) {
    $ResolvedEnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
    $EnvValues = Read-EnvValues -Path $ResolvedEnvFile
}

Write-Verbose "Raw values before resolution: code=$ImageTag base=$BaseImageTag seed=$SeedApiImage"
$ImageTag = Resolve-Setting $ImageTag "FILE_AGENT_IMAGE_TAG" "20260904"
$BaseImageTag = Resolve-Setting $BaseImageTag "FILE_AGENT_BASE_IMAGE_TAG" "20260904-v1"
$AptDebianMirror = Resolve-Setting $AptDebianMirror "APT_DEBIAN_MIRROR" "https://mirrors.aliyun.com/debian"
$AptSecurityMirror = Resolve-Setting $AptSecurityMirror "APT_SECURITY_MIRROR" "https://mirrors.aliyun.com/debian-security"
$NpmRegistry = Resolve-Setting $NpmRegistry "NPM_REGISTRY" "https://registry.npmmirror.com"
$PipIndexUrl = Resolve-Setting $PipIndexUrl "PIP_INDEX_URL" "https://mirrors.aliyun.com/pypi/simple"
$HfEndpoint = Resolve-Setting $HfEndpoint "HF_ENDPOINT" "https://hf-mirror.com"
$DoclingModelGitBase = Resolve-Setting $DoclingModelGitBase "DOCLING_MODEL_GIT_BASE" "https://hf-mirror.com"
$SeedApiImage = Resolve-Setting $SeedApiImage "FILE_AGENT_BASE_SEED_IMAGE" ""
$SeedWebImage = Resolve-Setting $SeedWebImage "FILE_AGENT_WEB_SEED_IMAGE" ""
if ([string]::IsNullOrWhiteSpace($SeedWebImage) -and $SeedApiImage -match '^file-agent-api-full-cpu:(.+)$') {
    # API 与 Web 离线包使用相同标签时，自动找到配套的本地 Web 运行时镜像。
    $SeedWebImage = "file-agent-web:$($matches[1])"
}
Write-Verbose "Resolved image tags: code=$ImageTag base=$BaseImageTag apiSeed=$SeedApiImage webSeed=$SeedWebImage"

$cacheContextFromEnv = $false
if ([string]::IsNullOrWhiteSpace($LocalModelCacheContext) -and $EnvValues.ContainsKey("LOCAL_MODEL_CACHE_CONTEXT")) {
    $LocalModelCacheContext = $EnvValues["LOCAL_MODEL_CACHE_CONTEXT"]
    $cacheContextFromEnv = $true
}
if ([string]::IsNullOrWhiteSpace($LocalModelCacheContext)) {
    $LocalModelCacheContext = Join-Path $DeployDir "empty-model-cache"
} elseif (-not [System.IO.Path]::IsPathRooted($LocalModelCacheContext)) {
    $relativeRoot = if ($cacheContextFromEnv -and $null -ne $ResolvedEnvFile) {
        Split-Path -Parent $ResolvedEnvFile
    } else {
        (Get-Location).Path
    }
    $LocalModelCacheContext = Join-Path $relativeRoot $LocalModelCacheContext
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}
docker info | Out-Null
Assert-LastExitCode "Docker Desktop is not running or is not accessible."

$BaseImage = "file-agent-api-runtime-base:$BaseImageTag"
$ApiImage = "file-agent-api-full-cpu:$ImageTag"
$WebImage = "file-agent-web:$ImageTag"

$baseExists = Test-DockerImage $BaseImage
$seedExists = $false
if (-not $RebuildBase -and -not $baseExists -and -not [string]::IsNullOrWhiteSpace($SeedApiImage)) {
    $seedExists = Test-DockerImage $SeedApiImage
    if (-not $seedExists) { throw "The seed API image does not exist: $SeedApiImage" }
}

Push-Location $ProjectRoot
try {
    if (-not $RebuildBase -and -not $baseExists -and $seedExists) {
        Assert-SeedImageHasReusableRuntime $SeedApiImage
        Write-Host "Flattening a clean runtime base from the existing API image: $SeedApiImage -> $BaseImage" -ForegroundColor Cyan
        docker build `
            --build-arg "SEED_API_IMAGE=$SeedApiImage" `
            --tag $BaseImage `
            --file deploy/Dockerfile.api-base-reuse .
        Assert-LastExitCode "Creating the runtime base from the existing API image failed."
        Assert-ImageHasNoRuntimeData $BaseImage
        $baseExists = $true
    }

    if ($RebuildBase -or -not $baseExists) {
        # 只有真正构建重型基础镜像时才要求本地模型缓存目录存在。
        # Docker Desktop 29 的命名构建上下文要求 Windows 绝对路径使用正斜杠。
        if (-not (Test-Path -LiteralPath $LocalModelCacheContext -PathType Container)) {
            throw "The local model cache context does not exist: $LocalModelCacheContext"
        }
        # 使用 .NET 规范化避免 Windows PowerShell 对 PathInfo.ProviderPath 的环境差异。
        $windowsModelCacheContext = [System.IO.Path]::GetFullPath($LocalModelCacheContext)
        $dockerModelCacheContext = $windowsModelCacheContext.Replace([char]92, [char]47)
        if ([string]::IsNullOrWhiteSpace($dockerModelCacheContext)) {
            throw "The local model cache context resolved to an empty path: $LocalModelCacheContext"
        }
        Write-Verbose "Resolved local model cache context: $dockerModelCacheContext"
        Write-Host "Building the runtime base image: $BaseImage" -ForegroundColor Cyan
        docker build `
            --build-context "local-model-cache=$dockerModelCacheContext" `
            --build-arg MODEL_PRELOAD=true `
            --build-arg "APT_DEBIAN_MIRROR=$AptDebianMirror" `
            --build-arg "APT_SECURITY_MIRROR=$AptSecurityMirror" `
            --build-arg "NPM_REGISTRY=$NpmRegistry" `
            --build-arg "PIP_INDEX_URL=$PipIndexUrl" `
            --build-arg "HF_ENDPOINT=$HfEndpoint" `
            --build-arg "DOCLING_MODEL_GIT_BASE=$DoclingModelGitBase" `
            --tag $BaseImage `
            --file deploy/Dockerfile.api-base .
        Assert-LastExitCode "The runtime base image build failed."
    } else {
        Write-Host "Reusing the runtime base image: $BaseImage" -ForegroundColor Green
    }

    Write-Host "Building the latest API code image: $ApiImage" -ForegroundColor Cyan
    docker build `
        --build-arg "API_RUNTIME_BASE_IMAGE=$BaseImage" `
        --tag $ApiImage `
        --file deploy/Dockerfile.api .
    Assert-LastExitCode "The API code image build failed."

    if (-not $SkipWeb) {
        $seedWebExists = -not [string]::IsNullOrWhiteSpace($SeedWebImage) -and (Test-DockerImage $SeedWebImage)
        if ($seedWebExists) {
            Assert-SeedWebImageHasCaddy $SeedWebImage
            if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
                throw "npm was not found. It is required to compile the latest Web source for offline runtime reuse."
            }
            Write-Host "Compiling the latest Web source with local node_modules." -ForegroundColor Cyan
            $previousViteApiBaseUrl = $env:VITE_API_BASE_URL
            Push-Location (Join-Path $ProjectRoot "apps/web")
            try {
                $env:VITE_API_BASE_URL = "/api"
                npm run build
                Assert-LastExitCode "The local Web source build failed."
            } finally {
                $env:VITE_API_BASE_URL = $previousViteApiBaseUrl
                Pop-Location
            }
            $webDistPath = Join-Path $ProjectRoot "apps/web/dist"
            if (-not (Test-Path -LiteralPath $webDistPath -PathType Container)) {
                throw "The local Web build did not create its dist directory: $webDistPath"
            }
            $dockerWebDistContext = ([System.IO.Path]::GetFullPath($webDistPath)).Replace([char]92, [char]47)
            Write-Host "Building the latest Web image from the local Caddy runtime: $SeedWebImage -> $WebImage" -ForegroundColor Cyan
            docker build `
                --build-context "web-dist=$dockerWebDistContext" `
                --build-arg "SEED_WEB_IMAGE=$SeedWebImage" `
                --tag $WebImage `
                --file deploy/Dockerfile.web-reuse .
            Assert-LastExitCode "The offline Web code image build failed."
        } else {
            Write-Host "Building the latest Web image: $WebImage" -ForegroundColor Cyan
            docker build `
                --build-arg VITE_API_BASE_URL=/api `
                --build-arg "NPM_REGISTRY=$NpmRegistry" `
                --tag $WebImage `
                --file deploy/Dockerfile.web .
            Assert-LastExitCode "The Web image build failed."
        }
    }

    Write-Host "Layered image build completed. Future source-only updates rebuild only the code layer." -ForegroundColor Green
    Write-Host "Base: $BaseImage" -ForegroundColor Green
    Write-Host "API:  $ApiImage" -ForegroundColor Green
    if (-not $SkipWeb) { Write-Host "Web:  $WebImage" -ForegroundColor Green }
} finally {
    Pop-Location
}
