param(
    [string]$Message = "chore: checkpoint extractor state",
    [switch]$NoPush,
    [switch]$ForceVacuum
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Lock = Join-Path $Root "data\.crawler.lock"

function Update-OriginBranch {
    param([Parameter(Mandatory = $true)][string]$Branch)

    git fetch --prune origin
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to fetch origin before handoff."
    }
    git show-ref --verify --quiet "refs/remotes/origin/$Branch"
    $RemoteStatus = $LASTEXITCODE
    if ($RemoteStatus -eq 1) {
        return
    }
    if ($RemoteStatus -ne 0) {
        throw "Unable to inspect origin/$Branch before handoff."
    }
    $BehindText = git rev-list --count "HEAD..origin/$Branch"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to compare this checkpoint with origin/$Branch."
    }
    if ([int]$BehindText -gt 0) {
        throw "origin/$Branch is ahead. Run .\scripts\handoff.ps1 -Direction pull first."
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if (Test-Path -LiteralPath $Lock) {
    throw "The crawler lock exists. Stop the crawler cleanly before checkpointing."
}

Push-Location $Root
try {
    $Branch = git branch --show-current
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to determine the current Git branch."
    }
    if ([string]::IsNullOrWhiteSpace($Branch)) {
        throw "Checkpointing from a detached HEAD is not supported. Switch to a branch first."
    }
    if (-not $NoPush) {
        Update-OriginBranch -Branch $Branch
    }

    $CheckpointArgs = @((Join-Path $Root "main.py"), "checkpoint")
    if ($ForceVacuum) {
        $CheckpointArgs += "--force-vacuum"
    }
    & $Python @CheckpointArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Checkpoint verification failed with exit code $LASTEXITCODE."
    }
    $DatabaseArchive = Join-Path $Root "data\checkpoints\progress.db.gz"
    if (-not (Test-Path -LiteralPath $DatabaseArchive)) {
        throw "Checkpoint did not create the compressed database archive."
    }

    git add -A
    & $Python (Join-Path $Root "credential_guard.py") --scope staged --unstage
    if ($LASTEXITCODE -ne 0) {
        throw "Checkpoint stopped because the staged credential scan failed."
    }
    git diff --cached --quiet
    $DiffExit = $LASTEXITCODE
    if ($DiffExit -eq 1) {
        git commit -m $Message
        if ($LASTEXITCODE -ne 0) {
            throw "Checkpoint commit failed with exit code $LASTEXITCODE."
        }
    }
    elseif ($DiffExit -ne 0) {
        throw "Unable to inspect staged checkpoint changes."
    }

    if (-not $NoPush) {
        Update-OriginBranch -Branch $Branch
        git lfs status
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect Git LFS state before push."
        }
        git push --set-upstream origin "HEAD:refs/heads/$Branch"
        if ($LASTEXITCODE -ne 0) {
            throw "Checkpoint push failed with exit code $LASTEXITCODE."
        }
        git fetch origin
        if ($LASTEXITCODE -ne 0) {
            throw "Checkpoint was pushed, but origin/$Branch could not be confirmed."
        }
        $LocalHead = git rev-parse HEAD
        $RemoteHead = git rev-parse "origin/$Branch"
        if ($LASTEXITCODE -ne 0 -or $LocalHead -ne $RemoteHead) {
            throw "Checkpoint push could not be verified against origin/$Branch."
        }
    }
}
finally {
    Pop-Location
}
