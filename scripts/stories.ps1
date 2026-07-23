param(
    [ValidateSet("status", "plan", "enrich", "export")]
    [string]$Action = "status",

    [string[]]$Crawl,

    [string[]]$Source,

    [ValidateRange(1, 1000000)]
    [int]$Limit = 10,

    [switch]$All,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -eq "enrich" -and -not $Apply) {
    throw "Enrichment downloads Common Crawl source files; pass -Apply after reviewing the plan."
}
if ($Apply -and $Action -ne "enrich") {
    throw "Apply is valid only with -Action enrich."
}
if ($All -and ($Crawl -or $Source)) {
    throw "All cannot be combined with Crawl or Source."
}

$Arguments = @((Join-Path $Root "main.py"), "stories", $Action)
if ($Action -in @("status", "plan", "enrich")) {
    if ($All) {
        $Arguments += "--all"
    }
    else {
        $Arguments += @("--limit", $Limit)
    }
    foreach ($CrawlId in $Crawl) {
        $Arguments += @("--crawl", $CrawlId)
    }
    foreach ($SourceFile in $Source) {
        $Arguments += @("--source", $SourceFile)
    }
}
if ($Action -eq "enrich") {
    $Arguments += "--yes"
}

Push-Location $Root
try {
    & $Python @Arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
