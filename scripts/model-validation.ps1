param(
    [ValidateSet("capture", "compare")]
    [string]$Action = "compare",

    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [string]$Annotations,

    [string]$Baseline,

    [string]$Candidate,

    [string]$Output,

    [Nullable[int]]$Limit = $null,

    [switch]$AsBaseline
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($null -ne $Limit -and $Limit -le 0) {
    throw "Limit must be positive."
}
if ($AsBaseline -and $Action -ne "capture") {
    throw "AsBaseline is valid only with -Action capture."
}
if ($AsBaseline -and $Profile -eq "auto") {
    throw "Select an explicit profile when capturing the tracked baseline."
}

$BaselinePath = if ([string]::IsNullOrWhiteSpace($Baseline)) {
    Join-Path $Root "data\evaluation\model-baseline.json"
}
else {
    $Baseline
}
$CandidatePath = if ([string]::IsNullOrWhiteSpace($Candidate)) {
    Join-Path $Root "data\evaluation\model-candidate-$Profile.json"
}
else {
    $Candidate
}

if ($Action -eq "capture") {
    $CapturePath = if (-not [string]::IsNullOrWhiteSpace($Output)) {
        $Output
    }
    elseif ($AsBaseline) {
        $BaselinePath
    }
    else {
        $CandidatePath
    }
    $Arguments = @(
        (Join-Path $Root "main.py"),
        "model-validation",
        "capture",
        "--profile", $Profile,
        "--output", $CapturePath
    )
    if (-not [string]::IsNullOrWhiteSpace($Annotations)) {
        $Arguments += @("--annotations", $Annotations)
    }
    if ($null -ne $Limit) {
        $Arguments += @("--limit", $Limit)
    }
}
else {
    $Arguments = @(
        (Join-Path $Root "main.py"),
        "model-validation",
        "compare",
        "--baseline", $BaselinePath,
        "--candidate", $CandidatePath
    )
    if (-not [string]::IsNullOrWhiteSpace($Output)) {
        $Arguments += @("--output", $Output)
    }
}

Push-Location $Root
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
