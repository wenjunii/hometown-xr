param(
    [ValidateSet("plan", "run", "evidence", "adopt")]
    [string]$Action = "plan",

    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [Nullable[int]]$Workers = $null,

    [Nullable[int]]$CandidateBatchSize = $null,

    [Nullable[int]]$InferenceBatchSize = $null,

    [Nullable[int]]$EncodingBatchSize = $null,

    [ValidateSet("auto", "fp32", "fp16")]
    [string]$Precision = "auto",

    [switch]$NoAdaptiveBatching,

    [switch]$NoCache,

    [ValidateRange(1, 10)]
    [int]$PerCrawl = 2,

    [string[]]$Crawl,

    [string]$Report,

    [switch]$IncludeCurrent,

    [switch]$Apply,

    [ValidateRange(0.0, 1.0)]
    [double]$SampleRate = 0.05,

    [Nullable[double]]$SemanticThreshold = $null,

    [Nullable[double]]$LanguageThreshold = $null
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -in @("run", "adopt") -and -not $Apply) {
    throw "$Action changes operational state; pass -Apply after reviewing the plan or evidence."
}
if ($Action -in @("evidence", "adopt") -and [string]::IsNullOrWhiteSpace($Report)) {
    throw "Report is required for audit evidence or adoption."
}
if ($null -ne $SemanticThreshold -and ($SemanticThreshold -lt 0 -or $SemanticThreshold -gt 1)) {
    throw "SemanticThreshold must be between 0 and 1."
}
if ($null -ne $LanguageThreshold -and ($LanguageThreshold -lt 0 -or $LanguageThreshold -gt 1)) {
    throw "LanguageThreshold must be between 0 and 1."
}
foreach ($Override in @{
    Workers = $Workers
    CandidateBatchSize = $CandidateBatchSize
    InferenceBatchSize = $InferenceBatchSize
    EncodingBatchSize = $EncodingBatchSize
}.GetEnumerator()) {
    if ($null -ne $Override.Value -and $Override.Value -le 0) {
        throw "$($Override.Key) must be positive."
    }
}

$Arguments = @((Join-Path $Root "main.py"), "audit", $Action)
if ($Action -in @("plan", "run")) {
    $Arguments += @("--per-crawl", $PerCrawl)
}
else {
    $Arguments += @("--report", $Report)
}
foreach ($CrawlId in $Crawl) {
    $Arguments += @("--crawl", $CrawlId)
}
if ($IncludeCurrent) {
    $Arguments += "--include-current"
}
if ($null -ne $SemanticThreshold) {
    $Arguments += @(
        "--threshold",
        $SemanticThreshold.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}
if ($null -ne $LanguageThreshold) {
    $Arguments += @(
        "--language-threshold",
        $LanguageThreshold.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}
if ($Action -eq "run") {
    $Arguments += @(
        "--profile", $Profile,
        "--sample-rate", $SampleRate.ToString([Globalization.CultureInfo]::InvariantCulture),
        "--yes"
    )
    if ($null -ne $Workers) {
        $Arguments += @("--workers", $Workers)
    }
    if ($null -ne $CandidateBatchSize) {
        $Arguments += @("--candidate-batch-size", $CandidateBatchSize)
    }
    if ($null -ne $InferenceBatchSize) {
        $Arguments += @("--inference-batch-size", $InferenceBatchSize)
    }
    if ($null -ne $EncodingBatchSize) {
        $Arguments += @("--encoding-batch-size", $EncodingBatchSize)
    }
    if ($Precision -ne "auto") {
        $Arguments += @("--precision", $Precision)
    }
    if ($NoAdaptiveBatching) {
        $Arguments += "--no-adaptive-batching"
    }
    if ($NoCache) {
        $Arguments += "--no-cache"
    }
}
elseif ($Action -eq "adopt") {
    $Arguments += "--yes"
}

$ExitCode = 1
Push-Location $Root
try {
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $ExitCode
