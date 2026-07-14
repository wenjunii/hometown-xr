param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("pull", "push")]
    [string]$Direction,

    [ValidateSet("auto", "3080", "4090")]
    [string]$Profile = "auto",

    [string]$Message = "chore: checkpoint extractor state",

    [switch]$ForceVacuum,

    [switch]$SkipVerify,

    [switch]$RefreshResults
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Join-Path $Root "data\.crawler.lock"
$Python = Join-Path $Root ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $Lock) {
    throw "The crawler lock exists. Stop the crawler cleanly before a workstation handoff."
}

Push-Location $Root
try {
    if ($Direction -eq "pull") {
        $Changes = git status --porcelain
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect the worktree before handoff."
        }
        if ($Changes) {
            throw "The worktree is not clean. Commit or resolve local changes before handoff."
        }
        git pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            throw "Git pull failed with exit code $LASTEXITCODE."
        }
        git lfs pull
        if ($LASTEXITCODE -ne 0) {
            throw "Git LFS pull failed with exit code $LASTEXITCODE."
        }

        if (-not $SkipVerify) {
            if (-not (Test-Path -LiteralPath $Python)) {
                throw "Virtual environment is missing. Run .\scripts\setup.ps1 -Profile $Profile first."
            }
            & $Python (Join-Path $Root "main.py") doctor --profile $Profile
            if ($LASTEXITCODE -ne 0) {
                throw "Environment verification failed with exit code $LASTEXITCODE."
            }
            & $Python (Join-Path $Root "main.py") status
            if ($LASTEXITCODE -ne 0) {
                throw "Checkpoint status failed with exit code $LASTEXITCODE."
            }
            & $Python (Join-Path $Root "main.py") verify-output
            if ($LASTEXITCODE -ne 0) {
                throw "Output verification failed with exit code $LASTEXITCODE."
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
        & $Checkpoint -Message $Message -ForceVacuum:$ForceVacuum
        if ($LASTEXITCODE -ne 0) {
            throw "Git handoff failed with exit code $LASTEXITCODE."
        }
    }
}
finally {
    Pop-Location
}
