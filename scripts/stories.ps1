param(
    [ValidateSet(
        "status",
        "plan",
        "enrich",
        "export",
        "stop",
        "failures",
        "retry",
        "report",
        "verify",
        "pack",
        "unpack",
        "pack-status",
        "serve",
        "curate",
        "review-export",
        "full-plan",
        "full-status",
        "full-recover",
        "full-export"
    )]
    [string]$Action = "status",

    [string[]]$Crawl,

    [string[]]$Source,

    [string[]]$Category,

    [string]$Month,

    [ValidateRange(1, 1000000)]
    [int]$Limit = 10,

    [switch]$All,

    [switch]$Apply,

    [switch]$IncludeShort,

    [switch]$Replace,

    [string]$BindHost = "127.0.0.1",

    [ValidateRange(1, 65535)]
    [int]$Port = 8770,

    [switch]$OpenBrowser,

    [switch]$NoWorkstationGuard,

    [ValidateRange(0, 64)]
    [int]$NearDistance = 3,

    [ValidateScript({
        $ParsedWorkers = 0
        if (
            $_ -eq "auto" -or
            ([int]::TryParse($_, [ref]$ParsedWorkers) -and
                $ParsedWorkers -ge 1 -and
                $ParsedWorkers -le 16)
        ) {
            return $true
        }
        throw "Workers must be 'auto' or an integer from 1 through 16."
    })]
    [string]$Workers = "auto"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LockPath = Join-Path $Root "data\.crawler.lock"
$StopPath = Join-Path $Root "data\.story-stop-request.json"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -in @("enrich", "full-recover") -and -not $Apply) {
    throw "$Action downloads Common Crawl source data; pass -Apply after reviewing the plan."
}
if ($Apply -and $Action -notin @("enrich", "retry", "full-recover")) {
    throw "Apply is valid only with -Action enrich, retry, or full-recover."
}
if ($IncludeShort -and $Action -ne "export") {
    throw "IncludeShort is valid only with -Action export."
}
if ($Replace -and $Action -ne "unpack") {
    throw "Replace is valid only with -Action unpack."
}
if (
    (
        $PSBoundParameters.ContainsKey("BindHost") -or
        $PSBoundParameters.ContainsKey("Port") -or
        $OpenBrowser
    ) -and
    $Action -ne "serve"
) {
    throw "BindHost, Port, and OpenBrowser are valid only with -Action serve."
}
if ($PSBoundParameters.ContainsKey("Workers") -and $Action -notin @("enrich", "full-recover")) {
    throw "Workers is valid only with -Action enrich or full-recover."
}
if ($PSBoundParameters.ContainsKey("NearDistance") -and $Action -ne "curate") {
    throw "NearDistance is valid only with -Action curate."
}
if ($NoWorkstationGuard -and $Action -notin @("enrich", "full-recover")) {
    throw "NoWorkstationGuard is valid only with -Action enrich or full-recover."
}
if ($Category -and $Action -ne "retry") {
    throw "Category is valid only with -Action retry."
}
if (
    $PSBoundParameters.ContainsKey("Limit") -and
    $Action -notin @("status", "plan", "enrich", "failures", "full-plan", "full-status", "full-recover")
) {
    throw "Limit is not valid with Action $Action."
}
if ($All -and ($Crawl -or $Source)) {
    throw "All cannot be combined with Crawl or Source."
}
if (
    ($All -or $Crawl -or $Source) -and
    $Action -notin @("status", "plan", "enrich", "retry", "full-plan", "full-status", "full-recover")
) {
    throw "All, Crawl, and Source are not valid with Action $Action."
}
if (($Crawl -or $Source) -and $Action -like "full-*") {
    throw "Crawl and Source are not valid with full-source actions; use Month."
}
if ($Action -like "full-*" -and -not $Month) {
    throw "Month is required for full-source actions and must use YYYY-MM."
}
if ($Month -and $Action -notlike "full-*") {
    throw "Month is valid only with a full-source action."
}
if ($Action -eq "full-recover" -and $Workers -eq "auto") {
    throw "Full-source recovery Workers must be an integer from 1 through 8."
}
if ($Action -eq "full-recover" -and [int]$Workers -gt 8) {
    throw "Full-source recovery Workers must be an integer from 1 through 8."
}
if ($Action -eq "retry" -and -not ($All -or $Crawl -or $Source -or $Category)) {
    throw "Retry requires All, Crawl, Source, or Category."
}

