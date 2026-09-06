# claude-sessions

Save the Claude Code sessions that are open in a console window, so they can be
reopened after a reboot with `claude --resume`.

Windows only, and PowerShell 7 or later. It reads Windows process start times to tell a
live session from a stale record, and opens consoles through Windows Terminal. No
dependencies beyond PowerShell itself and the Windows Script Host, which is part of
Windows and is only used to start the scheduled task without a window.

## Why this is not just a directory listing

Claude Code registers every running session as `~/.claude/sessions/<pid>.json` and
deletes that file when the session exits. The directory is a live picture and never a
history, so by the time the machine has shut down there is nothing left to read. The
list has to be captured while the windows are still open, which is what the scheduled
task is for.

Open means a console window you could type a prompt into. Three conditions, all
required:

| Field | Value | Rules out |
| --- | --- | --- |
| `kind` | `interactive` | background jobs, which report `bg` |
| `entrypoint` | `cli` | `claude -p` and other SDK callers, which report `sdk-cli` |
| process | alive, with a matching start time | sessions that have exited, and reused pids |

The second row is the one that is easy to get wrong. A `claude -p` run also reports
`kind: interactive`, so filtering on `kind` alone quietly includes every scripted
invocation.

The third row uses the `procStart` field, a Windows FILETIME recorded when the session
started. Checking that the pid exists is not enough on its own, because Windows reuses
pids; checking that the pid exists *and* started at exactly the recorded moment is an
identity check.

## The three pieces

### Save-ClaudeSessions.ps1

Reads the registry, applies the filter above, and writes the survivors to
`%LOCALAPPDATA%\claude-sessions\snapshot.json`. The previous snapshot is kept beside it
as `snapshot.json.prev.json`, so a run that catches a moment with nothing open does not
destroy the list from the run before.

```powershell
./Save-ClaudeSessions.ps1 -NoWrite     # list what is open, write nothing
./Save-ClaudeSessions.ps1              # list it and write the snapshot
./Save-ClaudeSessions.ps1 -Quiet       # how the scheduled task runs it
```

The write is atomic: a temporary file next to the target, then a move into place, so a
reboot landing mid-write leaves the old snapshot rather than half a new one.

If the registry directory is missing, or every file in it lacks the fields this script
expects, it throws rather than writing an empty snapshot over a good one. That is
deliberate. The alternative failure, silently reporting that nothing is open, is the
one you would not notice until the day you needed the snapshot.

### Register-SnapshotTask.ps1

Registers a scheduled task that runs the collector every few minutes, as the current
user, while that user is logged on.

```powershell
./Register-SnapshotTask.ps1                        # install, three minute interval
./Register-SnapshotTask.ps1 -IntervalMinutes 5     # or a different one
./Register-SnapshotTask.ps1 -Status                # is it there, did it work
./Register-SnapshotTask.ps1 -Unregister            # remove it
```

It does not need an elevated shell. After registering, the task first fires at your
next logon; run `Start-ScheduledTask -TaskName ClaudeSessionSnapshot` to take one
immediately.

The task does not start pwsh directly. Its action is

```text
wscript.exe //B //Nologo "<dir>\Start-Hidden.js" "<pwsh>" -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "<dir>\Save-ClaudeSessions.ps1" -Quiet
```

where `Start-Hidden.js` is a small script that starts whatever command follows it
with no window, waits, and exits with the child's exit code. Started directly, pwsh
flashes a console window on every run, which every three minutes is a real nuisance;
the gotcha below has the details. The launcher knows nothing about Claude Code, so it
can be copied anywhere else that has the same problem.

Snapshotting on a timer rather than at shutdown is the whole design decision. A
shutdown script is the obvious approach and it fails in both directions: a graceful
shutdown may let every session delete its own registry entry before any shutdown script
runs, and a crash or a power cut runs nothing at all. A timer costs one short process
every few minutes, and the worst case after an unexpected shutdown is losing the
sessions you opened inside the last interval.

### Restore-ClaudeSessions.ps1

Opens one console per remembered session, in the directory that session was started
from, running `claude --resume <id>`.

```powershell
./Restore-ClaudeSessions.ps1 -WhatIf     # show what would open
./Restore-ClaudeSessions.ps1             # open them as tabs in one window
./Restore-ClaudeSessions.ps1 -Separate   # a window each
./Restore-ClaudeSessions.ps1 -Verify     # skip any with no transcript on disk
```

