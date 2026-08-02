param(
    [ValidateSet("status", "plan", "campaign", "sample", "annotate", "report", "replay", "undo", "multilingual", "serve")]
    [string]$Action = "status",

    [ValidateRange(1, 1000000)]
    [int]$Size = 400,

    [string]$Language,

    [Nullable[int]]$Limit = $null,

    [ValidateSet("accepted", "rejected")]
    [string]$Prediction,

    [ValidateSet("all", "tuning", "holdout")]
    [string]$Split,

    [string]$SampleId,

    [string]$Annotator,

    [switch]$Relabel,

    [switch]$Quick,

    [string]$HostName = "127.0.0.1",

    [ValidateRange(1, 65535)]
    [int]$Port = 8765,

    [switch]$OpenBrowser
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

$Arguments = @((Join-Path $Root "main.py"), "evaluation", $Action)
if ($Action -eq "sample") {
    $Arguments += @("--size", $Size)
}
elseif ($Action -eq "annotate") {
    if (-not [string]::IsNullOrWhiteSpace($Language)) {
        $Arguments += @("--language", $Language)
    }
    if ($null -ne $Limit) {
        $Arguments += @("--limit", $Limit)
    }
    if (-not [string]::IsNullOrWhiteSpace($Prediction)) {
        $Arguments += @("--prediction", $Prediction)
    }
    if (-not [string]::IsNullOrWhiteSpace($Split)) {
        $Arguments += @("--split", $Split)
    }
    if (-not [string]::IsNullOrWhiteSpace($SampleId)) {
        $Arguments += @("--sample-id", $SampleId)
    }
    if (-not [string]::IsNullOrWhiteSpace($Annotator)) {
        $Arguments += @("--annotator", $Annotator)
    }
    if ($Relabel) {
        $Arguments += "--relabel"
    }
    if ($Quick) {
        $Arguments += "--quick"
    }
}
elseif ($Action -eq "undo" -and -not [string]::IsNullOrWhiteSpace($SampleId)) {
    $Arguments += @("--sample-id", $SampleId)
}
elseif ($Action -eq "serve") {
    $Arguments += @("--host", $HostName, "--port", $Port)
    if ($OpenBrowser) {
        $Arguments += "--open-browser"
    }
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
