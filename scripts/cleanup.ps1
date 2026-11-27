[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Verified
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$WindowsHost = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
$Comparison = if ($WindowsHost) { [StringComparison]::OrdinalIgnoreCase } else { [StringComparison]::Ordinal }
$Prefix = $Root + [IO.Path]::DirectorySeparatorChar
$CachePaths = @('.pytest_cache', '.ruff_cache', '__pycache__', 'tests/__pycache__',
    'scripts/__pycache__', '4090/__pycache__', '5090/__pycache__')

if ($Apply -and -not $Verified) {
    throw "Apply requires -Verified after successful task verification and stopping project writers."
}
if (Test-Path -LiteralPath (Join-Path $Root 'data/.crawler.lock')) {
    throw "Stop the project cleanly before cache cleanup; the crawler lock exists."
}

function Assert-NoLinkedParents {
    param([string]$Path)
    $Current = Get-Item -LiteralPath $Path -Force
    while ($null -ne $Current) {
        if ($Current.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Cleanup refuses linked paths: $($Current.FullName)"
        }
        $Current = $Current.Parent
    }
}

Assert-NoLinkedParents -Path $Root
$GitCheckout = Test-Path -LiteralPath (Join-Path $Root '.git')
if ($GitCheckout) {
    $GitRoot = & git -C $Root rev-parse --show-toplevel
    if ($LASTEXITCODE -ne 0 -or -not ([IO.Path]::GetFullPath($GitRoot)).Equals($Root, $Comparison)) {
        throw "Unable to verify the Git checkout before cleanup."
    }
}

function Get-CachePlan {
    param([string]$Relative)
    $Path = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $Path.StartsWith($Prefix, $Comparison)) {
        throw "Cleanup target escaped the checkout: $Path"
    }
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-NoLinkedParents -Path $Path
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "Expected a cache directory: $Path"
    }
    if ($GitCheckout) {
        $Tracked = & git --literal-pathspecs -C $Root ls-files -- $Relative
        if ($LASTEXITCODE -ne 0 -or $Tracked) {
            throw "Cleanup refuses tracked or unverifiable files in $Relative"
        }
    }
    $Kind = Split-Path -Leaf $Path
    if ($Kind -in @('.pytest_cache', '.ruff_cache')) {
        $Tag = Join-Path $Path 'CACHEDIR.TAG'
        if (-not (Test-Path -LiteralPath $Tag -PathType Leaf) -or
            (Get-Content -LiteralPath $Tag -TotalCount 1) -ne 'Signature: 8a477f597d28d172789f06886806bc55') {
            throw "Cache marker is missing or invalid: $Relative"
        }
    }
    $Files = @()
    $Pending = [Collections.Generic.Stack[string]]::new()
    $Pending.Push($Path)
    while ($Pending.Count -gt 0) {
        foreach ($Entry in Get-ChildItem -LiteralPath $Pending.Pop() -Force) {
            if ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Cleanup refuses linked cache entries: $($Entry.FullName)"
            }
            $Name = $Entry.FullName.Substring($Path.Length + 1).Replace('\', '/')
            if ($Entry.PSIsContainer) {
                $Allowed = ($Kind -eq '.pytest_cache' -and $Name -in @('v', 'v/cache')) -or
                    ($Kind -eq '.ruff_cache' -and $Name -match '^\d+\.\d+\.\d+([-.][a-z0-9]+)*$')
                if (-not $Allowed) { throw "Unrecognized cache directory: $($Entry.FullName)" }
                $Pending.Push($Entry.FullName)
                continue
            }
            $Allowed = ($Kind -eq '__pycache__' -and $Name -match '^[^/]+\.cpython-\d+(-pytest-\d+\.\d+\.\d+)?(\.opt-\d+)?\.pyc$') -or
                ($Kind -eq '.pytest_cache' -and $Name -in @('.gitignore', 'CACHEDIR.TAG', 'README.md',
                    'v/cache/nodeids', 'v/cache/lastfailed', 'v/cache/stepwise')) -or
                ($Kind -eq '.ruff_cache' -and ($Name -in @('.gitignore', 'CACHEDIR.TAG') -or
                    $Name -match '^\d+\.\d+\.\d+([-.][a-z0-9]+)*/\d+$'))
            if (-not $Allowed) { throw "Unrecognized cache file: $($Entry.FullName)" }
            $Files += [pscustomobject]@{
                name = $Name
                bytes = $Entry.Length
                sha256 = (Get-FileHash -LiteralPath $Entry.FullName -Algorithm SHA256).Hash
            }
        }
    }
    return [pscustomobject]@{
        path = $Path
        relative = $Relative
        bytes = [long](($Files | Measure-Object -Property bytes -Sum).Sum)
        files = @($Files | Sort-Object name)
    }
}

# Validate every target before any operation, then recheck immediately before recycling.
$Plan = @($CachePaths | ForEach-Object { Get-CachePlan -Relative $_ })
if ($Apply -and $Plan.Count -gt 0) {
    if (-not $WindowsHost -or -not [Environment]::UserInteractive) {
        throw "Apply needs an interactive Windows Recycle Bin. No permanent-delete fallback is provided."
    }
    Add-Type -AssemblyName Microsoft.VisualBasic
    foreach ($Item in $Plan) {
        $Current = Get-CachePlan -Relative $Item.relative
        if (($Current | ConvertTo-Json -Depth 5 -Compress) -ne ($Item | ConvertTo-Json -Depth 5 -Compress)) {
            throw "Cache changed during cleanup; stop writers and preview again: $($Item.path)"
        }
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory(
            $Item.path,
            [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
            [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin,
            [Microsoft.VisualBasic.FileIO.UICancelOption]::ThrowException
        )
        if (Test-Path -LiteralPath $Item.path) { throw "Cache recycling was not completed: $($Item.path)" }
    }
}
[pscustomobject]@{
    schema_version = 1
    applied = [bool]$Apply
    recycle_bin_requested = [bool]($Apply -and $Plan.Count -gt 0)
    recoverability = 'Windows Recycle Bin requested on apply; retention depends on Windows configuration. The bin is never emptied.'
    bytes = [long](($Plan | Measure-Object -Property bytes -Sum).Sum)
    targets = @($Plan | Select-Object relative, path, bytes)
} | ConvertTo-Json -Depth 4
