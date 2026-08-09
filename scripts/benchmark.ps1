param(
    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [switch]$Quick,

    [switch]$NoWrite,

    [switch]$Real,

    [string]$Crawl = "CC-MAIN-2014-15",

    [ValidateRange(1, 10)]
    [int]$Sources = 5,

    [int[]]$WorkerCount,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$Arguments = @((Join-Path $Root "main.py"), "benchmark", "--profile", $Profile)
if ($Quick) {
    $Arguments += "--quick"
}
if ($NoWrite) {
    $Arguments += "--no-write"
}
if ($Real) {
    $Arguments += @("--real", "--crawl", $Crawl, "--sources", $Sources)
    foreach ($Count in $WorkerCount) {
        if ($Count -le 0) {
            throw "WorkerCount values must be positive."
        }
        $Arguments += @("--worker-count", $Count)
    }
    if ($Apply) {
        $Arguments += "--apply"
    }
}
elseif ($Apply) {
    throw "Apply is only valid with -Real."
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
