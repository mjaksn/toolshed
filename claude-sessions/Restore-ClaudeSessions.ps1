#Requires -Version 7.0

<#
.SYNOPSIS
    Reopen the Claude Code sessions that were open before the machine went down.

.DESCRIPTION
    Reads the snapshot written by Save-ClaudeSessions.ps1 and opens one console per
    session, in the directory that session was started from, running
    `claude --resume <id>`.

    Two sources are merged and deduplicated on session id:

      the snapshot, which is the normal case after a reboot, and

      whatever is still sitting in the live registry, which after an ungraceful
      shutdown is a set of files for processes that no longer exist. Those are stale
      records rather than junk: they name sessions that were open when the power went.

    Any session already open in a console is skipped, so this is safe to run twice and
    safe to run with sessions already open. Open means what it means in the collector:
    an interactive cli session whose process is alive. A background job or an SDK caller
    is running too, but it is not a console anyone could type into and it is not counted
    as one.

.PARAMETER SnapshotPath
    The snapshot written by Save-ClaudeSessions.ps1.

.PARAMETER SessionDir
    Claude Code's live session registry, read both for stale records and to tell what
    is already running.

.PARAMETER Verify
    Check that a transcript exists for each session before opening a console for it,
    and skip the ones with none. The transcript path is derived from the recorded
    working directory, which is an inferred convention rather than a documented one,
    so this is opt-in.

.PARAMETER Separate
    Open each session in its own window rather than as tabs in one.

.EXAMPLE
    ./Restore-ClaudeSessions.ps1 -WhatIf

    Show what would be opened, without opening anything.

.EXAMPLE
    ./Restore-ClaudeSessions.ps1 -Verify

    Reopen every snapshotted session that still has a transcript on disk.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string] $SnapshotPath = (Join-Path $env:LOCALAPPDATA 'claude-sessions/snapshot.json'),
    [string] $SessionDir = (Join-Path $HOME '.claude/sessions'),
    [string] $ProjectDir = (Join-Path $HOME '.claude/projects'),
    [switch] $Verify,
    [switch] $Separate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-TranscriptPath {
    <#
        Claude Code files a session's transcript under a directory named after its
        working directory, with every character that is not a letter or digit replaced
        by a hyphen. Checked against all 35 project directories on the machine this was
        written on, including worktree paths, but it is a convention read off the disk
        rather than a documented one.
    #>
    param([string] $Cwd, [string] $SessionId)

    $encoded = $Cwd -replace '[^A-Za-z0-9]', '-'
    return (Join-Path $ProjectDir (Join-Path $encoded "$SessionId.jsonl"))
}

function Get-LiveRegistryRecord {
    param([string] $Directory)

    if (-not (Test-Path -LiteralPath $Directory)) { return @() }

    $out = @()
    foreach ($file in Get-ChildItem -LiteralPath $Directory -Filter '*.json' -File) {
        try {
            $j = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
        } catch {
            continue
        }
        if (-not $j.PSObject.Properties.Name.Contains('sessionId')) { continue }

        $running = $false
        try {
            $p = Get-Process -Id ([int] $j.pid) -ErrorAction Stop
            $running = ([string] $p.StartTime.ToFileTime()) -eq ([string] $j.procStart)
        } catch {
            $running = $false
        }

        $out += [pscustomobject]@{
            SessionId = [string] $j.sessionId
            Cwd       = [string] $j.cwd
            Name      = if ($j.PSObject.Properties.Name.Contains('name')) { [string] $j.name } else { '' }
            Kind      = [string] $j.kind
            Entry     = [string] $j.entrypoint
            Running   = $running
        }
    }
    return $out
}

$live = Get-LiveRegistryRecord -Directory $SessionDir

