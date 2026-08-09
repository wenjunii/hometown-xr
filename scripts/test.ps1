$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

Push-Location $Root
try {
    & $Python (Join-Path $Root "credential_guard.py") --scope worktree
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Get-ChildItem (Join-Path $Root "scripts") -Filter *.ps1 | ForEach-Object {
        $Tokens = $null
        $ParseErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile(
            $_.FullName,
            [ref]$Tokens,
            [ref]$ParseErrors
        ) | Out-Null
        if ($ParseErrors.Count -gt 0) {
            $ParseErrors | ForEach-Object { Write-Error $_ }
            throw "PowerShell parse failed: $($_.FullName)"
        }
    }
    & $Python -m pytest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m compileall -q -x "[\\/](\.venv|data)[\\/]" .
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    Pop-Location
}
