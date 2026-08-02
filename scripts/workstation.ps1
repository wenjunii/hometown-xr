param(
    [ValidateSet("status", "preflight", "claim", "release")]
    [string]$Action = "status",

    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [ValidateRange(1, 168)]
    [int]$LeaseHours = 2,

    [switch]$IncludePreflight,

    [switch]$ForceRecovery,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -in @("claim", "release") -and -not $Apply) {
    throw "$Action changes shared workstation ownership; pass -Apply deliberately."
}
if ($Apply -and $Action -notin @("claim", "release")) {
    throw "Apply is valid only with claim or release."
}
if ($IncludePreflight -and $Action -ne "status") {
    throw "IncludePreflight is valid only with status."
}
if ($ForceRecovery -and $Action -ne "claim") {
    throw "ForceRecovery is valid only with claim."
}

$Arguments = @(
    (Join-Path $Root "main.py"),
    "workstation",
    $Action,
    "--profile",
    $Profile
)
if ($Action -eq "claim") {
    $Arguments += @("--lease-hours", $LeaseHours, "--yes")
    if ($ForceRecovery) {
        $Arguments += "--force-recovery"
    }
}
elseif ($Action -eq "release") {
    $Arguments += "--yes"
}
elseif ($IncludePreflight) {
    $Arguments += "--preflight"
}

Push-Location $Root
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
