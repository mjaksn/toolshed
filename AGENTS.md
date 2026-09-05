# AGENTS.md

Instructions for AI coding agents working in this repository. Every agent that
reads an `AGENTS.md` gets these, which is why they live here and not in a
vendor-specific file. `CLAUDE.md` is a one line pointer to this file and holds
nothing of its own.

## What this project is

A collection of small scripts and helper apps that belong to no particular
project. It is not a project itself and should not be made into one: there is
no package here, no shared library, no root build, and nothing to install.

The test of whether something belongs is in the README, and it is worth reading
before adding anything, because the interesting half of it is what to keep out.

## Layout

One directory per tool at the top level, named after the tool, each one
self-contained: its own README, its own dependencies if it has any, its own
tests if it has any, and its own `ci.json` if it wants CI to check it. Nothing
imports across tool directories.

That rule is the whole architecture, and it is easy to erode with a helper that
two tools want. Copy it into both rather than creating a root package. When
copying genuinely stops being tolerable, the answer is a real package published
properly, not a shared directory here.

## Environment

- Languages: mixed on purpose. PowerShell, Python and shell are all expected.
- Platform: written on Windows 11 with PowerShell 7. Python tools should stay
  cross-platform unless the tool is inherently about Windows, in which case say
  so in its README.
- Package manager: whatever the individual tool declares. There is none at the
  root.

There is no repository-wide setup command. A checkout is ready to read, and a
tool is set up by following its own README.

## Commands

| Task | Command |
| --- | --- |
| Install | none at the root; per tool, see its README |
| Test | none at the root; per tool, see its README |
| Lint | `ruff check .` |
| Check one tool | `cd <tool>`, then the `check` command from its `ci.json` |
| Type check | none configured |
| Format | none configured |
| Run | per tool, see its README |

`ruff check .` was run in this checkout and passes. It covers two things now:
`apitrace/apitrace.py`, which carries its own `apitrace/ruff.toml` switching off
three stylistic rules for that one file with a reason beside each, and
`.github/changed_tools.py`, the CI plumbing. It runs repository-wide and
unconditionally, so any Python that lands is linted on arrival whatever else a
change touched.

`cd dispatch-desk && ./check.ps1` is the one tool check that exists. It runs
PSScriptAnalyzer over that directory and takes about a minute the first time,
because it fetches the module, and seconds afterwards from the cache. It passes
with nothing reported.

The PowerShell in `claude-sessions` is not linted, because that tool has no
`ci.json`. That is a gap rather than a decision, and closing it is a matter of
giving it one.

Every command in this table has been run in this repo and its output verified.
If one is added without running it, mark it `UNVERIFIED` rather than implying
otherwise.

## Conventions

Dependencies are pinned by version and by hash, in whatever file the tool's
ecosystem uses, and no third-party release is used within seven days of being
published. A tool with dependencies pins them in its own directory.

A new tool arrives with a README that says what it does, what it needs, and how
to run it. That README is the only documentation a reader of that tool is
promised, so it carries the whole story rather than deferring to the root.

Prose here, including code comments and anything the tools print, avoids em
dashes and double hyphens used as punctuation. A long command line option keeps
its two leading hyphens, and hyphenated words are ordinary spelling.

## How CI decides what to run

CI runs one workflow, `.github/workflows/ci.yml`, and it has four jobs.

`changes` works out which tools a change touched. It runs
`.github/changed_tools.py`, which walks the top level for directories holding a
`ci.json`, intersects them with the directories the diff touched, and prints
the survivors as a JSON array of matrix entries. Each entry carries the tool's
name, the runner it wants, the shell to use and the command to run.

`lint` runs `ruff check .` over everything, unconditionally. The only Python
here is the CI plumbing, which every tool depends on, so it is checked whatever
a change touched.

`tools` takes that array as its matrix, so it runs once per changed tool, on
that tool's own runner, from that tool's own directory. It does not run at all
when the array is empty.

`gate` is the single check for branch protection to require.

### Giving a tool a check

Put a `ci.json` in the tool's directory with three keys:

```json
{
  "runner": "windows-latest",
  "shell": "pwsh",
  "check": "./check.ps1"
}
```

`runner` is a GitHub hosted runner label, `shell` is what the command should be
run with, and `check` is run from the tool's own directory and must exit
non-zero on failure. A tool with no `ci.json` is never checked, which is the
ordinary state for a script with nothing to run against it and not a thing to
apologise for.

Nothing in the workflow knows the name of any tool, and adding one must not
require editing it. If a change to CI would need a tool named in the root, that
is the signal it is being done the wrong way.

The command in `check` is run as written, so a `ci.json` deserves the same
reading in review as the script beside it.

### Why it is shaped this way

The obvious arrangement is a workflow per tool with a `paths:` filter, and it
is a trap. A workflow skipped by a path filter reports nothing at all, so a
required check on one sits in Pending forever and the pull request can never
merge. GitHub's own advice is to avoid requiring a workflow that can be
skipped. Hence one workflow, an unconditional `gate`, and the conditionality
pushed down into a job inside it.

## Testing

There is no repository-wide suite. A tool with tests keeps them inside its own
directory and its README says how to run them. Adding tests to a tool that has
none is welcome; adding a root-level harness that reaches into every tool is
the shared-library mistake wearing different clothes.

## Gotchas

- **Line endings are set per language, not per repository.** `.gitattributes`
  gives PowerShell and batch files CRLF, holds shell, Python and Markdown at
  LF, and leaves everything else to `text=auto`, which stores LF and checks out
  whatever the platform uses, so those files are CRLF in a Windows working copy
  and git says so on the first `git add`. PowerShell on Windows is the common
  case for the tools here, and some hosts are fussy about the shebang line in a
  shell script that arrived with CRLF. Do not normalise a file against that.
- **CI's `gate` job is the only check worth requiring.** A job added above it
  but left out of its `needs` and its final step runs and is then ignored,
  which is worse than not running at all.
- **`gate` treats a skipped job differently depending on which job it is.** A
  skipped job reports `skipped` rather than `success`, so a gate that accepts
  only `success` fails the moment anything conditional is skipped. `tools` is
  conditional and skipped is its ordinary resting state, so `gate` accepts it.
  `changes` and `lint` run on every trigger, so for them anything but `success`
  is a failure, and that includes `skipped`: a `changes` job that did not run
  means `tools` was skipped for want of a matrix rather than for want of work.
  A new job needs deciding into one of those two camps rather than copied into
  whichever line is nearest.

## Out of bounds

- Do not push, open or edit pull requests or issues, or take any other action
  that leaves this machine, without explicit permission.
- Do not add a root `pyproject.toml`, a root package directory, or anything
  else that turns the collection into a single project.
