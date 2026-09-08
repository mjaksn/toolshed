#Requires -Version 7.0

<#
.SYNOPSIS
    Put desktop shortcuts to the collector and the restorer, ready to double click.

.DESCRIPTION
    A shortcut that points straight at a .ps1 does not run it. Windows opens the file
    in whatever the file type is associated with, which on a machine that has ever
    been asked is an editor, and the "Run with PowerShell" entry on the context menu
    is Windows PowerShell 5.1, which refuses a script that requires 7 and closes the
    window on the refusal. Both failures look exactly like a script that ran and did
    nothing.

    So each shortcut targets pwsh.exe and passes its script with -File. -NoExit keeps
    the console open when the script finishes, which is what makes an error readable
    rather than a flash.

    Running this again over shortcuts that already exist needs -Force, because a
    shortcut on a desktop is somewhere a person may have changed something.

.PARAMETER Destination
    Where to write the shortcuts. The desktop by default.

.PARAMETER SaveIcon
    Icon for the collector's shortcut, in the "file,index" form the shell uses.

.PARAMETER RestoreIcon
    Icon for the restorer's shortcut, in the same form.

.PARAMETER Force
    Replace a shortcut that is already there.

.EXAMPLE
    ./New-DesktopShortcut.ps1 -WhatIf

    Say what would be written, and write nothing.

.EXAMPLE
    ./New-DesktopShortcut.ps1 -Force

    Write both, replacing whatever is on the desktop under those names.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $Destination = [Environment]::GetFolderPath('Desktop'),
    [string] $SaveIcon = '%SystemRoot%\System32\SHELL32.dll,122',
    [string] $RestoreIcon = '%SystemRoot%\System32\SHELL32.dll,146',
    [switch] $Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Destination)) {
    throw "No directory at $Destination to write shortcuts into."
}

# Resolved rather than assumed, because pwsh is not where powershell.exe is and a
# shortcut carrying the wrong one is the whole problem this script exists to avoid.
$pwshPath = (Get-Command pwsh.exe -ErrorAction Stop).Source

$wanted = @(
    [pscustomobject]@{
        Name    = 'Save-ClaudeSessions'
        Icon    = $SaveIcon
        Summary = 'Record the Claude Code sessions that are open in a console right now.'
    }
    [pscustomobject]@{
        Name    = 'Restore-ClaudeSessions'
        Icon    = $RestoreIcon
        Summary = 'Reopen the sessions the last snapshot recorded.'
    }
)

$shell = New-Object -ComObject WScript.Shell
$written = 0

foreach ($item in $wanted) {
    $script = Join-Path $PSScriptRoot "$($item.Name).ps1"
    if (-not (Test-Path -LiteralPath $script)) {
        throw "Cannot find $script. This makes shortcuts to the scripts beside it, so it has to live with them."
    }

    $path = Join-Path $Destination "$($item.Name).lnk"
    if ((Test-Path -LiteralPath $path) -and -not $Force) {
        Write-Warning "$path is already there. Pass -Force to replace it."
        continue
    }

    if (-not $PSCmdlet.ShouldProcess($path, 'create shortcut')) { continue }

    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $pwshPath
    $link.Arguments = "-NoExit -File `"$script`""
    $link.WorkingDirectory = $PSScriptRoot
    $link.IconLocation = $item.Icon
    $link.Description = $item.Summary
    $link.Save()

    $written++
    Write-Host "wrote $path"
}

if ($written -eq 0) {
    Write-Host "Nothing written."
} else {
    Write-Host "$written shortcut(s) in $Destination, each running $pwshPath."
}
