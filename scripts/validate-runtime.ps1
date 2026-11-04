param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("3080", "4090", "5090")]
    [string]$Profile,

    [ValidateSet("fp32", "fp16")]
    [string]$Precision = "fp16"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run setup.ps1 -Profile $Profile -Dev in the isolated candidate checkout first."
}

function Invoke-ValidationPython {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Validation step failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

Push-Location $Root
try {
    $GpuName = & $Python -c "import torch; print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU')"
    if ($LASTEXITCODE -ne 0 -or $GpuName -notmatch [regex]::Escape($Profile)) {
        throw "This PC reports '$GpuName', not the requested $Profile GPU. No evidence was captured."
    }
    Invoke-ValidationPython -Arguments @("dependency_profiles.py", "--check", "--installed", "--profile", $Profile)
    Invoke-ValidationPython -Arguments @("-m", "pip", "check")
    Invoke-ValidationPython -Arguments @("dependency_audit.py")
    $Candidate = "data/evaluation/model-candidate-$Profile.json"
    Invoke-ValidationPython -Arguments @(
        "main.py", "model-validation", "capture", "--profile", $Profile,
        "--precision", $Precision, "--output", $Candidate
    )
    Invoke-ValidationPython -Arguments @(
        "main.py", "model-validation", "compare", "--candidate", $Candidate
    )
    Invoke-ValidationPython -Arguments @(
        "main.py", "benchmark", "--real", "--profile", $Profile,
        "--precision", $Precision, "--crawl", "CC-MAIN-2014-15",
        "--sources", "5", "--worker-count", "7"
    )
    $Workload = "data/evidence-local/workload-$Profile.json"
    $Report = Get-Content -LiteralPath $Workload -Raw | ConvertFrom-Json
    $InvalidTrials = @($Report.trials | Where-Object {
        $_.completed -ne 5 -or $_.failed -ne 0 -or $_.equivalent_output -ne $true
    })
    if ($Report.trial_outputs_agree -ne $true -or @($Report.trials).Count -eq 0 -or $InvalidTrials.Count -gt 0) {
        throw "The workload failed or differs from historical output. Evidence was not exported."
    }
    Invoke-ValidationPython -Arguments @(
        "main.py", "evidence", "export", "--profile", $Profile,
        "--candidate", $Candidate, "--workload", $Workload
    )
    Invoke-ValidationPython -Arguments @(
        "main.py", "evidence", "import", "--path",
        "data/evidence-outbox/profile-evidence-$Profile.json"
    )
    Write-Host "Evidence exported for $Profile. No profile settings, historical output, or migration approval were changed."
}
finally {
    Pop-Location
}