# Running is not the same as open. A background job or an SDK caller has a live process
# and a registry entry, and counting either as an open console overstates what this run
# left alone. The filter is the collector's, so both scripts mean the same thing by it.
$alreadyOpen = @($live |
    Where-Object { $_.Running -and $_.Kind -eq 'interactive' -and $_.Entry -eq 'cli' } |
    Select-Object -ExpandProperty SessionId)

$candidates = [ordered]@{}

if (Test-Path -LiteralPath $SnapshotPath) {
    $snap = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json
    if ($snap.PSObject.Properties.Name.Contains('schema') -and $snap.schema -ne 1) {
        Write-Warning "Snapshot claims schema $($snap.schema); this script understands 1. Reading it anyway."
    }
    foreach ($s in $snap.sessions) {
        $candidates[$s.sessionId] = [pscustomobject]@{
            SessionId = [string] $s.sessionId
            Cwd       = [string] $s.cwd
            Name      = [string] $s.name
            Source    = 'snapshot'
        }
    }
    Write-Verbose "Snapshot from $($snap.capturedAt) lists $($snap.sessions.Count) session(s)."
} else {
    Write-Warning "No snapshot at $SnapshotPath. Falling back to whatever the live registry still holds."
}

# Stale registry entries: a session that was open when the machine went down without
# giving Claude Code the chance to clean up after itself.
foreach ($r in $live) {
    if ($r.Running) { continue }
    if ($r.Kind -ne 'interactive' -or $r.Entry -ne 'cli') { continue }
    if (-not $candidates.Contains($r.SessionId)) {
        $candidates[$r.SessionId] = [pscustomobject]@{
            SessionId = $r.SessionId
            Cwd       = $r.Cwd
            Name      = $r.Name
            Source    = 'stale registry entry'
        }
    }
}

$toOpen = @()
foreach ($c in $candidates.Values) {
    if ($alreadyOpen -contains $c.SessionId) {
        Write-Verbose "Skipping $($c.SessionId): already open."
        continue
    }
    if (-not (Test-Path -LiteralPath $c.Cwd)) {
        Write-Warning "Skipping $($c.SessionId): its directory $($c.Cwd) is gone."
        continue
    }
    if ($Verify) {
        $t = Get-TranscriptPath -Cwd $c.Cwd -SessionId $c.SessionId
        if (-not (Test-Path -LiteralPath $t)) {
            Write-Warning "Skipping $($c.SessionId): no transcript at $t"
            continue
        }
    }
    $toOpen += $c
}

if ($toOpen.Count -eq 0) {
    Write-Host "Nothing to reopen. $($candidates.Count) session(s) known, $($alreadyOpen.Count) already running."
    return
}

$wt = Get-Command wt.exe -ErrorAction SilentlyContinue
$opened = 0

foreach ($s in $toOpen) {
    $title = if ($s.Name) { $s.Name } else { $s.SessionId.Substring(0, 8) }
    $target = "$title  ($($s.SessionId)) in $($s.Cwd)"

    if (-not $PSCmdlet.ShouldProcess($target, 'open a console and resume')) { continue }

    # -NoExit keeps the console alive if claude exits, so a resume that fails leaves a
    # window with the error in it rather than one that vanishes.
    $inner = "claude --resume $($s.SessionId)"

    if ($wt -and -not $Separate) {
        # -w 0 reuses the most recent Windows Terminal window, so the sessions arrive as
        # tabs. Without an existing window it creates one.
        & $wt.Source -w 0 new-tab --title $title -d $s.Cwd pwsh.exe -NoExit -Command $inner
    } elseif ($wt) {
        & $wt.Source -w new new-tab --title $title -d $s.Cwd pwsh.exe -NoExit -Command $inner
    } else {
        Start-Process -FilePath 'pwsh.exe' -WorkingDirectory $s.Cwd `
            -ArgumentList '-NoExit', '-Command', $inner
    }

    $opened++
    Write-Host "opened $title  $($s.SessionId)  [$($s.Source)]"
}

Write-Host "$opened session(s) reopened, $($alreadyOpen.Count) left alone because they are already running."
