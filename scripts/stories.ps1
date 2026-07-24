param(
    [ValidateSet("status", "plan", "enrich", "export", "stop")]
    [string]$Action = "status",

    [string[]]$Crawl,

    [string[]]$Source,

    [ValidateRange(1, 1000000)]
    [int]$Limit = 10,

    [switch]$All,

    [switch]$Apply,

    [switch]$IncludeShort,

    [ValidateRange(1, 16)]
    [int]$Workers = 3
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$LockPath = Join-Path $Root "data\.crawler.lock"
$StopPath = Join-Path $Root "data\.story-stop-request.json"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment is missing. Run .\scripts\setup.ps1 first."
}
if ($Action -eq "enrich" -and -not $Apply) {
    throw "Enrichment downloads Common Crawl source files; pass -Apply after reviewing the plan."
}
if ($Apply -and $Action -ne "enrich") {
    throw "Apply is valid only with -Action enrich."
}
if ($IncludeShort -and $Action -ne "export") {
    throw "IncludeShort is valid only with -Action export."
}
if ($PSBoundParameters.ContainsKey("Workers") -and $Action -ne "enrich") {
    throw "Workers is valid only with -Action enrich."
}
if ($All -and ($Crawl -or $Source)) {
    throw "All cannot be combined with Crawl or Source."
}

if ($Action -eq "enrich" -and -not ("HometownXrStoryCtrlC" -as [type])) {
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
if ($Action -eq "enrich") {
    $Arguments += @("--yes", "--workers", $Workers)
}
if ($IncludeShort) {
    $Arguments += "--include-short"
}
Push-Location $Root
$ForwarderInstalled = $false
$RunTokenWasSet = $null -ne $env:HOMETOWN_XR_STORY_RUN_TOKEN
$PreviousRunToken = $env:HOMETOWN_XR_STORY_RUN_TOKEN
$ExitCode = 0
try {
    if ($Action -eq "enrich") {
        $env:HOMETOWN_XR_STORY_RUN_TOKEN = [Guid]::NewGuid().ToString("N")
        [HometownXrStoryCtrlC]::Install($StopPath, $env:HOMETOWN_XR_STORY_RUN_TOKEN)
        $ForwarderInstalled = $true
    }
    & $Python @Arguments
    $ExitCode = $LASTEXITCODE
    if (
        $Action -eq "enrich" -and
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
