#Requires -Version 7.0

<#
.SYNOPSIS
    Record which Claude Code sessions are open in a console window right now.

.DESCRIPTION
    Claude Code registers each running session as ~/.claude/sessions/<pid>.json and
    deletes that file when the session exits, so the directory is a live picture and
    never a history. Once the machine is shut down there is nothing left to read, which
    is why this snapshot has to be taken while the windows are still open.

    A session counts as open, meaning a console window you could type a prompt into,
    when all three of these hold:

      kind is "interactive", which excludes background jobs.

      entrypoint is "cli", which excludes `claude -p` and other SDK callers. Those
      report kind "interactive" as well, so filtering on kind alone is not enough.

      the process is alive, meaning the recorded pid exists and its start time matches
      the recorded procStart, so a reused pid cannot impersonate a session that has
      already gone.

.PARAMETER SessionDir
    Where Claude Code keeps its live session registry.

.PARAMETER SnapshotPath
    Where to write the snapshot. A run that finds nothing open leaves an existing
    snapshot that names sessions exactly as it is, rather than emptying it, because
    nothing open now is not evidence that nothing was open a minute ago. The snapshot
    it replaces is also kept alongside it with a .prev.json suffix, as a second line
    of defence that nothing reads automatically.

.PARAMETER NoWrite
    Report what is open without touching the snapshot.

.PARAMETER Quiet
    Emit nothing on success. For the scheduled task.

.EXAMPLE
    ./Save-ClaudeSessions.ps1 -NoWrite

    List the open sessions without writing anything.

.EXAMPLE
    ./Save-ClaudeSessions.ps1 -Quiet

    How the scheduled task runs it.
#>

