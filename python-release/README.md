# python-release

A PowerShell script that does the two halves of releasing a Python project:
opening the pull request that bumps the version, and, once that is merged,
tagging `main` so the project's release workflow publishes it. It checks
everything it can before changing anything, and asks before each step that
leaves the machine.

It is written for projects shaped like nettail, netflume, lanname and
readerboard, and it assumes what they have in common:

- a static `version = "X.Y.Z"` in `pyproject.toml`;
- the same version as `__version__ = "X.Y.Z"` in the package's `__init__.py`;
- a `CHANGELOG.md` with a `## [X.Y.Z] - YYYY-MM-DD` section per release, and
  reference links for those headings at the foot of the file;
- a release workflow that fires on a pushed `vX.Y.Z` tag;
- a remote called `origin` and a default branch called `main`.

## What it needs

- PowerShell 7.4 or later.
- git on `PATH`.
- The GitHub CLI, `gh`, on `PATH` and signed in (`gh auth login`). The script
  checks both before it asks anything.

## Running it

```powershell
./python-release.ps1 ~/repos/netflume
```

The one parameter, `-RepositoryPath`, is the local checkout. A directory
inside it works too. The script then asks which goal you want.

Any check that fails prints what was wrong, prefixed with `python-release:`,
and exits 1 before anything has changed. A push or `gh pr create` that fails
once the commit exists also exits 1, and its message says what is already
done. Declining a confirmation stops the run and exits 0, saying what was done
and how to carry on by hand.

### 1: open a version bump pull request

In order, it:

1. refuses a checkout with staged or unstaged changes to tracked files
   (untracked files are fine);
2. reads the version from every place it is recorded, and refuses if they
   disagree, naming each file and what it says;
3. refuses unless the checkout is on `main` and `main` is the same commit as
   `origin/main`, since the release branch is cut from it;
4. asks for the new version, which must be `MAJOR.MINOR.PATCH` and higher than
   the current one;
5. refuses if a `release-X.Y.Z` branch already exists locally or on `origin`;
6. works out the changelog (see below);
7. creates `release-X.Y.Z`, writes the new version everywhere, and commits the
   changed files as `Release X.Y.Z`;
8. shows the commit and asks before pushing the branch;
9. shows the pull request body and asks before running
   `gh pr create --base main`, titled `Release X.Y.Z`.

Everything up to step 7 happens in memory, so a changelog it cannot read
unambiguously stops the run before a file is touched. The checkout is left on
`release-X.Y.Z`.

### 2: tag and push to trigger the release

Run this on `main` after the bump pull request is merged and pulled. In order,
it:

1. refuses a checkout with staged or unstaged changes to tracked files, because
   the version is read from the files on disk and has to be the version of the
   commit being tagged;
2. reads the version and refuses if the places disagree;
3. refuses if `CHANGELOG.md` has no non-empty `## [X.Y.Z]` section, since the
   release workflows fail on exactly that;
4. refuses if `vX.Y.Z` already exists locally or on `origin`;
5. refuses unless the checkout is on `main` and `main` is the same commit as
   `origin/main`;
6. asks, then creates an annotated tag `vX.Y.Z` with the message `vX.Y.Z` and
   pushes only that tag to `origin`.

The four projects are not consistent about tags: some annotated with the
version as the message, one lightweight, one annotated with the project name.
The release workflows accept any of them, so the script picks annotated, which
is what most of them already use.

## Where the version is recorded

| File | Line | |
| --- | --- | --- |
| `pyproject.toml` | `version = "X.Y.Z"` | required, exactly one |
| `<package>/__init__.py` or `src/<package>/__init__.py` | `__version__ = "X.Y.Z"` | required, exactly one |
| `README.md` | `<package>.__version__  # "X.Y.Z"` | if present, every one |
| `docs/openapi.json` | `"version": "X.Y.Z"` | if present, exactly one |

`<package>` is the `name` from `pyproject.toml`, lower cased, with hyphens and
dots turned into underscores. Only the `pyproject.toml` at the root counts, so
the nested ones under a project's `tools` directory are left alone.

The README line is the example netflume and lanname show of reading
`__version__`. `docs/openapi.json` is readerboard's generated API description,
which its CI compares byte for byte with a fresh one; only that one line is
replaced, and the file is never parsed and rewritten.

Edits are replacements inside the text as it was read, so line endings and any
byte order mark stay as they were.

## The changelog

The changelog is assumed to be well formatted, and the handling is deliberately
simple. For the new version, one of three things happens:

- **There is already a `## [X.Y.Z]` heading.** The changelog is left alone.
- **There is a `## [Unreleased]` heading.** It becomes `## [X.Y.Z] - <today>`,
  which is how every release of these projects has been cut, and an
  `[Unreleased]:` link at the foot becomes the link for the new version.
- **Neither.** It asks for the entry, as Markdown, ending with a line holding
  only a full stop. The section goes above the first `## ` heading, so the
  changelog still opens with the newest release, which nettail's tests insist
  on.

When it adds a heading and the file has no link for it yet, it copies the
shape of the newest existing link: a compare link from the previous version,
as lanname uses, or a link to the release page, as the other three do.

The pull request body, and the check on the tag path, lift the section out
with the same pattern the release workflows use for the release notes: from
the heading to the next `## [` heading or the first link reference. More than
one heading for the version, a section with nothing after it, or an empty
section is an error.

## Tests

```powershell
./test.ps1
```

Plain PowerShell, no framework. Each case builds a small project in a temporary
directory with a bare repository beside it standing in for `origin`, so pushes
and tags happen for real without a network. `gh` and every prompt are replaced
with fakes, and git runs against a throwaway global configuration so nothing
on the machine running the tests can change the result. 87 cases.

`./check.ps1` runs PSScriptAnalyzer over this directory and then the tests.
It fetches the analyzer from the PowerShell Gallery, pinned by version and
SHA256, and caches it in the temporary directory; the tests themselves need
nothing but git. CI runs it whenever a change touches this directory.
