param(
    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$ExitCode = 1
Push-Location $Root
try {
    & $Python (Join-Path $Root "main.py") maintenance plan --profile $Profile
    $ExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $ExitCode
