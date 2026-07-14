param(
    [switch]$ApplyRefilter,

    [switch]$SkipParquet,

    [ValidateSet("none", "exact", "near")]
    [string]$Dedupe = "near",

    [ValidateRange(0, 64)]
    [int]$NearDistance = 3,

    [ValidateRange(1, 1000000)]
    [int]$DomainStoryCap = 100,

    [Nullable[double]]$SemanticThreshold = $null,

    [Nullable[int]]$NarrativeThreshold = $null
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Lock = Join-Path $Root "data\.crawler.lock"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if (Test-Path -LiteralPath $Lock) {
    throw "The crawler lock exists. Stop the crawler cleanly before refreshing results."
}

$ExitCode = 1
Push-Location $Root
try {
    if ($ApplyRefilter) {
        $Changes = git status --porcelain
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect the worktree before applying filters."
        }
        if ($Changes) {
            throw "The worktree is not clean. Run .\scripts\checkpoint.ps1 before applying filters."
        }
        Write-Output "Applying current filters to accepted JSONL output."
    }
    else {
        Write-Output "Dry-running current filters; accepted JSONL output will not be replaced."
    }

    $RefilterArgs = @((Join-Path $Root "refilter_output.py"))
    if (-not $ApplyRefilter) {
        $RefilterArgs += "--dry-run"
    }
    if ($null -ne $SemanticThreshold) {
        $RefilterArgs += @("--semantic-threshold", $SemanticThreshold.ToString([Globalization.CultureInfo]::InvariantCulture))
    }
    if ($null -ne $NarrativeThreshold) {
        $RefilterArgs += @("--narrative-threshold", $NarrativeThreshold.ToString([Globalization.CultureInfo]::InvariantCulture))
    }

    & $Python @RefilterArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Historical-result filtering failed with exit code $LASTEXITCODE."
    }

    if ($ApplyRefilter) {
        & $Python (Join-Path $Root "main.py") verify-output
        if ($LASTEXITCODE -ne 0) {
            throw "Applied output failed verification with exit code $LASTEXITCODE."
        }
    }

    if (-not $SkipParquet) {
        $ParquetArgs = @(
            (Join-Path $Root "main.py"),
            "parquet",
            "--dedupe", $Dedupe,
            "--domain-story-cap", $DomainStoryCap
        )
        if ($Dedupe -eq "near") {
            $ParquetArgs += @("--near-distance", $NearDistance)
        }
        $ParquetOutput = & $Python @ParquetArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Canonical Parquet refresh failed with exit code $LASTEXITCODE."
        }
        $Manifest = (($ParquetOutput | Out-String) | ConvertFrom-Json)
        Write-Output (
            ("Canonical refresh: {0} captures -> {1} stories " +
            "({2} exact duplicates, {3} near duplicates); {4} within domain cap.") -f
            $Manifest.input_captures,
            $Manifest.rows,
            $Manifest.duplicates.exact,
            $Manifest.duplicates.near,
            $Manifest.quality.stories_within_domain_cap
        )
    }

    $ExitCode = 0
}
finally {
    Pop-Location
}

exit $ExitCode
