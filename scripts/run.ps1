param(
    [ValidateSet("auto", "3080", "4090")]
    [string]$Profile = "auto",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$PreviousProfile = [Environment]::GetEnvironmentVariable("HOMETOWN_XR_PROFILE", "Process")
$ExitCode = 1
Push-Location $Root
try {
    $env:HOMETOWN_XR_PROFILE = $Profile
    $Database = Join-Path $Root "data\progress.db"
    $Archive = Join-Path $Root "data\checkpoints\progress.db.gz"
    if (-not (Test-Path -LiteralPath $Database) -and (Test-Path -LiteralPath $Archive)) {
        & $Python (Join-Path $Root "main.py") database restore
        if ($LASTEXITCODE -ne 0) {
            throw "Database checkpoint restore failed with exit code $LASTEXITCODE."
        }
    }
    & $Python (Join-Path $Root "main.py") @CommandArgs
    $ExitCode = $LASTEXITCODE
}
finally {
    if ($null -eq $PreviousProfile) {
        Remove-Item Env:HOMETOWN_XR_PROFILE -ErrorAction SilentlyContinue
    }
    else {
        $env:HOMETOWN_XR_PROFILE = $PreviousProfile
    }
    Pop-Location
}

exit $ExitCode
