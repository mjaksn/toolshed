# toolshed

[![CI](https://github.com/mjaksn/toolshed/actions/workflows/ci.yml/badge.svg)](https://github.com/mjaksn/toolshed/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mjaksn/toolshed/blob/main/LICENSE)

Small scripts and helper apps that belong to no particular project. Anything
useful enough to keep and too small, too general, or too unrelated to live in
a repository of its own goes in here.

## What is in here

- [BasicUpsAdapter](BasicUpsAdapter/), an HTTP adapter for a CyberPower UPS on
  Linux: it reports what `pwrstat` knows and can shut the machine down, both
  behind a static key. Node, no dependencies.
- [apitrace](apitrace/), a Win32 API call tracer, the Windows analogue of strace:
  it launches a program as a debuggee and logs the calls it makes into the DLLs
  you select. In C and in Python, the same program twice.
- [claude-sessions](claude-sessions/), PowerShell scripts that record which Claude
  Code sessions are open in a console window and reopen them after a reboot.
- [dispatch-desk](dispatch-desk/), a PowerShell script, and the same thing again
  as a module, that creates Windows desktop shortcuts, each one dispatching a
  GitHub Actions workflow and then watching the run through to its conclusion.
- [EdfiScripts](EdfiScripts/), simple batch scripts providing basic automation of
  common tasks when doing local Ed-Fi ODS platform development, and very possibly
  outdated now.
- [lock-hashes](lock-hashes/), a Python script that rewrites a pip requirements
  file so every pin carries the hashes `pip install --require-hashes` checks
  against, with a check mode for CI. Standard library only.
- [RemoveNewline](RemoveNewline/), a PowerShell module whose one command strips
  every line break from a text file, in every form a line break takes, and
  keeps the encoding and byte order mark it found.
- [WinEvents](WinEvents/), a small Tk viewer for the classic Windows event logs,
  written while learning how Windows stores them: the log list comes out of the
  registry and the message text is assembled from the source's message DLL.

## What belongs here

A thing belongs in the shed when all three are true:

- It is useful more than once. A command run and forgotten does not need a home.
- It is not anchored to another project. Something only nettail or readerboard
  would ever run belongs in nettail or readerboard, where it stays next to the
  code it knows about.
- It is too small to carry a repository of its own. When one of these grows a
  release, a package on an index, an issue tracker, or users other than me, that
  is the signal to lift it out into its own repository rather than let the shed
  become the place a real project hides.

Languages mix here on purpose. PowerShell, Python and shell all end up in the
same shed because the sorting that matters is by task, not by runtime.

## Layout

One directory per tool at the top level, named after the tool, and each one
self-contained: its own README saying what the tool does and how to run it, its
own dependencies if it has any, its own tests if it has any, and its own
`ci.json` if it wants CI to check it.

Self-contained is the rule that keeps this repository from turning into a
project. Nothing imports across tool directories, and there is no shared
library at the root. Two tools that need the same helper each keep a copy, and
if that ever becomes genuinely painful the answer is a package, published
properly, rather than a root directory everything reaches into.

## Running one

Each tool's README is the instruction. There is no repository-wide install step
and nothing to build at the root, because there is no repository-wide anything:
a tool is fetched, read, and run on its own terms.

## Checks

CI checks a tool only when a change touches it, and runs that tool's own check
rather than something generic imposed from the root.

A tool opts in with a `ci.json` in its directory:

```json
{
  "runner": "windows-latest",
  "check": "pwsh -File ./check.ps1"
}
```

`runner` is the GitHub hosted runner it needs, which matters here because the
shed mixes PowerShell on Windows with things that want Linux. `check` runs from
the tool's own directory, under bash on every runner, and fails the build by
exiting non-zero. A tool wanting another interpreter names it in the command,
as this one does. Running the check by hand is the same command from the same
place, so a check that passes locally is the check CI runs.

A tool with no `ci.json` is not checked, and most will not have one. There is
no penalty for that, and nothing at the root has to be edited either way: the
workflow finds the tools by looking, never from a list.

Five tools have a check so far. `dispatch-desk` runs PSScriptAnalyzer,
fetched from the gallery and verified against a recorded hash rather than
installed, so what the check ran against is the same module every time. That is
the same arrangement the tool already uses for powershell-yaml at run time, and
for the same reason. `RemoveNewline` fetches the analyzer the same way and then
runs its own tests, written in plain PowerShell. `BasicUpsAdapter` runs its own
test suite under `node --test`, with nothing to install first. `WinEvents`
installs its test dependencies, pinned by version and hash, and runs `pytest`.
`lock-hashes` runs its own tests under `unittest`, with nothing to install.
Those last three each keep the command in a `check.sh` beside its `ci.json`, so
the check CI runs is one a person can run too.

## Licence

MIT. See [LICENSE](LICENSE).
