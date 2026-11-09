<#
.SYNOPSIS
Transfer verified project state between workstations.
.DESCRIPTION
Uses folder snapshots when -Store or local folder configuration is present;
otherwise uses the original Git checkpoint workflow. Folder transfers preview
by default and need -Apply to write. Git pushes include durable research state,
so this script is not a code-only GitHub publishing command. Stop project and
review writers before a handoff; only one PC may write at a time.
.EXAMPLE
.\scripts\handoff.ps1 -Direction push -Store E:\HometownXR-store
Preview a verified local snapshot export without publishing it.
.EXAMPLE
.\scripts\handoff.ps1 -Direction push -Store E:\HometownXR-store -Apply
Publish the local snapshot to the selected store, not GitHub.
.EXAMPLE
.\scripts\handoff.ps1 -Direction pull -Store E:\HometownXR-store -Destination C:\Projects\hometown-resume -Apply
Receive into a new directory; an existing destination is never overwritten.
.NOTES
GitHub branch publication and USB/shared-folder data handoffs are separate.
See README.md for code-only sync and LOCAL_HANDOFF.md for snapshot operations.
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("pull", "push")]
    [string]$Direction,

    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [string]$Message = "chore: checkpoint extractor state",

    [string]$Store,

    [string]$Destination,

    [switch]$Apply,

    [switch]$ForceVacuum,

    [switch]$SkipVerify,

    [switch]$RefreshResults,

    [switch]$SkipStoryRefresh
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Join-Path $Root "data\.crawler.lock"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if ($Store -or (Test-Path -LiteralPath (Join-Path $Root "data\handoff.local.json"))) {
    if ($SkipVerify -or $RefreshResults -or $SkipStoryRefresh -or $ForceVacuum) {
        throw "Git-only switches are not supported for local snapshots. Snapshots are always verified."
    }
    $TransferArguments = @{ Action = $(if ($Direction -eq "push") { "export" } else { "import" }) }
    if ($Store) { $TransferArguments.Store = $Store }
    if ($Destination) { $TransferArguments.Destination = $Destination }
    if ($Direction -eq "pull" -and -not $Store) {
        $TransferArguments.Store = (Get-Content -LiteralPath (Join-Path $Root "data\handoff.local.json") -Raw | ConvertFrom-Json).store
    }
    & (Join-Path $PSScriptRoot "transfer.ps1") @TransferArguments -Apply:$Apply
    exit $LASTEXITCODE
}
if ($Apply -or $Destination) {
    throw "Apply and Destination are valid only for local-folder handoffs (-Store)."
}

if (Test-Path -LiteralPath $Lock) {
    throw "The crawler lock exists. Stop the crawler cleanly before a workstation handoff."
}

Push-Location $Root
try {
    if ($Direction -eq "pull") {
        if ($SkipStoryRefresh) {
            throw "SkipStoryRefresh is valid only with -Direction push."
        }
        if (-not (Test-Path -LiteralPath $Python)) {
            throw "Virtual environment is missing. Run .\scripts\setup.ps1 -Profile $Profile first."
        }
        $Changes = git status --porcelain
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect the worktree before handoff."
        }
        if ($Changes) {
            throw "The worktree is not clean. Commit or resolve local changes before handoff."
        }
        $Database = Join-Path $Root "data\progress.db"
        $Archive = Join-Path $Root "data\checkpoints\progress.db.gz"
        if ((Test-Path -LiteralPath $Database) -and (Test-Path -LiteralPath $Archive)) {
            & $Python (Join-Path $Root "main.py") database check
            if ($LASTEXITCODE -ne 0) {
                throw (
                    "The local database contains uncheckpointed work. " +
                    "Push a checkpoint first, or explicitly restore it before pulling."
                )
            }
        }
        $StoryPackCatalog = Join-Path $Root "data\stories\_packs\catalog.json.gz"
        if (Test-Path -LiteralPath $StoryPackCatalog) {
            $PackStatusText = & $Python (Join-Path $Root "main.py") stories pack-status
            if ($LASTEXITCODE -ne 0) {
                throw "Unable to compare local story fragments with their checkpoint."
            }
            $PackStatus = $PackStatusText | ConvertFrom-Json
            if (-not $PackStatus.safe_to_pull) {
                throw (
                    "Local story fragments contain uncheckpointed work. " +
                    "Push a checkpoint before pulling."
                )
            }
        }
        git pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            throw "Git pull failed with exit code $LASTEXITCODE."
        }
        git lfs pull
        if ($LASTEXITCODE -ne 0) {
            throw "Git LFS pull failed with exit code $LASTEXITCODE."
        }

        $StoryPackCatalog = Join-Path $Root "data\stories\_packs\catalog.json.gz"
        if (Test-Path -LiteralPath $StoryPackCatalog) {
            & $Python (Join-Path $Root "main.py") stories unpack --replace
            if ($LASTEXITCODE -ne 0) {
                throw "Story pack restore failed with exit code $LASTEXITCODE."
            }
        }

        & $Python (Join-Path $Root "main.py") database restore
        if ($LASTEXITCODE -ne 0) {
            throw "Database checkpoint restore failed with exit code $LASTEXITCODE."
        }

        if (-not $SkipVerify) {
            & $Python (Join-Path $Root "main.py") health --profile $Profile --full --strict
            if ($LASTEXITCODE -ne 0) {
                throw "Project health verification failed with exit code $LASTEXITCODE."
            }
        }

        if ($RefreshResults) {
            $Refresh = Join-Path $PSScriptRoot "refresh-results.ps1"
            & $Refresh
            if ($LASTEXITCODE -ne 0) {
                throw "Local result refresh failed with exit code $LASTEXITCODE."
            }
        }
    }
    else {
        $Checkpoint = Join-Path $PSScriptRoot "checkpoint.ps1"
        & $Checkpoint `
            -Message $Message `
            -ForceVacuum:$ForceVacuum `
            -SkipStoryRefresh:$SkipStoryRefresh
        if ($LASTEXITCODE -ne 0) {
            throw "Git handoff failed with exit code $LASTEXITCODE."
        }
    }
}
finally {
    Pop-Location
}
