param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("pull", "push")]
    [string]$Direction,

    [string]$Message = "chore: checkpoint extractor state",

    [switch]$ForceVacuum
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Lock = Join-Path $Root "data\.crawler.lock"

if (Test-Path -LiteralPath $Lock) {
    throw "The crawler lock exists. Stop the crawler cleanly before a workstation handoff."
}

Push-Location $Root
try {
    if ($Direction -eq "pull") {
        $Changes = git status --porcelain
        if ($Changes) {
            throw "The worktree is not clean. Commit or resolve local changes before handoff."
        }
        git pull --ff-only
        git lfs pull
    }
    else {
        $Checkpoint = Join-Path $PSScriptRoot "checkpoint.ps1"
        & $Checkpoint -Message $Message -ForceVacuum:$ForceVacuum
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Git handoff failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
