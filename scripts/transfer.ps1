<#
.SYNOPSIS
Prepare, verify, export, or restore a local workstation snapshot.
.EXAMPLE
.\scripts\transfer.ps1 -Action prepare -Source C:\Projects\hometown-3080 -Store E:\HometownXR-store

Scan the source and verify its current snapshot without publishing it. Exit
code 1 means the handoff is not ready, for example if the original store is
disconnected. Preparation never starts a transfer.
.EXAMPLE
.\scripts\transfer.ps1 -Action import -Store E:\HometownXR-store -Destination C:\Projects\hometown-resume -ExpectedSha256 $Receipt.snapshot.sha256

Preview restoration of the exact archive verified in the preparation receipt.
Only repeat with -Apply after confirming the preview and approving the restore.
#>
param(
    [ValidateSet("prepare", "export", "import", "verify", "status", "check")]
    [string]$Action = "status",
    [string]$Store,
    [string]$Destination,
    [string]$Path,
    [string]$Source,
    [string]$ExpectedSha256,
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
foreach ($Name in @("Store", "Destination", "Path", "Source", "ExpectedSha256")) {
    if ($PSBoundParameters.ContainsKey($Name) -and [string]::IsNullOrWhiteSpace($PSBoundParameters[$Name])) {
        throw "$Name must not be blank."
    }
}
if ($Store -and $Action -notin @("prepare", "export", "import", "status")) {
    throw "Store is valid only with prepare, export, import, or status."
}
if ($Source -and $Action -ne "prepare") {
    throw "Source is valid only with prepare."
}
if ($ExpectedSha256 -and $Action -ne "import") {
    throw "ExpectedSha256 is valid only with import."
}
if ($ExpectedSha256 -and $ExpectedSha256 -cnotmatch '^[a-fA-F0-9]{64}$') {
    throw "ExpectedSha256 must contain exactly 64 hexadecimal digits."
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
$Arguments = @("-B", (Join-Path $Root "portable_transfer.py"), "transfer", $Action)
if ($Store) { $Arguments += @("--store", $Store) }
if ($Destination) { $Arguments += @("--destination", $Destination) }
if ($Path) { $Arguments += @("--path", $Path) }
if ($Source) { $Arguments += @("--source", $Source) }
if ($ExpectedSha256) { $Arguments += @("--expected-sha256", $ExpectedSha256) }
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