It merges two sources and deduplicates on session id: the snapshot, and any records
still sitting in the live registry. The second matters after an ungraceful shutdown,
where the files were never cleaned up and so name exactly the sessions that were open
when the power went.

Anything currently running is skipped, so running it twice does not give you two
consoles on the same session.

## Try this before installing any of it

There is one experiment worth running first, because it might make the scheduled task
unnecessary for the common case. A clean exit removes a session's registry file, but it
is not established what a Windows shutdown does. If shutdown kills the consoles faster
than they can clean up, then whatever remains in `~/.claude/sessions/` at the next boot
is already the list you wanted.

After the next reboot, before starting any session:

```powershell
Get-ChildItem "$HOME\.claude\sessions\*.json"
```

Non-empty means the registry survives a graceful shutdown on this machine, and
`Restore-ClaudeSessions.ps1` will work off those stale records with no snapshot at all.
Empty means the snapshot is doing real work. The scheduled task is worth having either
way, because it also covers a crash, but it is useful to know which mechanism is
actually carrying you.

## Things that are inferred rather than documented

None of this is a published interface. It is an internal Claude Code layout, read off
disk under version 2.1.261, and a future version may rename a field or move a
directory. The collector shouts when the fields it needs are missing, which is the
early warning.

Two specific inferences:

- **The entrypoint filter has only been checked against two observed values,** `cli`
  and `sdk-cli`. A session hosted by an IDE extension or the desktop app might also
  report `cli`, and would then be reopened into a console it never lived in. To find
  out, open one and read its file in `~/.claude/sessions/`.
- **The transcript path convention** used by `-Verify` takes the session's working
  directory and replaces every character that is not a letter or digit with a hyphen,
  giving the directory under `~/.claude/projects/`. That matched all 35 project
  directories on the machine this was written on, including worktree paths, but it was
  read off the disk rather than found in any documentation. That is why `-Verify` is
  opt-in rather than the default.

There is a third, milder one: `claude --resume` is run from the session's recorded
working directory on the grounds that transcripts are filed per directory. Resuming
from the wrong directory was never tested, so the scripts always restore the directory
first. It is free if it turns out not to be required.

## Gotchas

- **`[TimeSpan]::MaxValue` cannot be a repetition duration.** It serialises to
  `P99999999DT23H59M59S` and Task Scheduler rejects the whole task with "The task XML
  contains a value which is incorrectly formatted or out of range". Leaving the
  duration unset is what actually means indefinitely.
- **`Register-ScheduledTask` can fail without terminating.** It reported that error and
  the script carried on to print a success message. The installer now asks the
  scheduler whether the task exists rather than trusting the call, which is worth
  copying anywhere else that registers a task.
- **A newly registered task reports `LastTaskResult` 267011,** which is
  `SCHED_S_TASK_HAS_NOT_RUN` and not a failure. `-Status` says so rather than warning.
- **The launch path has not been run against a directory whose name contains a
  space.** Windows Terminal parses its own command line and then tokenises the trailing
  command again, so `-d` with such a path is the most likely thing to break first. Every
  restore test so far was a dry run, and none of the working directories involved had a
  space in it. Use `-Separate`, which goes through `Start-Process` instead, if a restore
  opens a console in the wrong place.
- **The task only runs while you are logged on,** by design. Process start times and
  the session registry both belong to the logged-on user, so a task configured to run
  whether or not the user is present would see nothing useful.
- **`pwsh -WindowStyle Hidden` still flashes a window from Task Scheduler.** pwsh is a
  console program, so it has a console before it reads that flag, and when Windows
  Terminal is the default terminal it takes that console over and paints a window in
  the moment before pwsh hides it. The task's `Hidden` setting is no help either; it
  only hides the task in the Task Scheduler list. The fix is to start pwsh from a
  program that has no console and asks for the child to be hidden from the start,
  which is what `Start-Hidden.js` under `wscript.exe` does. `conhost.exe --headless`
  also hides the window but was seen to return exit code 0 for a child that exited 7,
  which would turn every failed snapshot into a success in `-Status`. VBScript would
  work the same way and is on Microsoft's deprecation list; JScript under the same
  host is not.
