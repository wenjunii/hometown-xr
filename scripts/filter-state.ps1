param(
    [ValidateSet("status", "reset-stale", "stamp-current")]
    [string]$Action = "status",

    [string]$Crawl,

    [ValidateRange(0, 1000000)]
    [int]$Limit = 0,

    [switch]$IncludeUnknown,

    [switch]$Apply,

    [Nullable[double]]$SemanticThreshold = $null,

    [Nullable[double]]$LanguageThreshold = $null
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($null -ne $SemanticThreshold -and ($SemanticThreshold -lt 0 -or $SemanticThreshold -gt 1)) {
    throw "SemanticThreshold must be between 0 and 1."
}
if ($null -ne $LanguageThreshold -and ($LanguageThreshold -lt 0 -or $LanguageThreshold -gt 1)) {
    throw "LanguageThreshold must be between 0 and 1."
}

if ($Action -eq "reset-stale") {
    if (-not $Apply) {
        throw "A stale-source reset requires -Apply. Run without it to keep checkpoint state unchanged."
    }
    if ([string]::IsNullOrWhiteSpace($Crawl)) {
        throw "A bounded stale-source reset requires -Crawl."
    }
    if ($Limit -le 0) {
        throw "A bounded stale-source reset requires a positive -Limit."
    }
}
elseif ($Action -eq "stamp-current" -and -not $Apply) {
    throw "Adopting historical signatures requires -Apply after an audit."
}

$Arguments = @((Join-Path $Root "main.py"), "filters")
if ($null -ne $SemanticThreshold) {
    $Arguments += @(
        "--threshold",
        $SemanticThreshold.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}
if ($null -ne $LanguageThreshold) {
    $Arguments += @(
        "--language-threshold",
        $LanguageThreshold.ToString([Globalization.CultureInfo]::InvariantCulture)
    )
}
$Arguments += $Action

if ($Action -eq "reset-stale") {
    $Arguments += @("--crawl", $Crawl, "--limit", $Limit, "--yes")
    if ($IncludeUnknown) {
        $Arguments += "--include-unknown"
    }
}
elseif ($Action -eq "stamp-current") {
    $Arguments += "--yes"
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
