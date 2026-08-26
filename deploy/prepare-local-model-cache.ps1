[CmdletBinding()]
param(
    [string]$OutputDirectory = ".\data\build-model-cache",
    [string]$PaddleXCacheRoot = "$env:USERPROFILE\.paddlex",
    [string]$HuggingFaceCacheRoot = "$env:USERPROFILE\.cache\huggingface",
    [string]$DocumentEmbeddingPath
)

$ErrorActionPreference = "Stop"
$DeployDir = $PSScriptRoot
$ProjectRoot = (Resolve-Path (Join-Path $DeployDir "..")).Path
$OutputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $ProjectRoot $OutputDirectory))
}
$AllowedPaddleModels = @(
    "PP-Chart2Table",
    "PP-DocBlockLayout",
    "PP-DocLayout_plus-L",
    "PP-DocLayoutV3",
    "PP-FormulaNet_plus-L",
    "PP-LCNet_x1_0_doc_ori",
    "PP-LCNet_x1_0_table_cls",
    "PP-LCNet_x1_0_textline_ori",
    "PP-OCRv4_server_seal_det",
    "PP-OCRv5_server_det",
    "PP-OCRv5_server_rec",
    "PP-OCRv6_medium_det",
    "PP-OCRv6_medium_rec",
    "PaddleOCR-VL-1.6-0.9B",
    "RT-DETR-L_wired_table_cell_det",
    "RT-DETR-L_wireless_table_cell_det",
    "SLANeXt_wired",
    "SLANet_plus",
    "UVDoc"
)
$Imported = New-Object System.Collections.Generic.List[string]
New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null

$PaddleSource = Join-Path $PaddleXCacheRoot "official_models"
$PaddleTarget = Join-Path $OutputRoot "paddlex\official_models"
foreach ($ModelName in $AllowedPaddleModels) {
    $Source = Join-Path $PaddleSource $ModelName
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { continue }
    $Target = Join-Path $PaddleTarget $ModelName
    if (Test-Path -LiteralPath $Target) {
        Write-Warning "Skipping existing immutable cache directory: $Target"
        continue
    }
    New-Item -ItemType Directory -Force -Path $PaddleTarget | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Target -Recurse
    $Imported.Add("paddlex/$ModelName")
}

$EmbeddingCacheName = "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
$HfSource = Join-Path (Join-Path $HuggingFaceCacheRoot "hub") $EmbeddingCacheName
if (Test-Path -LiteralPath $HfSource -PathType Container) {
    $HfTargetRoot = Join-Path $OutputRoot "huggingface\hub"
    $HfTarget = Join-Path $HfTargetRoot $EmbeddingCacheName
    if (-not (Test-Path -LiteralPath $HfTarget)) {
        New-Item -ItemType Directory -Force -Path $HfTargetRoot | Out-Null
        Copy-Item -LiteralPath $HfSource -Destination $HfTarget -Recurse
        $Imported.Add("huggingface/$EmbeddingCacheName")
    }
}

if (-not [string]::IsNullOrWhiteSpace($DocumentEmbeddingPath)) {
    $ResolvedEmbedding = (Resolve-Path -LiteralPath $DocumentEmbeddingPath).Path
    $EmbeddingTarget = Join-Path $OutputRoot "document-embedding"
    if (Test-Path -LiteralPath $EmbeddingTarget) {
        throw "Document embedding cache target already exists: $EmbeddingTarget"
    }
    Copy-Item -LiteralPath $ResolvedEmbedding -Destination $EmbeddingTarget -Recurse
    $Imported.Add("document-embedding")
}

@{
    schema_version = 1
    profile = "file-agent-approved-local-model-cache"
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    imported = @($Imported)
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $OutputRoot "cache-manifest.json") -Encoding UTF8

Write-Host "Approved local model cache context: $OutputRoot" -ForegroundColor Green
Write-Host "Imported items: $($Imported.Count)" -ForegroundColor Green
