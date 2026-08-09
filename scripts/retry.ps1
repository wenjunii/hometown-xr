param(
    [string]$Crawl,

    [switch]$All,

    [ValidateRange(1, 1000000)]
    [int]$Limit = 100,

    [ValidateSet(
        "connection", "http_404", "http_429", "http_500", "http_502",
        "http_503", "http_504", "inference", "other", "output",
        "process_pool", "timeout"
    )]
    [string]$Category,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($All -and -not [string]::IsNullOrWhiteSpace($Crawl)) {
    throw "Choose either -All or -Crawl, not both."
}

$ExitCode = 1
Push-Location $Root
try {
    if (-not $Apply) {
        $Arguments = @((Join-Path $Root "main.py"), "failures")
        if (-not [string]::IsNullOrWhiteSpace($Crawl)) {
            $Arguments += @("--crawl", $Crawl)
        }
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
        Write-Output "Dry run only. Add -Apply to reset a bounded retry batch."
        $ExitCode = 0
    }
    else {
        $Arguments = @((Join-Path $Root "main.py"), "retry", "--limit", $Limit)
        if ($All) {
            $Arguments += "--all"
        }
        elseif (-not [string]::IsNullOrWhiteSpace($Crawl)) {
            $Arguments += @("--crawl", $Crawl)
        }
        if (-not [string]::IsNullOrWhiteSpace($Category)) {
            $Arguments += @("--category", $Category)
        }
        & $Python @Arguments
        $ExitCode = $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

exit $ExitCode
