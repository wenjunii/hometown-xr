$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

Push-Location $Root
try {
    & $Python -m pytest
    & $Python -m ruff check .
    & $Python -m compileall -q -x "[\\/](\.venv|data)[\\/]" .
}
finally {
    Pop-Location
}
