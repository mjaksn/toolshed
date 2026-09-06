#Requires -Version 7.0

<#
.SYNOPSIS
    Install, inspect or remove the scheduled task that keeps the session snapshot fresh.

.DESCRIPTION
    The snapshot has to be taken while the console windows are still open, because
    Claude Code deletes a session's registry entry as it exits. Racing the shutdown is
    the obvious idea and the wrong one: a graceful shutdown may let every session clean
    up before any shutdown script runs, and a crash or a power cut runs nothing at all.

    A repeating task sidesteps both. It costs one short process every few minutes and
    the worst case after an unexpected shutdown is losing sessions opened inside the
    last interval.

    The task runs as the current user, only while that user is logged on, because the
    session registry it reads belongs to that user and process start times are only
    readable from inside the same session.

    The action goes through Start-Hidden.js, beside this script, rather than starting
    pwsh directly. Started directly, pwsh shows a console window for a moment on every
    run; the launcher creates it hidden from the start. The README has the details.

.PARAMETER IntervalMinutes
    How often to snapshot. Three minutes is a reasonable trade between a stale snapshot
    and pointless wakeups.

.PARAMETER TaskName
    Name to register under. Also what -Unregister and -Status look for.

.PARAMETER Unregister
    Remove the task.

.PARAMETER Status
    Report on the task without changing anything.

.EXAMPLE
    ./Register-SnapshotTask.ps1

    Install with the default three minute interval.

.EXAMPLE
    ./Register-SnapshotTask.ps1 -Status

    Show whether the task exists, when it last ran, and what it returned.

.EXAMPLE
    ./Register-SnapshotTask.ps1 -Unregister

    Take it away again.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [ValidateRange(1, 1440)]
    [int] $IntervalMinutes = 3,
    [string] $TaskName = 'ClaudeSessionSnapshot',
    [string] $ScriptPath = (Join-Path $PSScriptRoot 'Save-ClaudeSessions.ps1'),
    [switch] $Unregister,
    [switch] $Status
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Status) {
    if (-not $existing) {
        Write-Host "No task named $TaskName is registered."
        return
    }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        TaskName        = $existing.TaskName
        State           = $existing.State
        Action          = ($existing.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' | '
        LastRunTime     = $info.LastRunTime
        LastTaskResult  = $info.LastTaskResult
        NextRunTime     = $info.NextRunTime
    }
    # 267011 is SCHED_S_TASK_HAS_NOT_RUN and 267009 is SCHED_S_TASK_RUNNING. Neither is
    # a failure, and a freshly registered task reports the first of them until its
    # trigger fires at the next logon.
    switch ($info.LastTaskResult) {
        0       { }
        267011  { Write-Host "Registered but not run yet. It starts at next logon, or run Start-ScheduledTask -TaskName $TaskName now." }
        267009  { Write-Host "Currently running." }
        default { Write-Warning "Last run returned $($info.LastTaskResult). Zero is success; anything else means the snapshot is not being kept up to date." }
    }
    return
}

if ($Unregister) {
    if (-not $existing) {
        Write-Host "No task named $TaskName to remove."
        return
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'unregister scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed $TaskName."
    }
    return
}

if (-not (Test-Path -LiteralPath $ScriptPath)) {
    throw "Cannot find the collector at $ScriptPath. Pass -ScriptPath if the tool lives somewhere else."
}
$ScriptPath = (Resolve-Path -LiteralPath $ScriptPath).Path

$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'

# pwsh cannot be started without a window from Task Scheduler. It owns a console
# before it reads -WindowStyle Hidden, and Windows Terminal, when it is the default
# terminal, shows that console for a moment before pwsh hides it. Start-Hidden.js
# runs under wscript.exe, which has no console, creates the child hidden from the
# start, and passes its exit code back so LastTaskResult still means something.
$launcher = Join-Path $PSScriptRoot 'Start-Hidden.js'
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Cannot find the launcher at $launcher. It lives beside this script."
}

# -NonInteractive and -NoProfile keep the run short and stop a slow profile from
# turning a three minute cadence into overlapping runs. //B makes a script host
# error exit rather than show a dialog.
$action = New-ScheduledTaskAction -Execute $wscript `
    -Argument "//B //Nologo `"$launcher`" `"$pwsh`" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPath`" -Quiet"

# At logon, then every IntervalMinutes for as long as the session lasts. The repetition
# has to be lifted off a throwaway Once trigger because New-ScheduledTaskTrigger will
# not put a repetition on an AtLogOn trigger directly.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
#
# The duration is deliberately left unset. Task Scheduler reads an empty duration as
# repeat indefinitely, and the obvious [TimeSpan]::MaxValue is rejected outright:
# it serialises to P99999999DT23H59M59S and the register call fails with
# "The task XML contains a value which is incorrectly formatted or out of range".
$repeatSource = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$trigger.Repetition = $repeatSource.Repetition

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

# A snapshot of an empty machine is not worth waking a sleeping laptop for.
$settings.DisallowStartIfOnBatteries = $false
$settings.Hidden = $true

$verb = if ($existing) { 'replace scheduled task' } else { 'register scheduled task' }
if (-not $PSCmdlet.ShouldProcess("$TaskName every $IntervalMinutes minute(s)", $verb)) { return }

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Snapshot the open Claude Code sessions so they can be resumed after a reboot. From the toolshed repository, claude-sessions." `
    -Force -ErrorAction Stop | Out-Null

# Register-ScheduledTask can report a failure without terminating, which would leave
# this script claiming a success that never happened. Ask the scheduler instead.
if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    throw "Register-ScheduledTask reported no error but $TaskName is not there."
}

Write-Host "Registered $TaskName, running every $IntervalMinutes minute(s) as $env:USERNAME."
Write-Host "It starts at next logon. To take the first snapshot now:"
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
