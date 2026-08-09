param(
    [ValidateSet("plan", "validate", "approve")]
    [string]$Action = "plan",

    [string]$Baseline,

    [string]$Candidate3080,

    [string]$Candidate4090,

    [string]$Candidate5090,

    [string]$Output,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -eq "approve" -and -not $Apply) {
    throw "Approval writes shared migration evidence; pass -Apply after validation."
}
if ($Apply -and $Action -ne "approve") {
    throw "Apply is valid only with -Action approve."
}
if (-not [string]::IsNullOrWhiteSpace($Output) -and $Action -ne "approve") {
    throw "Output is valid only with -Action approve."
}

$Arguments = @((Join-Path $Root "main.py"), "model-migration", $Action)
foreach ($Value in @{
    baseline = $Baseline
    "candidate-3080" = $Candidate3080
    "candidate-4090" = $Candidate4090
    "candidate-5090" = $Candidate5090
    output = $Output
}.GetEnumerator()) {
    if (-not [string]::IsNullOrWhiteSpace($Value.Value)) {
        $Arguments += @("--$($Value.Key)", $Value.Value)
    }
}
if ($Apply) {
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
