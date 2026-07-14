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

$env:HOMETOWN_XR_PROFILE = $Profile
& $Python (Join-Path $Root "main.py") @CommandArgs
exit $LASTEXITCODE

