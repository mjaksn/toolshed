# WinEvents

A small Tk viewer for the classic Windows event logs. Pick a log from the
dropdown, click "Get Last Hour", and the events land in a time-sorted list.
Double-click a row to open the full detail in its own window.

This was a project I did while learning more about how Windows stores event
logs, so it is written to make that machinery visible rather than to be the
tool anyone reaches for. Event Viewer is better at every part of the job. What
this one has is a short enough path from the registry to the screen to read in
one sitting.

## What it turned out to be about

Three things drove most of the code, and all three are visible in
`event_viewer.py`:

- **The log list lives in the registry, not in an API call.** The sources come
  from enumerating the subkeys of
  `HKLM\SYSTEM\CurrentControlSet\Services\EventLog`, which is the classic
  logging model: one subkey per log, each naming the sources allowed to write
  to it. When that read fails the viewer falls back to the three everyone has,
  Application, System and Security.
- **Timestamps arrive naive and local.** A record's `TimeGenerated` comes back
  with no offset attached, so the one hour cutoff it is compared against has to
  be naive local too. Attaching a timezone to either side would break the
  filter rather than improve it.
- **The message text is not in the log.** A record stores a message ID and
  insertion strings; the readable sentence is assembled at display time by
  looking up the ID in the message DLL that the source registered. That is why
  formatting is wrapped in a fallback: when the DLL is missing or the source is
  gone, the event is still there and only its wording is lost.

The viewer also matches the Windows light and dark app theme, including tinting
the native title bar, and offers to relaunch itself elevated because the
Security log is unreadable without administrator rights.

## What it needs

Windows, Python 3.12, and pywin32. Everything else it uses is standard library,
including the tkinter GUI.

```
pip install --require-hashes --requirement requirements.txt
```

## Running it

```
python event_viewer.py
```

It asks for elevation on startup via UAC. Decline and it carries on unelevated,
with a warning that the Security log will not open.

## Tests

```
pip install --require-hashes --requirement requirements-dev.txt
python -m pytest
```

37 tests, and they pass. They mock the Win32 calls and the registry, so they run
anywhere the imports resolve and do not need a real event log or administrator
rights.

## State

Finished as a learning exercise and not maintained since. It is committed as it
was written, apart from one blank line removed to satisfy the shed's linter. The
rules that linter is not applying to it, and why, are written out in
`ruff.toml`.
