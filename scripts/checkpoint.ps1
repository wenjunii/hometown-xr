param(
    [string]$Message = "chore: checkpoint extractor state",
    [switch]$NoPush,
    [switch]$ForceVacuum
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Lock = Join-Path $Root "data\.crawler.lock"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if (Test-Path -LiteralPath $Lock) {
    throw "The crawler lock exists. Stop the crawler cleanly before checkpointing."
}

Push-Location $Root
try {
    $CheckpointArgs = @((Join-Path $Root "main.py"), "checkpoint")
    if ($ForceVacuum) {
        $CheckpointArgs += "--force-vacuum"
    }
    & $Python @CheckpointArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Checkpoint verification failed with exit code $LASTEXITCODE."
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
        git fetch --prune origin
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to fetch origin before handoff."
        }
        $Behind = [int](git rev-list --count HEAD..origin/main)
        if ($Behind -gt 0) {
            throw "origin/main is ahead. Run .\scripts\handoff.ps1 -Direction pull first."
        }
        git lfs status
        git push origin HEAD
        if ($LASTEXITCODE -ne 0) {
            throw "Checkpoint push failed with exit code $LASTEXITCODE."
        }
    }
}
finally {
    Pop-Location
}
