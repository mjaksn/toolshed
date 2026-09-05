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
tests if it has any. Nothing imports across tool directories.

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
| Type check | none configured |
| Format | none configured |
| Run | per tool, see its README |

`ruff check .` was run in this checkout: it reports `warning: No Python files
found under the given path(s)` and passes, which is the true state of an empty
repository rather than evidence that anything was checked. CI runs the same
command, so the first Python file to land is linted on arrival.

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

## Out of bounds

- Do not push, open or edit pull requests or issues, or take any other action
  that leaves this machine, without explicit permission.
- Do not add a root `pyproject.toml`, a root package directory, or anything
  else that turns the collection into a single project.
