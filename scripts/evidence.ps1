param(
    [ValidateSet("status", "export", "import")]
    [string]$Action = "status",

    [ValidateSet("3080", "4090", "5090")]
    [string]$Profile,

    [string]$Path,

    [string]$Output,

    [string]$Candidate,

    [string]$Workload,

    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -eq "export" -and [string]::IsNullOrWhiteSpace($Profile)) {
    throw "Profile is required for export."
}
if ($Action -eq "import" -and [string]::IsNullOrWhiteSpace($Path)) {
    throw "Path is required for import."
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
