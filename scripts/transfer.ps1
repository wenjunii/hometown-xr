param(
    [ValidateSet("export", "import", "verify", "status", "check")]
    [string]$Action = "status",
    [string]$Store,
    [string]$Destination,
    [string]$Path,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
foreach ($Name in @("Store", "Destination", "Path")) {
    if ($PSBoundParameters.ContainsKey($Name) -and [string]::IsNullOrWhiteSpace($PSBoundParameters[$Name])) {
        throw "$Name must not be blank."
    }
}
if ($Store -and $Action -notin @("export", "import", "status")) {
    throw "Store is valid only with export, import, or status."
}
if ($Destination -and $Action -ne "import") {
    throw "Destination is valid only with import."
}
if ($Path -and $Action -ne "verify") {
    throw "Path is valid only with verify."
}
if ($Action -eq "import" -and (-not $Store -or -not $Destination)) {
    throw "Import requires -Store and -Destination (a new, nonexistent directory)."
}
if ($Action -eq "verify" -and -not $Path) {
    throw "Verify requires -Path."
}
if ($Apply -and $Action -notin @("export", "import")) {
    throw "Apply is valid only with export or import."
}
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$PrefixArguments = @()
if (-not (Test-Path -LiteralPath $Python)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $Python = "py"
        $PrefixArguments = @("-3")
    }
    elseif (Get-Command python -ErrorAction SilentlyContinue) {
        $Python = "python"
    }
    else {
        throw "Install Python 3.10+ to transfer snapshots. No GPU environment is required."
    }
}
$Arguments = @((Join-Path $Root "portable_transfer.py"), "transfer", $Action)
if ($Store) { $Arguments += @("--store", $Store) }
if ($Destination) { $Arguments += @("--destination", $Destination) }
if ($Path) { $Arguments += @("--path", $Path) }
if ($Apply) { $Arguments += "--yes" }
Push-Location $Root
try {
    & $Python @PrefixArguments @Arguments
    $Code = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $Code
