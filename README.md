# toolshed

[![CI](https://github.com/mjaksn/toolshed/actions/workflows/ci.yml/badge.svg)](https://github.com/mjaksn/toolshed/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mjaksn/toolshed/blob/main/LICENSE)

Small scripts and helper apps that belong to no particular project. Anything
useful enough to keep and too small, too general, or too unrelated to live in
a repository of its own goes in here.

## What is in here

- [claude-sessions](claude-sessions/), PowerShell scripts that record which Claude
  Code sessions are open in a console window and reopen them after a reboot.
- [dispatch-desk](dispatch-desk/), a PowerShell script that creates Windows
  desktop shortcuts, each one dispatching a GitHub Actions workflow and then
  watching the run through to its conclusion.

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
own dependencies if it has any, its own tests if it has any.

Self-contained is the rule that keeps this repository from turning into a
project. Nothing imports across tool directories, and there is no shared
library at the root. Two tools that need the same helper each keep a copy, and
if that ever becomes genuinely painful the answer is a package, published
properly, rather than a root directory everything reaches into.

## Running one

Each tool's README is the instruction. There is no repository-wide install step
and nothing to build at the root, because there is no repository-wide anything:
a tool is fetched, read, and run on its own terms.

## Licence

MIT. See [LICENSE](LICENSE).
