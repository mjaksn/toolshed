#Requires -Version 7
<#
.SYNOPSIS
    Tests for Install-ToolshedModule.ps1. Run by check.ps1, and runnable by hand.

.DESCRIPTION
    Plain PowerShell with no test framework, so there is nothing to install.

    Everything runs against a synthetic shed built in a temporary directory,
    and installs into another temporary directory, so a script whose whole job
    is writing into Documents never writes into Documents while being tested.
    The synthetic shed is also the only way to cover the cases that matter and
    that the real shed happens not to have: a tool directory whose name matches
    its module, a hidden directory, a .psd1 with no .psm1 beside it.

    Symbolic links need Developer Mode or an elevated shell on Windows. If they
    cannot be created here, the link cases are reported as skipped rather than
    quietly passing, and the copy cases still run.

    Exits non-zero if any case failed.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$installer = Join-Path $PSScriptRoot 'Install-ToolshedModule.ps1'
$work = Join-Path ([System.IO.Path]::GetTempPath()) "installmodules-test-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $work | Out-Null

$script:passed = 0
$script:failed = 0
$script:skipped = 0

function Assert-Equal {
    param([string]$Case, $Expected, $Actual)
    if ("$Expected" -ceq "$Actual") {
        $script:passed++
        Write-Output "ok   $Case"
    }
    else {
        $script:failed++
        Write-Output "FAIL $Case"
        Write-Output "     expected [$Expected], got [$Actual]"
    }
}

function Assert-True {
    param([string]$Case, $Condition, [string]$Detail = '')
    if ($Condition) {
        $script:passed++
        Write-Output "ok   $Case"
    }
    else {
        $script:failed++
        Write-Output "FAIL $Case"
        if ($Detail) { Write-Output "     $Detail" }
    }
}

function Assert-Throw {
    param([string]$Case, [scriptblock]$Action, [string]$Contains)
    try {
        & $Action | Out-Null
        $script:failed++
        Write-Output "FAIL $Case"
        Write-Output '     nothing was thrown'
    }
    catch {
        $message = $_.Exception.Message
        if ($message -like "*$Contains*") {
            $script:passed++
            Write-Output "ok   $Case"
        }
        else {
            $script:failed++
            Write-Output "FAIL $Case"
            Write-Output "     expected a message containing [$Contains], got [$message]"
        }
    }
}

function Skip-Case {
    param([string]$Case, [string]$Why)
    $script:skipped++
    Write-Output "skip $Case ($Why)"
}

# ---------------------------------------------------------------------
# A synthetic shed, shaped like the real one plus the awkward cases
# ---------------------------------------------------------------------
$shed = Join-Path $work 'shed'

# A tool directory whose name is not the module's name, which is the shape of
# dispatch-desk and the reason this script exists.
$alpha = Join-Path $shed 'alpha-tool'
New-Item -ItemType Directory -Path $alpha -Force | Out-Null
Set-Content -LiteralPath (Join-Path $alpha 'AlphaModule.psm1') -Value @'
function Get-Alpha { 'alpha' }
Export-ModuleMember -Function Get-Alpha
'@
Set-Content -LiteralPath (Join-Path $alpha 'AlphaModule.psd1') -Value @'
@{
    RootModule        = 'AlphaModule.psm1'
    ModuleVersion     = '2.3.4'
    GUID              = '2f3d1a10-4c2b-4f7e-9a01-b3c4d5e6f708'
    PowerShellVersion = '5.1'
    FunctionsToExport = @('Get-Alpha')
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
}
'@
# Files that belong to whoever edits the module, not to whoever runs it.
Set-Content -LiteralPath (Join-Path $alpha 'check.ps1') -Value '# a check'
Set-Content -LiteralPath (Join-Path $alpha 'ci.json') -Value '{}'
Set-Content -LiteralPath (Join-Path $alpha 'PSScriptAnalyzerSettings.psd1') -Value '@{ ExcludeRules = @() }'

