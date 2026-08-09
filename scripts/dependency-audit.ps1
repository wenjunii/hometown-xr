param(
    [string]$Requirements = "requirements.txt"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 -Dev first."
}

Push-Location $Root
try {
    & $Python (Join-Path $Root "dependency_profiles.py") --check
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency profile validation failed with exit code $LASTEXITCODE."
    }
    & $Python (Join-Path $Root "dependency_audit.py") --requirements $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency vulnerability policy failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
