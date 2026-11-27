<#
.SYNOPSIS
Inspect, export, import, or compare portable GPU validation evidence.
.DESCRIPTION
Compare is read-only and needs the full runtime pin from the handoff receipt.
It does not import evidence or approve migration. In a code-only checkout,
pass the Python executable from an existing project environment to compare.
.EXAMPLE
.\scripts\evidence.ps1 -Action compare @Bundles

Compare the three selected bundles against the exact prepared runtime.
#>
param(
    [ValidateSet("status", "export", "import", "compare")]
    [string]$Action = "status",

    [ValidateSet("3080", "4090", "5090")]
    [string]$Profile,

    [string]$Path,

    [string]$Output,

    [string]$Candidate,

    [string]$Workload,

    [string]$ExpectedCommit,

    [string]$Evidence3080,

    [string]$Evidence4090,

    [string]$Evidence5090,

    [string]$Baseline,

    [switch]$Apply,

    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if ($Action -eq "export" -and [string]::IsNullOrWhiteSpace($Profile)) {
    throw "Profile is required for export."
}
if ($Action -eq "import" -and [string]::IsNullOrWhiteSpace($Path)) {
    throw "Path is required for import."
}
if ($Apply -and $Action -ne "import") {
    throw "Apply is valid only with -Action import."
}
if ($Action -eq "compare" -and [string]::IsNullOrWhiteSpace($ExpectedCommit)) {
    throw "ExpectedCommit is required for compare. Use the full runtime pin from the handoff."
}
if (($ExpectedCommit -or $Evidence3080 -or $Evidence4090 -or $Evidence5090 -or $Baseline) -and
    $Action -ne "compare") {
    throw "ExpectedCommit, Evidence3080, Evidence4090, Evidence5090, and Baseline require compare."
}
if ($PSBoundParameters.ContainsKey("PythonExecutable")) {
    if ($Action -ne "compare") {
        throw "PythonExecutable is valid only with -Action compare."
    }
    if ([string]::IsNullOrWhiteSpace($PythonExecutable) -or
        -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw "PythonExecutable must point to an existing project Python executable."
    }
    $Python = (Resolve-Path -LiteralPath $PythonExecutable).ProviderPath
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    if ($Action -eq "compare") {
        throw "Virtual environment is missing. Pass -PythonExecutable from an existing project environment."
    }
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}

$Arguments = @((Join-Path $Root "main.py"), "evidence", $Action)
if ($Action -eq "export") {
    $Arguments += @("--profile", $Profile)
    foreach ($Item in @{ Output = $Output; Candidate = $Candidate; Workload = $Workload }.GetEnumerator()) {
        if (-not [string]::IsNullOrWhiteSpace($Item.Value)) {
            $Arguments += @("--$($Item.Key.ToLower())", $Item.Value)
        }
    }
}
elseif ($Action -eq "import") {
    $Arguments += @("--path", $Path)
    if ($Apply) {
        $Arguments += "--yes"
    }
}
elseif ($Action -eq "compare") {
    $Arguments = @("-B") + $Arguments + @("--expected-commit", $ExpectedCommit)
    foreach ($Item in @{
        "evidence-3080" = $Evidence3080
        "evidence-4090" = $Evidence4090
        "evidence-5090" = $Evidence5090
        baseline = $Baseline
    }.GetEnumerator()) {
        if (-not [string]::IsNullOrWhiteSpace($Item.Value)) {
            $Arguments += @("--$($Item.Key)", $Item.Value)
        }
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