# A tool directory whose name does match, which is the shape of RemoveNewline.
$beta = Join-Path $shed 'BetaModule'
New-Item -ItemType Directory -Path $beta -Force | Out-Null
Set-Content -LiteralPath (Join-Path $beta 'BetaModule.psm1') -Value @'
function Get-Beta { 'beta' }
Export-ModuleMember -Function Get-Beta
'@

# A module that says it needs PowerShell 7, and says it in the .psm1 rather
# than a manifest, because that is the other place a module can say it.
$gamma = Join-Path $shed 'gamma-tool'
New-Item -ItemType Directory -Path $gamma -Force | Out-Null
Set-Content -LiteralPath (Join-Path $gamma 'GammaModule.psm1') -Value @'
#Requires -Version 7
function Get-Gamma { 'gamma' }
Export-ModuleMember -Function Get-Gamma
'@

# A tool with prose and a settings file but no module at all. The real shed has
# one of these, and its PSScriptAnalyzerSettings.psd1 is not a manifest.
$none = Join-Path $shed 'not-a-module'
New-Item -ItemType Directory -Path $none -Force | Out-Null
Set-Content -LiteralPath (Join-Path $none 'PSScriptAnalyzerSettings.psd1') -Value '@{ ExcludeRules = @() }'
Set-Content -LiteralPath (Join-Path $none 'Do-Something.ps1') -Value '# a script, not a module'

# A hidden directory, which is what .github looks like from here.
$hidden = Join-Path $shed '.hidden'
New-Item -ItemType Directory -Path $hidden -Force | Out-Null
Set-Content -LiteralPath (Join-Path $hidden 'HiddenModule.psm1') -Value '# should never be found'

# Can symbolic links be made here at all?
$canLink = $false
try {
    $probe = Join-Path $work 'link-probe'
    New-Item -ItemType SymbolicLink -Path $probe -Target $beta -ErrorAction Stop | Out-Null
    (Get-Item -LiteralPath $probe -Force).Delete()
    $canLink = $true
}
catch { $canLink = $false }

function Get-FreshDestination {
    $d = Join-Path $work ([guid]::NewGuid().ToString('N').Substring(0, 8))
    return $d
}

