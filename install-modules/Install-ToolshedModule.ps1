#Requires -Version 7
<#
.SYNOPSIS
    Put the shed's PowerShell modules where PowerShell will find them.

.DESCRIPTION
    Walks the top level of the shed for modules and links each one into a
    directory on $env:PSModulePath, so that every module here is available in
    any session without an Import-Module by path.

    A module is a .psm1 in a tool directory, and its name is the .psm1's base
    name, never the directory's. Those differ: dispatch-desk holds
    DispatchDesk.psm1, and a module directory called dispatch-desk would never
    be autoloaded as DispatchDesk. Nothing here knows the name of any tool.

    By default it makes a symbolic link per module, pointing at the tool
    directory in this checkout, so an edit to a module is live in the next
    session with no reinstall step. The link is named after the module, which
    is what makes the mismatch above harmless: PowerShell reads the name of the
    link, not of its target. Use -Copy for a real copy instead, which is what a
    machine with no checkout wants.

    Nothing is moved. The files stay in the checkout where git can see them.

.PARAMETER Edition
    Which PowerShell's user module directory to install into. The two are
    separate directories and a module in one is invisible to the other, which
    is why this is a choice rather than a guess:

      Core     PowerShell 7            Documents\PowerShell\Modules
      Desktop  Windows PowerShell 5.1  Documents\WindowsPowerShell\Modules
      Both     both of the above

    Defaults to Core. Ignored when -Destination is given.

    A module that declares it needs a newer PowerShell than the edition asked
    for is reported and left out rather than installed somewhere it cannot
    load. The declaration is PowerShellVersion in the module's .psd1, or a
    "#Requires -Version" line in its .psm1; a module that declares neither is
    installed for either edition. Pass -Force to install it anyway.

    Desktop is 5.1, which is where Windows PowerShell stopped. Core is the
    version of the PowerShell running this script, so a module asking for 7.4
    is fine here on 7.6 and would not be on 7.2.

.PARAMETER Destination
    Install into these directories instead of working one out from -Edition.
    Mostly for tests, and for a module directory that is neither of the usual
    two.

.PARAMETER ShedPath
    The root of the shed. Defaults to the directory above this script, which is
    right whenever this script is where it lives in the repository.

.PARAMETER Name
    Install only the named modules. Without it, every module found is
    installed. A name that matches nothing is an error, because a typo that
    silently installs nothing is worse than a stop.

.PARAMETER Copy
    Copy the module instead of linking it. A copy takes the .psm1 and, if there
    is one beside it with the same base name, the .psd1, and nothing else: a
    check script and a test script are for whoever edits the module, not for
    whoever runs it. A link, being a view of the checkout, brings the whole tool
    directory along, and the extra files in it are inert to the module loader.

.PARAMETER Force
    Do it anyway. That covers the two things this script otherwise refuses:
    replacing a real directory or a link pointing somewhere else, which is the
    one thing here that could destroy something that is not a copy, and
    installing a module into an edition older than the module says it needs.

.EXAMPLE
    ./Install-ToolshedModule.ps1 -WhatIf

    Say what would be installed where, and write nothing.

.EXAMPLE
    ./Install-ToolshedModule.ps1

    Link every module in the shed into PowerShell 7's user module directory.

.EXAMPLE
    ./Install-ToolshedModule.ps1 -Name DispatchDesk -Edition Both -Force

    Install one module for both editions, replacing whatever is there.

.EXAMPLE
    ./Install-ToolshedModule.ps1 -Copy -Destination D:\Modules

    Copy every module somewhere specific, for a machine that will not have this
    checkout.