[CmdletBinding()]
param(
    [string] $SessionDir = (Join-Path $HOME '.claude/sessions'),
    [string] $SnapshotPath = (Join-Path $env:LOCALAPPDATA 'claude-sessions/snapshot.json'),
    [switch] $NoWrite,
    [switch] $Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# The registry is an internal Claude Code file with no documented stability. If a
# future version renames any of these, that is a change worth shouting about rather
# than quietly reporting that nothing is open.
$RequiredFields = @('pid', 'sessionId', 'cwd', 'kind', 'entrypoint', 'procStart')

function Test-ProcessMatches {
    <#
        True when the pid is running and started at exactly the recorded time.
        Windows records process start as a FILETIME, which is what makes this an
        identity check and not just a liveness one.
    #>
    param([int] $ProcessId, [string] $ProcStart)

    try {
        $p = Get-Process -Id $ProcessId -ErrorAction Stop
    } catch {
        return $false
    }

    try {
        return ([string] $p.StartTime.ToFileTime()) -eq ([string] $ProcStart)
    } catch {
        # StartTime throws for a process this user cannot query. A session belongs to
        # the user running this script, so treat that as not ours.
        return $false
    }
}

function Get-ClaudeSessionRecord {
    param([string] $Directory)

    if (-not (Test-Path -LiteralPath $Directory)) {
        throw "No session registry at $Directory. Either Claude Code has never run as this user, or the registry has moved and this script needs updating."
    }

    $malformed = 0
    $records = @()

    foreach ($file in Get-ChildItem -LiteralPath $Directory -Filter '*.json' -File) {
        try {
            $json = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        } catch {
            # A session starting or stopping mid-read is normal and not worth a warning.
            continue
        }

        $missing = $RequiredFields | Where-Object { -not $json.PSObject.Properties.Name.Contains($_) }
        if ($missing) {
            Write-Warning "$($file.Name) is missing $($missing -join ', '). The session registry format has probably changed; this script needs updating."
            $malformed++
            continue
        }

        $records += [pscustomobject]@{
            Pid        = [int] $json.pid
            SessionId  = [string] $json.sessionId
            Cwd        = [string] $json.cwd
            Kind       = [string] $json.kind
            Entrypoint = [string] $json.entrypoint
            ProcStart  = [string] $json.procStart
            Name       = if ($json.PSObject.Properties.Name.Contains('name')) { [string] $json.name } else { '' }
            Version    = if ($json.PSObject.Properties.Name.Contains('version')) { [string] $json.version } else { '' }
            StartedAt  = if ($json.PSObject.Properties.Name.Contains('startedAt')) { [long] $json.startedAt } else { 0 }
        }
    }

    if ($malformed -gt 0 -and $records.Count -eq 0) {
        throw "Every file in $Directory was unreadable in this script's terms. Refusing to write an empty snapshot over a good one."
    }

    return $records
}

function Test-SnapshotHasSession {
    <#
        True when the file at Path is a snapshot this script can read and it names at
        least one session. Anything it cannot read that way counts as naming none, so a
        corrupt snapshot cannot block a good one from replacing it.
    #>
    param([string] $Path)

    if (-not (Test-Path -LiteralPath $Path)) { return $false }

    try {
        $existing = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch {
        return $false
    }

    # ConvertFrom-Json returns nothing at all for valid JSON that is not an object, so
    # a null has to be caught here as well as a parse failure above.
    if ($null -eq $existing) { return $false }
    if (-not $existing.PSObject.Properties.Name.Contains('sessions')) { return $false }
    return (@($existing.sessions).Count -gt 0)
}

function Write-SnapshotAtomically {
    param([string] $Path, [object] $Payload)

    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }

    if (Test-Path -LiteralPath $Path) {
        Copy-Item -LiteralPath $Path -Destination "$Path.prev.json" -Force
    }

    # Write beside the target and move into place, so a reboot landing mid-write
    # leaves the old snapshot intact rather than a half-written one.
    $tmp = "$Path.tmp"
    $Payload | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $tmp -Encoding utf8 -NoNewline
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

# The @() matters. A function returning an empty array hands back nothing at all, and
# under Set-StrictMode the $all.Count below then throws rather than reporting zero.
$all = @(Get-ClaudeSessionRecord -Directory $SessionDir)

$open = @($all | Where-Object {
    $_.Kind -eq 'interactive' -and
    $_.Entrypoint -eq 'cli' -and
    (Test-ProcessMatches -ProcessId $_.Pid -ProcStart $_.ProcStart)
})

# Nothing open now is not evidence that nothing was open before. Closing every window
# a minute ahead of a shutdown would otherwise let the next run replace a good snapshot
# with an empty one, and Restore-ClaudeSessions.ps1 reads only the current file.
$keptPreviousSnapshot = (-not $NoWrite) -and $open.Count -eq 0 -and (Test-SnapshotHasSession -Path $SnapshotPath)

if (-not $NoWrite -and -not $keptPreviousSnapshot) {
    $payload = [pscustomobject]@{
        schema     = 1
        capturedAt = (Get-Date).ToUniversalTime().ToString('o')
        host       = $env:COMPUTERNAME
        registered = $all.Count
        sessions   = @($open | ForEach-Object {
            [pscustomobject]@{
                sessionId = $_.SessionId
                cwd       = $_.Cwd
                name      = $_.Name
                version   = $_.Version
                startedAt = $_.StartedAt
            }
        })
    }
    Write-SnapshotAtomically -Path $SnapshotPath -Payload $payload
}

if (-not $Quiet) {
    if ($open.Count -eq 0) {
        Write-Host "No open Claude Code sessions. $($all.Count) registered, none of them a typeable console window."
        if ($keptPreviousSnapshot) {
            Write-Host "Kept the snapshot at $SnapshotPath as it is, rather than emptying it."
        }
    } else {
        $open | Select-Object Pid, SessionId, Name, Cwd
        $where = if ($NoWrite) { 'not written' } else { $SnapshotPath }
        Write-Host "$($open.Count) of $($all.Count) registered sessions are open. Snapshot: $where"
    }
}
