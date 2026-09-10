# dispatch-desk

Create Windows desktop shortcuts that trigger GitHub Actions workflows.

A PowerShell module, `DispatchDesk`, exporting one command, `New-WorkflowShortcut`.

Run it once per workflow. It walks you through picking a repository, workflow, branch, and values for each `workflow_dispatch` input, verifies everything against GitHub, and drops a shortcut on your desktop. Double-clicking the shortcut opens a console window that prompts for any inputs you chose to leave open, dispatches the workflow, shows the run URL, waits for it to finish, and reports the result.

## Requirements

- Windows with PowerShell 5.1 or later (PowerShell 7 also works)
- [GitHub CLI](https://cli.github.com) installed and authenticated (`gh auth login`)
- Write (push) access to the target repository, which GitHub requires for dispatching workflows
- The [powershell-yaml](https://github.com/cloudbase/powershell-yaml) module, which the command fetches for itself on first run after asking. Nothing is installed: the package is downloaded from the gallery, checked against a SHA256 recorded beside the version it pins, unpacked under the temporary directory and imported from there. A copy of powershell-yaml already on the machine is neither used nor disturbed, and the import lands in this module's own session state, so the session you called it from does not gain a `ConvertFrom-Yaml` either.

## Setup

```powershell
Import-Module .\DispatchDesk.psd1
New-WorkflowShortcut -Owner mjaksn
```

To have PowerShell find it by name instead, copy `DispatchDesk.psd1` and `DispatchDesk.psm1` into a directory called `DispatchDesk` somewhere on `$env:PSModulePath`: `Documents\PowerShell\Modules` for PowerShell 7, or `Documents\WindowsPowerShell\Modules` for Windows PowerShell 5.1, which this module also supports. The two are separate and neither reads the other's, so a module wanted in both goes in both. It is then autoloaded on first use and the import line is unnecessary.

If your execution policy blocks the import:

```powershell
powershell -ExecutionPolicy Bypass -Command "Import-Module .\DispatchDesk.psd1; New-WorkflowShortcut -Owner mjaksn"
```

`-Owner` is the only required parameter, and the only thing the command cannot work out for itself. It then asks:

- **Repository**: name only, without the owner.
- **Branch**: defaults to the repository's default branch. The workflow file must exist on this branch.
- **Workflow**: chosen from a numbered list of the repository's workflows.
- **Inputs**: for each `workflow_dispatch` input you choose either a fixed value used on every run, or a prompt shown each time the shortcut runs. Prompted inputs can carry a default that the workflow's own default pre-fills. Choice, boolean, and environment inputs are picked from a list. Optional inputs can be left unset so the workflow default applies.
- **Shortcut name**: defaults to `<repo> - <workflow name>`.

A summary is shown and confirmed before anything is written.

### Answering ahead

The first four of those can be given on the command line instead, which is what makes the command scriptable:

```powershell
New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main `
    -Workflow dungeon-crawl.yml -ShortcutName 'Descend'
```

Supplying one skips its question and nothing else. The value is verified against GitHub exactly as a typed one is, so a branch that does not exist fails the same way whether you typed it or passed it. `-Workflow` matches either the file name or the display name in the workflow file, case-insensitively, and says which workflows the repository has if it matches none.

The workflow inputs and the final confirmation are always asked. There is no `-Force`, deliberately: writing to the desktop after a summary you agreed to is the whole safety story here, and a flag to skip it would be the first thing to regret.

### What it returns

A successful run emits one object, so a caller can do something with the result rather than reading it off the console:

```powershell
$shortcut = New-WorkflowShortcut -Owner mjaksn
$shortcut.ShortcutPath
```

It carries `Owner`, `Repository`, `Branch`, `Workflow`, `WorkflowFile`, `ShortcutName`, `ShortcutPath`, `LauncherPath`, `RunnerPath` and `LogDirectory`.

A failure throws, so `try`/`catch` works and the session you called it from is still there afterwards. Anything half written is removed before the error is raised. Importing the module touches neither the console encoding nor the error preference; `New-WorkflowShortcut` sets both for the length of a call.

## What gets created

```
~/.github_shortcuts/
  actions/
    <shortcut name>.cmd    launcher that the desktop shortcut points at
    <shortcut name>.ps1    generated runtime script with your configuration
  logs/
    <yyyyMMdd-HHmmss>_<owner>_<repo>_<workflow>.txt   one file per run
<Desktop>/<shortcut name>.lnk
```

The desktop location is resolved through Windows, so redirected desktops (for example OneDrive) are handled.

To remove a shortcut, delete the `.lnk` from the desktop and the matching `.cmd` and `.ps1` from `~/.github_shortcuts/actions`.

## Running a shortcut

When launched, the runtime script:

1. Re-checks that `gh` is installed and authenticated.
2. Prompts for each input configured as "prompt at run time", enforcing required inputs and validating types.
3. Dispatches the workflow with `gh workflow run` on the configured branch.
4. Finds the resulting run and prints its URL.
5. Streams progress with `gh run watch` until the run completes.
6. Prints the conclusion (success, failure, cancelled, and so on).
7. Keeps the window open until you press a key.

Every step, including the input values used, is appended to the run's log file.

## Prerequisite checks

Before writing any files the command verifies, with a specific error message for each failure:

- `gh` is on `PATH` and authenticated
- the owner exists
- the repository exists and you have write access
- the branch exists
- the workflow exists and is enabled
- the workflow file is present on the chosen branch
- the workflow declares a `workflow_dispatch` trigger

If file creation fails part way through, anything already written is removed.

## Notes and limitations

- Workflows are dispatched by file name (for example `deploy.yml`), which is stable even if the workflow's display name changes.
- Locating the new run is done by polling `gh run list` for a run by the current user created after dispatch. If someone else dispatches the same workflow on the same branch at the same moment, the wrong run could be picked up.
- Fixed input values are stored in plain text in the generated `.ps1`.
- Run the command again to regenerate a shortcut after a workflow's inputs change. The generated files are not meant to be edited by hand.
- The input list is read from the workflow YAML with `powershell-yaml`. Unusual YAML constructs may not parse.

## Troubleshooting

**`gh` not found**: install with `winget install --id GitHub.cli`, then open a new PowerShell window so `PATH` is refreshed.

**Not authenticated**: run `gh auth login` and choose GitHub.com. For organizations that use SSO, run `gh auth refresh` and authorize the organization when prompted.

**powershell-yaml will not download**: the fetch is a plain HTTPS request to the PowerShell Gallery, so a proxy or a blocked host is the usual cause. The package is cached at `%TEMP%\powershell-yaml-<version>.zip`; delete it to force a fresh attempt.

**powershell-yaml did not match its recorded hash**: the package is deleted and the command stops rather than using it. That is either a corrupted download, in which case running it again is enough, or the file served for that version has changed, which is worth understanding before retrying. The expected value is the `$script:YamlSha256` line near the top of `DispatchDesk.psm1`, and it pins that exact version.

**Workflow has no `workflow_dispatch` trigger**: add the following to the workflow file on the branch you want to run from:

```yaml
on:
  workflow_dispatch:
    inputs:
      example:
        description: An example input
        required: false
        default: hello
```

## Tests

```powershell
pwsh -File ./test.ps1
```

88 cases, in plain PowerShell with no test framework, so there is nothing to
install beyond what the module already fetches for itself.

The suite runs inside the module's own session state, through
`& (Get-Module DispatchDesk) { ... }`. That reaches the private helpers, which
are most of the module and none of them exported, and it means a function
defined in that scriptblock shadows the one a module function would otherwise
call. `gh`, the console and every prompt are replaced that way, so the whole
flow can be driven end to end with canned answers: no network, no GitHub token,
and no shortcut landing on the desktop of whoever ran the tests. Because that
trick is what everything else rests on, the first two cases prove it is in
effect rather than assume it, and the suite stops if they fail.

An answer queue stands in for the prompts and throws on a question it was not
expecting, so a case that would have gone interactive fails instead of hanging.
The module's `ActionsDir`, `LogsDir` and `Get-DesktopPath` are pointed at a
temporary directory that is removed afterwards, so a full run really does write
the `.cmd`, the `.ps1` and the `.lnk`, and the tests then read them back.

The one thing not faked is `powershell-yaml`. It is fetched and hash checked
exactly as a first run would do it, because parsing a workflow file is the part
worth testing against the real parser rather than a stand-in. A machine that
cannot reach the gallery cannot run these tests, which is already true of
`check.ps1`.

## Checks

`./check.ps1` runs PSScriptAnalyzer over this directory and then `test.ps1`. It needs PowerShell
7.4.6 or later, which is the analyzer's own floor and well above the 5.1 the
module itself supports, so it is a thing for whoever is editing this directory
rather than for whoever is running it. The `#Requires` line in `check.ps1` says 7.4,
because `#Requires` cannot express a patch version; below 7.4.6 the module
raises its own error saying so.

PSScriptAnalyzer is fetched from the gallery as a package file, checked against
a SHA256 recorded in `check.ps1`, and imported from where it was unpacked. It is
never installed, because `Install-Module -RequiredVersion` pins the version and
verifies nothing about what arrives. That is the same arrangement the module
uses for powershell-yaml at run time, and raising either version means replacing
a version and a hash together.

`PSScriptAnalyzerSettings.psd1` turns four rules off, each with its reason
written beside it. The one worth knowing about is `PSAvoidUsingWriteHost`: the
console is this program's entire user interface, so `Write-Host` is the right
call here, and the rule fires thirty three times across this directory saying
otherwise. `check.ps1` passes that file to the analyzer explicitly rather than
relying on it being found, which the analyzer would do anyway for a file of that
name, because a check should say what it is checking against.

Two more rules would fire, both only in `test.ps1` and both because it shadows
`Read-Host`, `Write-Host` and `Get-Command` on purpose. Those are suppressed on
the three functions themselves, with
`[Diagnostics.CodeAnalysis.SuppressMessageAttribute]` inside each `param` block,
rather than switched off for the directory, which would stop the rules watching
the module. An attribute like that belongs inside the function and before
`param`; put it above the `function` keyword and the analyzer reports
`UnexpectedAttribute` and applies nothing.

The analyzer reads the module and no further. The runtime script the module
generates lives in a here-string, which is a string as far as PowerShell is
concerned, so nothing checks that half. Read it with that in mind, and remember
it is the half that runs every time a shortcut is used.

## History

This tool was its own repository until it moved into the shed, and that
repository has since been deleted. The commits it had before the move did not
come with it, so the history here starts at the move.

It arrived as a single script, `New-WorkflowShortcut.ps1`, with the GitHub owner
a variable to edit at the top of the file. That became this module and the
script was removed, so an older reference to the file name is talking about the
command that now lives in `DispatchDesk.psm1`.

## A workflow to test it against

[`.github/workflows/dungeon-crawl.yml`](../.github/workflows/dungeon-crawl.yml) exists
so this tool has something real to be pointed at. It declares one required and one
optional input of every type `workflow_dispatch` supports, which is `string`, `number`,
`boolean`, `choice` and `environment`, so each of the prompting paths in the command
can be exercised without inventing a workflow each time. The two environments it
offers, `the-undercroft` and `the-tavern`, exist in this repository for that reason
alone and carry no protection rules.

Set the shortcut up against it with `toolshed` as the repository and `Dungeon Crawl`
as the workflow, once this file is on `main`, since GitHub only registers a
`workflow_dispatch` workflow from the default branch. A run descends one room per second, so `rooms` is also how long you
want to watch `gh run watch` stream for. Anything that is not a whole number from 0
to 120 fails the run on purpose, with a message saying why, which is a useful thing
to try from the shortcut too.