try {
    # -----------------------------------------------------------------
    # Discovery
    # -----------------------------------------------------------------
    $dest = Get-FreshDestination
    $result = @(& $installer -ShedPath $shed -Destination $dest -Copy)

    Assert-Equal 'exactly the real modules are found' 3 $result.Count
    Assert-Equal 'modules are named from the psm1, not the directory' 'AlphaModule,BetaModule,GammaModule' (($result.Name | Sort-Object) -join ',')
    Assert-True 'a directory with a psd1 but no psm1 is ignored' ($result.Name -notcontains 'PSScriptAnalyzerSettings')
    Assert-True 'a hidden directory is ignored' ($result.Name -notcontains 'HiddenModule')

    # -----------------------------------------------------------------
    # Copy mode takes the module and its manifest, and nothing else
    # -----------------------------------------------------------------
    $copied = @(Get-ChildItem (Join-Path $dest 'AlphaModule') | ForEach-Object { $_.Name } | Sort-Object)
    Assert-Equal 'a copy takes the psm1 and the psd1 beside it' 'AlphaModule.psd1,AlphaModule.psm1' ($copied -join ',')
    Assert-True 'a copy leaves check.ps1 behind' ($copied -notcontains 'check.ps1')
    Assert-True 'a copy leaves ci.json behind' ($copied -notcontains 'ci.json')
    Assert-True 'a copy leaves the analyzer settings behind' ($copied -notcontains 'PSScriptAnalyzerSettings.psd1')

    $betaCopied = @(Get-ChildItem (Join-Path $dest 'BetaModule') | ForEach-Object { $_.Name })
    Assert-Equal 'a module with no manifest copies just the psm1' 'BetaModule.psm1' ($betaCopied -join ',')

    # -----------------------------------------------------------------
    # WhatIf writes nothing
    # -----------------------------------------------------------------
    $dest = Get-FreshDestination
    $result = @(& $installer -ShedPath $shed -Destination $dest -Copy -WhatIf)
    Assert-Equal 'WhatIf reports what it would do' 'WouldCopy,WouldCopy,WouldCopy' (($result.Action) -join ',')
    Assert-True 'WhatIf does not create the destination' (-not (Test-Path -LiteralPath $dest))

    # -----------------------------------------------------------------
    # Selecting by name
    # -----------------------------------------------------------------
    $dest = Get-FreshDestination
    $result = @(& $installer -ShedPath $shed -Destination $dest -Copy -Name BetaModule)
    Assert-Equal 'a named module is the only one installed' 'BetaModule' ($result.Name -join ',')
    Assert-True 'the module that was not named is absent' (-not (Test-Path -LiteralPath (Join-Path $dest 'AlphaModule')))

    Assert-Throw 'an unknown name is an error, not a silent nothing' {
        & $installer -ShedPath $shed -Destination (Get-FreshDestination) -Copy -Name NoSuchModule
    } 'No module in the shed is called NoSuchModule'

    Assert-Throw 'a shed with no modules is an error' {
        $empty = Join-Path $work 'empty-shed'
        New-Item -ItemType Directory -Path $empty -Force | Out-Null
        & $installer -ShedPath $empty -Destination (Get-FreshDestination) -Copy
    } 'No modules found'

    # -----------------------------------------------------------------
    # The two editions
    # -----------------------------------------------------------------
    # These run under -WhatIf, which was proved above to write nothing, because
    # they are the only cases that must resolve the real Documents directories
    # rather than a temporary one.
    $documents = [Environment]::GetFolderPath('MyDocuments')
    $coreDir    = Join-Path $documents 'PowerShell\Modules'
    $desktopDir = Join-Path $documents 'WindowsPowerShell\Modules'

    # One run of each shape, with every assertion about that shape read off it.
    # The "What if:" lines these print cannot be redirected, since ShouldProcess
    # writes them straight to the host, and they are the proof the -WhatIf path
    # really ran.
    $both = @(& $installer -ShedPath $shed -Edition Both -WhatIf -WarningAction SilentlyContinue)
    Assert-Equal 'Both installs to both directories' "$coreDir,$desktopDir" (@($both.Destination | Split-Path -Parent | Sort-Object -Unique) -join ',')
    Assert-Equal 'Both covers every module for each edition' 6 $both.Count

    # A module is not put where it has said it cannot run.
    $onDesktop = $both | Where-Object Edition -eq 'Desktop'
    $onCore    = $both | Where-Object Edition -eq 'Core'
    $gammaRow  = $onDesktop | Where-Object Name -eq 'GammaModule'
    Assert-Equal 'a module needing 7 is held back from 5.1' 'Incompatible' $gammaRow.Action
    Assert-True 'and the reason says why' ($gammaRow.Reason -like '*needs PowerShell 7*')
    Assert-Equal 'a module declaring 5.1 goes to 5.1' 'WouldLink' ($onDesktop | Where-Object Name -eq 'AlphaModule').Action
    Assert-Equal 'a module declaring nothing goes to 5.1' 'WouldLink' ($onDesktop | Where-Object Name -eq 'BetaModule').Action
    Assert-Equal 'a module needing 7 is fine on 7' 'WouldLink' ($onCore | Where-Object Name -eq 'GammaModule').Action

    $core = @(& $installer -ShedPath $shed -Edition Core -WhatIf -WarningAction SilentlyContinue)
    Assert-Equal 'Core installs to PowerShell 7 only' $coreDir (@($core.Destination | Split-Path -Parent | Sort-Object -Unique) -join ',')
    Assert-Equal 'Core is labelled as such' 'Core' (@($core.Edition | Sort-Object -Unique) -join ',')

    $desktop = @(& $installer -ShedPath $shed -Edition Desktop -WhatIf -WarningAction SilentlyContinue)
    Assert-Equal 'Desktop installs to Windows PowerShell 5.1 only' $desktopDir (@($desktop.Destination | Split-Path -Parent | Sort-Object -Unique) -join ',')
    Assert-Equal 'Desktop is labelled as such' 'Desktop' (@($desktop.Edition | Sort-Object -Unique) -join ',')

    $forced = @(& $installer -ShedPath $shed -Edition Desktop -Force -WhatIf -WarningAction SilentlyContinue)
    Assert-Equal 'Force installs it into 5.1 anyway' 'WouldLink' ($forced | Where-Object Name -eq 'GammaModule').Action

    # Core's floor is the running PowerShell, not a flat 7.0. A module asking
    # for a 7.x newer than 7.0 but no newer than this one has to be allowed
    # through, and one asking for a version nobody has must not be. Its own
    # shed, so the counts above stay about the modules they are about.
    $versions = Join-Path $work 'version-shed'
    $running = $PSVersionTable.PSVersion
    $exact = '{0}.{1}' -f $running.Major, $running.Minor

    $exactTool = Join-Path $versions 'exact-tool'
    New-Item -ItemType Directory -Path $exactTool -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $exactTool 'ExactModule.psm1') -Value "#Requires -Version $exact"

    $futureTool = Join-Path $versions 'future-tool'
    New-Item -ItemType Directory -Path $futureTool -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $futureTool 'FutureModule.psm1') -Value '#Requires -Version 99.0'

    $onCoreNow = @(& $installer -ShedPath $versions -Edition Core -WhatIf -WarningAction SilentlyContinue)
    Assert-Equal "a module asking for $exact is installed on $running" 'WouldLink' ($onCoreNow | Where-Object Name -eq 'ExactModule').Action
    Assert-Equal 'a module asking for a version nobody has is held back' 'Incompatible' ($onCoreNow | Where-Object Name -eq 'FutureModule').Action

    # The edition directories are Windows locations, so asking for one anywhere
    # else has to stop rather than build a path with a separator in it that is
    # just a character. The guard reads $env:OS, which is what makes it testable
    # from here, and is the same check dispatch-desk makes.
    $realOs = $env:OS
    try {
        $env:OS = 'Something_Else'
        Assert-Throw 'an edition asked for off Windows stops' {
            & $installer -ShedPath $shed -Edition Core -WhatIf
        } 'not Windows'
        Assert-Throw 'and it says to use -Destination' {
            & $installer -ShedPath $shed -Edition Both -WhatIf
        } '-Destination'
        # -Destination names the directory itself, so it is not held to that.
        $offWindows = @(& $installer -ShedPath $shed -Destination (Get-FreshDestination) -Copy -Name BetaModule)
        Assert-Equal 'but an explicit destination still works' 'Copied' $offWindows[0].Action
    }
    finally { $env:OS = $realOs }

    # An explicit destination carries no version, so nothing is held back.
    $result = @(& $installer -ShedPath $shed -Destination (Get-FreshDestination) -Copy -Name GammaModule)
    Assert-Equal 'an explicit destination holds nothing back' 'Copied' $result[0].Action
    Assert-Equal 'an explicit destination is labelled Given' 'Given' $result[0].Edition

    # -----------------------------------------------------------------
    # Refusing to destroy what is already there
    # -----------------------------------------------------------------
    $dest = Get-FreshDestination
    New-Item -ItemType Directory -Path (Join-Path $dest 'AlphaModule') -Force | Out-Null
    $keep = Join-Path $dest 'AlphaModule\precious.txt'
    Set-Content -LiteralPath $keep -Value 'do not lose me'

    $result = @(& $installer -ShedPath $shed -Destination $dest -Copy -Name AlphaModule -WarningAction SilentlyContinue)
    Assert-Equal 'a real directory is not replaced without Force' 'Skipped' $result[0].Action
    Assert-True 'the real directory survives' (Test-Path -LiteralPath $keep)

    $result = @(& $installer -ShedPath $shed -Destination $dest -Copy -Name AlphaModule -Force)
    Assert-Equal 'a real directory is replaced with Force' 'Copied' $result[0].Action
    Assert-True 'the replaced directory is gone' (-not (Test-Path -LiteralPath $keep))

    # -----------------------------------------------------------------
    # Linking
    # -----------------------------------------------------------------
    if (-not $canLink) {
        foreach ($c in 'a link points at the tool directory',
                       'the link is named after the module',
                       'a second run changes nothing',
                       'a link somewhere else is not replaced without Force',
                       'a link somewhere else is replaced with Force',
                       'an installed module is found by name') {
            Skip-Case $c 'symbolic links cannot be created here'
        }
    }
    else {
        $dest = Get-FreshDestination
        $result = @(& $installer -ShedPath $shed -Destination $dest)
        Assert-Equal 'a link points at the tool directory' 'Linked,Linked,Linked' (($result.Action) -join ',')

        $link = Get-Item -LiteralPath (Join-Path $dest 'AlphaModule') -Force
        Assert-Equal 'the link is named after the module' 'SymbolicLink' $link.LinkType
        Assert-True 'the link targets the tool directory, whatever it is called' (
            (@($link.Target)[0]).TrimEnd('\') -ieq $alpha.TrimEnd('\')
        ) "target was $(@($link.Target)[0]), expected $alpha"

        $result = @(& $installer -ShedPath $shed -Destination $dest)
        Assert-Equal 'a second run changes nothing' 'AlreadyLinked,AlreadyLinked,AlreadyLinked' (($result.Action) -join ',')

        # A link pointing somewhere else is as protected as a real directory.
        (Get-Item -LiteralPath (Join-Path $dest 'AlphaModule') -Force).Delete()
        New-Item -ItemType SymbolicLink -Path (Join-Path $dest 'AlphaModule') -Target $beta | Out-Null
        $result = @(& $installer -ShedPath $shed -Destination $dest -Name AlphaModule -WarningAction SilentlyContinue)
        Assert-Equal 'a link somewhere else is not replaced without Force' 'Skipped' $result[0].Action

        $result = @(& $installer -ShedPath $shed -Destination $dest -Name AlphaModule -Force)
        Assert-Equal 'a link somewhere else is replaced with Force' 'Linked' $result[0].Action

        # The point of all of it: PowerShell finds the module by name, through
        # the link, even though the directory it points at is called something
        # else entirely.
        $probe = @"
`$env:PSModulePath = '$dest;' + `$env:PSModulePath
(Get-Module -ListAvailable -Name AlphaModule | Select-Object -First 1).Version.ToString()
"@
        $version = (pwsh -NoProfile -Command $probe 2>&1 | Select-Object -Last 1)
        Assert-Equal 'an installed module is found by name in PowerShell 7' '2.3.4' $version

        # The same link, read by the other edition. AlphaModule declares 5.1,
        # so this is the arrangement -Edition Desktop produces, checked against
        # the PowerShell that would actually load it.
        $version = (powershell.exe -NoProfile -Command $probe 2>&1 | Select-Object -Last 1)
        Assert-Equal 'an installed module is found by name in Windows PowerShell 5.1' '2.3.4' $version
    }
}
finally {
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Output ''
$total = $script:passed + $script:failed
$note = if ($script:skipped -gt 0) { ", $($script:skipped) skipped" } else { '' }
Write-Output "$total cases, $($script:passed) passed, $($script:failed) failed$note."
if ($script:failed -gt 0) { exit 1 }
exit 0
