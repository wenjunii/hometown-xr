param(
    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [switch]$Full,

    [switch]$Strict
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$Arguments = @((Join-Path $Root "main.py"), "health", "--profile", $Profile)
if ($Full) {
    $Arguments += "--full"
}
if ($Strict) {
    $Arguments += "--strict"
}

Push-Location $Root
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
