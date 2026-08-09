param(
    [ValidateSet("plan", "run", "evidence", "adopt")]
    [string]$Action = "plan",

    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [string[]]$Category,

    [ValidateRange(1, 100)]
    [int]$PerCategory = 5,

    [string]$Report,

    [Nullable[int]]$Workers = $null,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -in @("run", "adopt") -and -not $Apply) {
    throw "$Action changes operational state; pass -Apply after reviewing evidence."
}
if ($Action -in @("evidence", "adopt") -and [string]::IsNullOrWhiteSpace($Report)) {
    throw "Report is required for evidence or adoption."
}

$Arguments = @((Join-Path $Root "main.py"), "recovery-campaign", $Action)
if ($Action -in @("plan", "run")) {
    $Arguments += @("--profile", $Profile, "--per-category", $PerCategory)
    foreach ($Name in $Category) {
        $Arguments += @("--category", $Name)
    }
    if ($null -ne $Workers) {
        if ($Workers -le 0) { throw "Workers must be positive." }
        $Arguments += @("--workers", $Workers)
    }
}
else {
    $Arguments += @("--report", $Report)
}
if ($Apply -and $Action -in @("run", "adopt")) {
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
