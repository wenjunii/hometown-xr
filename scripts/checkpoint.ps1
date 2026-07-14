param(
    [string]$Message = "chore: checkpoint extractor state",
    [switch]$NoPush,
    [switch]$ForceVacuum
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Lock = Join-Path $Root "data\.crawler.lock"

function Update-OriginMain {
    git fetch --prune origin
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to fetch origin/main before handoff."
    }
    $BehindText = git rev-list --count HEAD..origin/main
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to compare this checkpoint with origin/main."
    }
    if ([int]$BehindText -gt 0) {
        throw "origin/main is ahead. Run .\scripts\handoff.ps1 -Direction pull first."
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
    if ($Branch -ne "main") {
        throw "Workstation checkpoints must be sent from main; current branch is '$Branch'."
    }
    if (-not $NoPush) {
        Update-OriginMain
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
        Update-OriginMain
        git lfs status
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect Git LFS state before push."
        }
        git push origin HEAD:main
        if ($LASTEXITCODE -ne 0) {
            throw "Checkpoint push failed with exit code $LASTEXITCODE."
        }
        git fetch origin
        if ($LASTEXITCODE -ne 0) {
            throw "Checkpoint was pushed, but origin/main could not be confirmed."
        }
        $LocalHead = git rev-parse HEAD
        $RemoteHead = git rev-parse origin/main
        if ($LASTEXITCODE -ne 0 -or $LocalHead -ne $RemoteHead) {
            throw "Checkpoint push could not be verified against origin/main."
        }
    }
}
finally {
    Pop-Location
}
