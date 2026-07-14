param(
    [ValidateSet("status", "sample", "annotate", "report", "replay")]
    [string]$Action = "status",

    [ValidateRange(1, 1000000)]
    [int]$Size = 400,

    [string]$Language,

    [Nullable[int]]$Limit = $null,

    [ValidateSet("accepted", "rejected")]
    [string]$Prediction
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
