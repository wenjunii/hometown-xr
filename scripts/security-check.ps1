param(
    [ValidateSet("staged", "tracked", "worktree")]
    [string]$Scope = "worktree"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

Push-Location $Root
try {
    & $Python (Join-Path $Root "credential_guard.py") --scope $Scope
    if ($LASTEXITCODE -ne 0) {
        throw "Credential scan failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
