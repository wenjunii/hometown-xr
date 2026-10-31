param(
    [ValidateSet("staged", "tracked", "worktree")]
    [string]$Scope = "worktree"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PrefixArguments = @()
if (-not (Test-Path -LiteralPath $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = "py"
        $PrefixArguments = @("-3")
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = "python"
    }
    else {
        throw "Install Python 3.10+ to scan credentials. No GPU environment or package installation is required."
    }
}

Push-Location $Root
try {
    & $Python @PrefixArguments -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
    if ($LASTEXITCODE -ne 0) {
        throw "Credential scanning requires a working Python 3.10+ interpreter. No files were staged or changed."
    }
    & $Python @PrefixArguments (Join-Path $Root "credential_guard.py") --scope $Scope
    if ($LASTEXITCODE -ne 0) {
        throw "Credential scan failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
