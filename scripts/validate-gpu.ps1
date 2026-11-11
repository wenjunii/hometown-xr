<#
.SYNOPSIS
Preview or run the pinned GPU candidate in a new local checkout.
.DESCRIPTION
Use the prepared USB store after verifying its receipt and stopping project
writers. Preview is the default. Apply requires the matching physical GPU,
Git/Git LFS, and a nonexistent destination outside the store. The runtime stays
pinned to 430a82aa; FP16, dependency audits, model limits, and the five-source,
seven-worker benchmark are unchanged. Successful evidence is hash-checked into
a new validation/returns folder. This never approves a migration or exports
the validation checkpoint as production data. The original USB kit is untouched.
.EXAMPLE
.\scripts\validate-gpu.ps1 -Store E:\HometownXR-store -Profile 4090

Preview the pinned checkout and evidence path without installing anything.
.EXAMPLE
.\scripts\validate-gpu.ps1 -Store E:\HometownXR-store -Profile 4090 -Apply

Validate on the actual 4090 and return its evidence after every check passes.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Store,
    [Parameter(Mandatory = $true)]
    [ValidateSet('4090', '5090')]
    [string]$Profile,
    [string]$Destination,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$Commit = '430a82aa88a402df71b337b43ec5f68ac78921ef'
$Store = [IO.Path]::GetFullPath($Store)
if (-not $Destination) {
    $Destination = Join-Path $HOME "hometown-xr-$Profile-430a82aa-validation-20261110"
}
$Destination = [IO.Path]::GetFullPath($Destination)
if ($Destination.Equals($Store, [StringComparison]::OrdinalIgnoreCase) -or
    $Destination.StartsWith($Store + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The validation checkout must not be inside the USB research-data store.'
}
if (Test-Path -LiteralPath $Destination) {
    throw 'The destination already exists. Preserve it and choose a new local folder.'
}
if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($Destination)) -PathType Container)) {
    throw 'The destination parent must already exist.'
}
$Receipt = Get-Content -LiteralPath (Join-Path $Store 'PREPARATION-2026-11-10.json') -Raw | ConvertFrom-Json
$Control = Get-Content -LiteralPath (Join-Path $Store 'control.json') -Raw | ConvertFrom-Json
if (-not $Receipt.store_id -or -not $Receipt.snapshot_id -or $Control.store_id -ne $Receipt.store_id -or
    $Control.snapshot_id -ne $Receipt.snapshot_id -or $Receipt.candidate.git_commit -ne $Commit) {
    throw 'The store identity, snapshot, or prepared runtime does not match the receipt.'
}
$StoreId = $Control.store_id
$SnapshotId = $Control.snapshot_id
$Evidence = Join-Path $Destination "data\evidence-outbox\profile-evidence-$Profile.json"
if (-not $Apply) {
    [pscustomobject]@{
        action = 'isolated_gpu_validation'
        applied = $false
        profile = $Profile
        destination = $Destination
        candidate_commit = $Commit
        required_precision = 'fp16'
        evidence = $Evidence
        production_data_changed = $false
    } | ConvertTo-Json
    exit 0
}

if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { throw 'Install a compatible NVIDIA driver first.' }
# Do not pipe the native command into Select-Object: early pipeline termination
# can leave LASTEXITCODE unset even when the matching GPU name was returned.
$GpuOutput = @(nvidia-smi --query-gpu=name --format=csv,noheader)
$GpuExitCode = $LASTEXITCODE
$Gpu = $GpuOutput | Select-Object -First 1
if ($null -eq $GpuExitCode -or $GpuExitCode -ne 0 -or
    [string]::IsNullOrWhiteSpace($Gpu) -or $Gpu -notmatch [regex]::Escape($Profile)) {
    throw "GPU precheck failed: query exit '$GpuExitCode', GPU '$Gpu', requested $Profile. Nothing was cloned or installed."
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'Git and Git LFS are required.' }
git lfs version | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Install Git LFS before starting.' }

$PreviousNoCache = $env:PIP_NO_CACHE_DIR
try {
    $env:PIP_NO_CACHE_DIR = '1'
    git clone --single-branch --branch codex/security-runtime-refresh https://github.com/wenjunii/hometown-xr.git $Destination
    if ($LASTEXITCODE -ne 0) { throw 'Clone failed. Preserve any partial directory for inspection; do not run setup there.' }
    Push-Location $Destination
    try {
        git switch -c "codex/$Profile-validation-430a82aa-20261110" $Commit
        if ($LASTEXITCODE -ne 0) { throw 'The pinned candidate commit is unavailable; stop and refresh the preparation.' }
        $ActualCommit = git rev-parse HEAD
        if ($LASTEXITCODE -ne 0 -or $ActualCommit -ne $Commit) { throw 'The checkout is not at the required runtime pin.' }
        .\scripts\setup.ps1 -Profile $Profile -Dev
        if ($LASTEXITCODE -ne 0) { throw 'Setup failed. Do not bypass the failed check.' }
        .\scripts\validate-runtime.ps1 -Profile $Profile -Precision fp16
        if ($LASTEXITCODE -ne 0) { throw 'Validation failed. Keep its reports; do not resume production or approve the migration.' }
    }
    finally { Pop-Location }
}
finally { $env:PIP_NO_CACHE_DIR = $PreviousNoCache }

$Control = Get-Content -LiteralPath (Join-Path $Store 'control.json') -Raw | ConvertFrom-Json
if ($Control.store_id -ne $StoreId -or $Control.snapshot_id -ne $SnapshotId) {
    throw 'The USB store changed. Keep the evidence locally instead of copying it.'
}
$Return = Join-Path $Store ("validation\returns\$Profile-430a82aa-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
if (Test-Path -LiteralPath $Return) { throw 'The evidence return folder already exists; do not overwrite it.' }
New-Item -ItemType Directory -Path $Return | Out-Null
$Copy = Join-Path $Return "profile-evidence-$Profile.json"
Copy-Item -LiteralPath $Evidence -Destination $Copy
if ((Get-FileHash -LiteralPath $Evidence -Algorithm SHA256).Hash -ne
    (Get-FileHash -LiteralPath $Copy -Algorithm SHA256).Hash) { throw 'Evidence copy verification failed.' }
Write-Output "Validation evidence copied to $Copy"
Write-Output 'This is test evidence only. No production rollout or migration approval was performed.'