if ($Action -in @("enrich", "full-recover") -and -not ("HometownXrStoryCtrlC" -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.IO;

public static class HometownXrStoryCtrlC
{
    private static string stopPath;
    private static string runToken;
    private static bool installed;

    public static bool StopRequested { get; private set; }

    public static void Install(string requestPath, string currentRunToken)
    {
        stopPath = requestPath;
        runToken = currentRunToken;
        StopRequested = false;
        if (!installed)
        {
            Console.CancelKeyPress += Handle;
            installed = true;
        }
    }

    public static void Remove()
    {
        if (installed)
        {
            Console.CancelKeyPress -= Handle;
            installed = false;
        }
    }

    private static void Handle(object sender, ConsoleCancelEventArgs eventArgs)
    {
        eventArgs.Cancel = true;
        if (StopRequested)
        {
            Console.Error.WriteLine(
                "A graceful shutdown is already pending; please wait for the final summary."
            );
            return;
        }

        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(stopPath));
            File.WriteAllText(
                stopPath,
                "{\"run_token\":\"" + runToken + "\"}"
            );
            StopRequested = true;
            Console.Error.WriteLine(
                "Ctrl+C received. Graceful shutdown requested; waiting for active sources..."
            );
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("Could not write the shutdown request: " + error.Message);
        }
    }
}
"@
}

$Arguments = @((Join-Path $Root "main.py"), "stories", $Action)
if ($Action -in @("status", "plan", "enrich")) {
    if ($All) {
        $Arguments += "--all"
    }
    else {
        $Arguments += @("--limit", $Limit)
    }
    foreach ($CrawlId in $Crawl) {
        $Arguments += @("--crawl", $CrawlId)
    }
    foreach ($SourceFile in $Source) {
        $Arguments += @("--source", $SourceFile)
    }
}
if ($Action -in @("full-plan", "full-status", "full-recover")) {
    $Arguments += @("--month", $Month)
    if ($All) {
        $Arguments += "--all"
    }
    else {
        $Arguments += @("--limit", $Limit)
    }
}
if ($Action -eq "full-export") {
    $Arguments += @("--month", $Month)
}
if ($Action -eq "failures") {
    $Arguments += @("--limit", $Limit)
}
if ($Action -eq "retry") {
    if ($All) {
        $Arguments += "--all"
    }
    foreach ($CrawlId in $Crawl) {
        $Arguments += @("--crawl", $CrawlId)
    }
    foreach ($SourceFile in $Source) {
        $Arguments += @("--source", $SourceFile)
    }
    foreach ($FailureCategory in $Category) {
        $Arguments += @("--category", $FailureCategory)
    }
    if ($Apply) {
        $Arguments += "--yes"
    }
}
if ($Action -eq "enrich") {
    $Arguments += @("--yes", "--workers", $Workers)
    if ($NoWorkstationGuard) {
        $Arguments += "--no-workstation-guard"
    }
}
if ($Action -eq "full-recover") {
    $Arguments += @("--yes", "--workers", $Workers)
    if ($NoWorkstationGuard) {
        $Arguments += "--no-workstation-guard"
    }
}
if ($IncludeShort) {
    $Arguments += "--include-short"
}
if ($Replace) {
    $Arguments += "--replace"
}
if ($Action -eq "serve") {
    $Arguments += @("--host", $BindHost, "--port", $Port)
    if ($OpenBrowser) {
        $Arguments += "--open-browser"
    }
}
if ($Action -eq "curate") {
    $Arguments += @("--near-distance", $NearDistance)
}
Push-Location $Root
$ForwarderInstalled = $false
$RunTokenWasSet = $null -ne $env:HOMETOWN_XR_STORY_RUN_TOKEN
$PreviousRunToken = $env:HOMETOWN_XR_STORY_RUN_TOKEN
$ExitCode = 0
try {
    if ($Action -in @("enrich", "full-recover")) {
        $env:HOMETOWN_XR_STORY_RUN_TOKEN = [Guid]::NewGuid().ToString("N")
        [HometownXrStoryCtrlC]::Install($StopPath, $env:HOMETOWN_XR_STORY_RUN_TOKEN)
        $ForwarderInstalled = $true
    }
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
    if (
        $Action -in @("enrich", "full-recover") -and
        [HometownXrStoryCtrlC]::StopRequested -and
        (Test-Path -LiteralPath $LockPath)
    ) {
        Write-Host "Waiting for the story workers to release the checkpoint lock..."
        $WaitSeconds = 0
        while ((Test-Path -LiteralPath $LockPath) -and $WaitSeconds -lt 600) {
            Start-Sleep -Seconds 1
            $WaitSeconds += 1
            if ($WaitSeconds % 10 -eq 0) {
                Write-Host "Still stopping safely ($WaitSeconds seconds)..."
            }
        }
        if (Test-Path -LiteralPath $LockPath) {
            Write-Warning (
                "The crawler lock still exists after 10 minutes. " +
                "Run .\scripts\stories.ps1 -Action status before restarting."
            )
            $ExitCode = 1
        }
    }
}
finally {
    if ($ForwarderInstalled) {
        [HometownXrStoryCtrlC]::Remove()
    }
    if ($RunTokenWasSet) {
        $env:HOMETOWN_XR_STORY_RUN_TOKEN = $PreviousRunToken
    }
    else {
        [Environment]::SetEnvironmentVariable(
            "HOMETOWN_XR_STORY_RUN_TOKEN",
            $null,
            "Process"
        )
    }
    Pop-Location
}
exit $ExitCode
