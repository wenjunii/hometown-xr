param(
    [ValidateSet("auto", "3080", "4090", "5090")]
    [string]$Profile = "auto",

    [switch]$Tune,

    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

function New-CompatibleVirtualEnvironment {
    $Candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($Version in $SupportedPythonVersions) {
            $Candidates += @{
                Command = "py"
                Arguments = @("-$Version")
            }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $Candidates += @{
            Command = "python"
            Arguments = @()
        }
    }

    foreach ($Candidate in $Candidates) {
        $Command = $Candidate.Command
        $PrefixArguments = @($Candidate.Arguments)
        $Compatible = $false
        try {
            & $Command @PrefixArguments -c $PythonCompatibilityCheck 2>$null
            $Compatible = $LASTEXITCODE -eq 0
        }
        catch {
            $Compatible = $false
        }
        if (-not $Compatible) {
            continue
        }
        & $Command @PrefixArguments -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -eq 0) {
            return
        }
    }

    throw "Unable to create .venv. Install 64-bit Python $($SupportedPythonVersions -join ' or ') for this profile."
}

function Resolve-InstallProfile {
    if ($Profile -ne "auto") {
        return $Profile
    }
    if ($env:HOMETOWN_XR_PROFILE -in @("3080", "4090", "5090")) {
        return $env:HOMETOWN_XR_PROFILE
    }
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $GpuName = nvidia-smi --query-gpu=name --format=csv,noheader 2>$null |
            Select-Object -First 1
        if ($LASTEXITCODE -eq 0 -and $GpuName -match "5090") {
            return "5090"
        }
    }
    return "legacy"
}

Push-Location $Root
try {
    $InstallProfile = Resolve-InstallProfile
    if ($InstallProfile -eq "5090") {
        throw (
            "5090 setup is security-blocked: retained Torch 2.12.1 requires setuptools<82, " +
            "but patched setuptools requires >=84.1.0. Complete the coordinated model migration " +
            "(PR #45) first. No exception was granted; the existing environment is unchanged."
        )
    }
    # Torch 2.1 on the 3080/4090 has no Python 3.12 wheel.
    $SupportedPythonVersions = if ($InstallProfile -eq "5090") { @("3.11", "3.12") } else { @("3.11") }
    $MaximumMinor = if ($InstallProfile -eq "5090") { 12 } else { 11 }
    $PythonCompatibilityCheck = (
        "import struct, sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, $MaximumMinor) " +
        "and struct.calcsize('P') == 8 else 1)"
    )
    if (Test-Path -LiteralPath $VenvPython) {
        & $VenvPython -c $PythonCompatibilityCheck
        if ($LASTEXITCODE -ne 0) {
            throw (
                "The existing .venv uses an unsupported Python. Stop project processes, preserve .venv " +
                "under a backup name, then rerun setup with 64-bit Python " +
                "$($SupportedPythonVersions -join ' or ') installed. Nothing was deleted or upgraded."
            )
        }
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is required. Install Git and Git LFS before setup."
    }
    git lfs version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Git LFS is required. Install it, then run 'git lfs install' and 'git lfs pull'."
    }
    git lfs pull
    if ($LASTEXITCODE -ne 0) {
        throw "Git LFS pull failed with exit code $LASTEXITCODE."
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        New-CompatibleVirtualEnvironment
    }

    & $VenvPython -m pip install --upgrade pip "setuptools==84.2.1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $RequirementsLock = if ($InstallProfile -eq "5090") {
        Join-Path $Root "requirements-lock-5090.txt"
    }
    else {
        Join-Path $Root "requirements-lock.txt"
    }
    & $VenvPython -m pip install -r $RequirementsLock
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($Dev) {
        & $VenvPython -m pip install -r (Join-Path $Root "requirements-test.txt")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $ValidationProfile = if ($InstallProfile -eq "legacy") { "3080" } else { $InstallProfile }
    & $VenvPython (Join-Path $Root "dependency_profiles.py") --check --installed --profile $ValidationProfile
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    $Archive = Join-Path $Root "data\checkpoints\progress.db.gz"
    $Database = Join-Path $Root "data\progress.db"
    if ((Test-Path -LiteralPath $Archive) -and -not (Test-Path -LiteralPath $Database)) {
        & $VenvPython (Join-Path $Root "main.py") database restore
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $StoryPackCatalog = Join-Path $Root "data\stories\_packs\catalog.json.gz"
    if (Test-Path -LiteralPath $StoryPackCatalog) {
        & $VenvPython (Join-Path $Root "main.py") stories unpack
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    & $VenvPython (Join-Path $Root "main.py") doctor --profile $Profile
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($Tune) {
        & (Join-Path $PSScriptRoot "benchmark.ps1") -Profile $Profile -Quick
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
finally {
    Pop-Location
}
