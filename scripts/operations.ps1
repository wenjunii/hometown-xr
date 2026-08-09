param(
    [ValidateSet("status", "serve")]
    [string]$Action = "status",

    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [string]$HostName = "127.0.0.1",

    [ValidateRange(1, 65535)]
    [int]$Port = 8770,

    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
$Arguments = @(
    (Join-Path $Root "main.py"),
    "operations",
    $Action,
    "--profile",
    $Profile
)
if ($Action -eq "serve") {
    $Arguments += @("--host", $HostName, "--port", $Port)
    if ($OpenBrowser) {
        $Arguments += "--open-browser"
    }
}

$ExitCode = 1
Push-Location $Root
try {
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $ExitCode