#>
[CmdletBinding(SupportsShouldProcess, DefaultParameterSetName = 'Edition')]
param(
    [Parameter(ParameterSetName = 'Edition')]
    [ValidateSet('Core', 'Desktop', 'Both')]
    [string]$Edition = 'Core',

    [Parameter(ParameterSetName = 'Destination', Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Destination,

    [ValidateNotNullOrEmpty()]
    [string]$ShedPath,

    [string[]]$Name,

    [switch]$Copy,

    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ShedModule {
    # Every .psm1 one level down, which is where the layout puts them: one
    # directory per tool at the top level, each self-contained. Not recursive,
    # because a .psm1 deeper than that would be somebody's fixture rather than
    # a module the shed offers.
    param([string]$Root)

    foreach ($tool in Get-ChildItem -LiteralPath $Root -Directory | Where-Object { $_.Name -notlike '.*' }) {
        foreach ($psm1 in Get-ChildItem -LiteralPath $tool.FullName -Filter '*.psm1' -File) {
            # A .psd1 counts only when it sits beside a .psm1 of the same base
            # name. The shed is full of PSScriptAnalyzerSettings.psd1 files,
            # including one in a directory with no module at all, and none of
            # them is a manifest.
            $manifest = Join-Path $tool.FullName "$($psm1.BaseName).psd1"
            [pscustomobject]@{
                Name     = $psm1.BaseName
                Tool     = $tool.Name
                ToolPath = $tool.FullName
                Module   = $psm1.FullName
                Manifest = if (Test-Path -LiteralPath $manifest -PathType Leaf) { $manifest } else { $null }
            }
        }
    }
}

function Get-ModuleDestination {
    # The two editions keep separate user module directories and neither reads
    # the other's, so installing for one says nothing about the other.
    #
    # Resolved through Windows rather than built from $HOME, so a redirected
    # Documents folder, OneDrive being the usual one, is handled.
    param([string]$Edition)

    $documents = [Environment]::GetFolderPath('MyDocuments')
    if (-not $documents) {
        throw 'Could not determine the Documents folder for the current user. Pass -Destination instead.'
    }
    # Core's version is this very PowerShell's, not a flat 7.0, because that is
    # the one that would actually load what goes in there. A module asking for
    # 7.4, which is what the check scripts in this repository ask for, must not
    # be held back from a 7.6 that can run it perfectly well.
    #
    # $PSVersionTable.PSVersion is a SemanticVersion, which will not compare
    # against a [version], so it is rebuilt rather than cast.
    $running = $PSVersionTable.PSVersion
    $core = [pscustomobject]@{
        Path    = Join-Path $documents 'PowerShell\Modules'
        Runs    = [version]('{0}.{1}.{2}' -f $running.Major, $running.Minor, $running.Patch)
        Edition = 'Core'
    }
    # Windows PowerShell stopped at 5.1 and is not going anywhere, so this one
    # is a constant in a way the other is not.
    $desktop = [pscustomobject]@{
        Path    = Join-Path $documents 'WindowsPowerShell\Modules'
        Runs    = [version]'5.1'
        Edition = 'Desktop'
    }

    switch ($Edition) {
        'Core'    { , @($core) }
        'Desktop' { , @($desktop) }
        'Both'    { , @($core, $desktop) }
    }
}

function Get-ModuleFloor {
    # The lowest PowerShell a module says it needs, or $null when it says
    # nothing. Two places can say it, and the manifest wins because it is the
    # one PowerShell itself enforces at import.
    param($Module)

    if ($Module.Manifest) {
        try {
            $data = Import-PowerShellDataFile -LiteralPath $Module.Manifest
            if ($data.ContainsKey('PowerShellVersion') -and $data.PowerShellVersion) {
                return ConvertTo-Version $data.PowerShellVersion
            }
        }
        catch {
            # An unreadable manifest is not this script's problem to report.
            # Fall through to the .psm1 and let the import fail later if it is
            # really broken.
            Write-Verbose "Could not read $($Module.Manifest): $($_.Exception.Message)"
        }
    }

    $requires = Get-Content -LiteralPath $Module.Module -TotalCount 20 |
        Select-String -Pattern '^\s*#Requires\s+-Version\s+([0-9]+(\.[0-9]+)*)' |
        Select-Object -First 1
    if ($requires) { return ConvertTo-Version $requires.Matches[0].Groups[1].Value }
    return $null
}

function ConvertTo-Version {
    # "7" is a perfectly ordinary thing to write in a #Requires line and not a
    # thing [version] will accept, so it gets the minor part it is missing.
    param([string]$Text)

    if ($Text -notmatch '\.') { $Text = "$Text.0" }
    return [version]$Text
}

function Get-TargetState {
    # What is already sitting where this module wants to go, and whether it is
    # safe to replace. A link holds no content, so replacing one that already
    # points at the right place is a no-op rather than work.
    param([string]$Path, [string]$ExpectedTarget)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return 'Absent' }
    if ($item.LinkType -eq 'SymbolicLink') {
        $current = @($item.Target)[0]
        if ($current -and (Test-SamePath $current $ExpectedTarget)) { return 'Current' }
        return 'OtherLink'
    }
    return 'RealDirectory'
}

function Test-SamePath {
    # Windows paths are case insensitive and a trailing separator means
    # nothing, so neither may decide that a correct link is a wrong one.
    param([string]$Left, [string]$Right)

    try {
        $l = [System.IO.Path]::GetFullPath($Left).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
        $r = [System.IO.Path]::GetFullPath($Right).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    }
    catch { return $false }
    return $l -ieq $r
}

function Install-ShedModule {
    # One module into one destination. Returns a record of what happened rather
    # than printing, so a caller can act on it and the tests can read it.
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)] $Module,
        [Parameter(Mandatory)] $Into,
        [switch]$AsCopy,
        [switch]$Replace
    )

    $target = Join-Path $Into.Path $Module.Name
    $source = if ($AsCopy) { $Module.Module } else { $Module.ToolPath }
    $state  = if ($AsCopy) { if (Test-Path -LiteralPath $target) { 'RealDirectory' } else { 'Absent' } }
              else         { Get-TargetState -Path $target -ExpectedTarget $Module.ToolPath }

    function New-Record {
        # Builds the result object and changes nothing, so the rule about the
        # New verb is suppressed here rather than switched off for the
        # directory. It stays on for the rest, which is a script that really
        # does write to the filesystem and wants watching.
        [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSUseShouldProcessForStateChangingFunctions', '',
            Justification = 'Constructs a record. The only thing here that changes state is Install-ShedModule, which does support ShouldProcess.')]
        param([string]$Action, [string]$Reason = '')
        [pscustomobject]@{
            Name        = $Module.Name
            Edition     = $Into.Edition
            Action      = $Action
            Destination = $target
            Source      = $source
            Reason      = $Reason
        }
    }

    # An edition older than the module says it needs would install cleanly and
    # then fail at import, which is a worse place to find out.
    if ($Into.Runs) {
        $floor = Get-ModuleFloor -Module $Module
        if ($floor -and $floor -gt $Into.Runs -and -not $Replace) {
            $why = "it needs PowerShell $floor and $($Into.Edition) is $($Into.Runs)"
            Write-Warning "$($Module.Name): $why. Left out. Pass -Force to install it anyway."
            return New-Record 'Incompatible' $why
        }
    }

    # A copy always rewrites, so an existing copy is only "current" in the sense
    # that a link is; treat it as something to replace and say so.
    if ($state -eq 'Current') {
        return New-Record 'AlreadyLinked' 'the link already points at this checkout'
    }

    if ($state -ne 'Absent' -and -not $Replace) {
        $what = switch ($state) {
            'RealDirectory' { 'a real directory is already there' }
            'OtherLink'     { 'a link is already there pointing somewhere else' }
        }
        Write-Warning "$($Module.Name): $what. Left alone. Pass -Force to replace it."
        return New-Record 'Skipped' $what
    }

    # Windows PowerShell's user module directory often does not exist until
    # something puts a module in it, so creating it is routine, not a red flag.
    if (-not (Test-Path -LiteralPath $Into.Path)) {
        if ($PSCmdlet.ShouldProcess($Into.Path, 'create the module directory')) {
            New-Item -ItemType Directory -Path $Into.Path -Force | Out-Null
        }
    }

    $verb = if ($AsCopy) { 'copy' } else { 'link' }
    if (-not $PSCmdlet.ShouldProcess($target, "$verb from $source")) {
        return New-Record $(if ($AsCopy) { 'WouldCopy' } else { 'WouldLink' }) 'WhatIf'
    }

    if ($state -ne 'Absent') {
        # A link is removed as a link. Remove-Item on a directory symlink
        # deletes the link and not what it points at, but -Recurse on one is a
        # long-standing way to be sorry, so it is never used here.
        $existing = Get-Item -LiteralPath $target -Force
        if ($existing.LinkType -eq 'SymbolicLink') { $existing.Delete() }
        else { Remove-Item -LiteralPath $target -Recurse -Force }
    }

    if ($AsCopy) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Copy-Item -LiteralPath $Module.Module -Destination $target
        if ($Module.Manifest) { Copy-Item -LiteralPath $Module.Manifest -Destination $target }
        return New-Record 'Copied'
    }

    try {
        New-Item -ItemType SymbolicLink -Path $target -Target $Module.ToolPath -ErrorAction Stop | Out-Null
    }
    catch {
        throw @(
            "Could not link $($Module.Name): $($_.Exception.Message)",
            'Creating a symbolic link on Windows needs Developer Mode turned on or an elevated shell.',
            'Run this again with -Copy to copy the modules instead.'
        ) -join [Environment]::NewLine
    }
    return New-Record 'Linked'
}

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
if (-not $ShedPath) { $ShedPath = Split-Path -Parent $PSScriptRoot }
$ShedPath = (Resolve-Path -LiteralPath $ShedPath).ProviderPath

$modules = @(Get-ShedModule -Root $ShedPath)
if ($modules.Count -eq 0) {
    throw "No modules found under $ShedPath. A module is a .psm1 in a tool directory one level down."
}

if ($Name) {
    $wanted = @($modules | Where-Object { $_.Name -in $Name })
    $missing = @($Name | Where-Object { $_ -notin $modules.Name })
    if ($missing.Count -gt 0) {
        throw @(
            "No module in the shed is called $($missing -join ', ').",
            "Found: $(($modules.Name | Sort-Object) -join ', ')"
        ) -join [Environment]::NewLine
    }
    $modules = $wanted
}

# An explicit -Destination carries no PowerShell version, so no module is ever
# held back from one on compatibility grounds. The caller said where.
$destinations = if ($PSCmdlet.ParameterSetName -eq 'Destination') {
    @($Destination | ForEach-Object {
        [pscustomobject]@{ Path = $_; Runs = $null; Edition = 'Given' }
    })
}
else { Get-ModuleDestination -Edition $Edition }

foreach ($into in $destinations) {
    foreach ($module in $modules) {
        Install-ShedModule -Module $module -Into $into -AsCopy:$Copy -Replace:$Force
    }
}
