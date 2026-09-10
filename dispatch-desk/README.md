# dispatch-desk

Create Windows desktop shortcuts that trigger GitHub Actions workflows.

Run the setup once per workflow. It walks you through picking a repository, workflow, branch, and values for each `workflow_dispatch` input, verifies everything against GitHub, and drops a shortcut on your desktop. Double-clicking the shortcut opens a console window that prompts for any inputs you chose to leave open, dispatches the workflow, shows the run URL, waits for it to finish, and reports the result.

There are two ways to run it, doing the same thing: `New-WorkflowShortcut.ps1`, a single file you can copy anywhere and run, and `DispatchDesk.psm1`, a module exporting one command. Pick whichever suits; the [Module](#module) section covers what differs.

## Requirements

- Windows with PowerShell 5.1 or later (PowerShell 7 also works)
- [GitHub CLI](https://cli.github.com) installed and authenticated (`gh auth login`)
- Write (push) access to the target repository, which GitHub requires for dispatching workflows
- The [powershell-yaml](https://github.com/cloudbase/powershell-yaml) module, which the setup fetches for itself on first run after asking. Nothing is installed: the package is downloaded from the gallery, checked against a SHA256 recorded beside the version it pins, unpacked under the temporary directory and imported from there. A copy of powershell-yaml already on the machine is neither used nor disturbed, and when the module form loads it the import lands in the module's own session state, so the session you called it from does not gain a `ConvertFrom-Yaml` either.

## Setup

Step 1 is for the script. The module takes the owner as a parameter instead, and the rest of this section applies to both.

1. Open `New-WorkflowShortcut.ps1` and set `$GitHubOwner` to your GitHub username or organization.
2. Run the script from this directory:

   ```powershell
   .\New-WorkflowShortcut.ps1
   ```

   If your execution policy blocks it:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\New-WorkflowShortcut.ps1
   ```

3. Answer the prompts:
   - **Repository**: name only, without the owner.
   - **Branch**: defaults to the repository's default branch. The workflow file must exist on this branch.
   - **Workflow**: chosen from a numbered list of the repository's workflows.
   - **Inputs**: for each `workflow_dispatch` input you choose either a fixed value used on every run, or a prompt shown each time the shortcut runs. Prompted inputs can carry a default that the workflow's own default pre-fills. Choice, boolean, and environment inputs are picked from a list. Optional inputs can be left unset so the workflow default applies.
   - **Shortcut name**: defaults to `<repo> - <workflow name>`.

A summary is shown and confirmed before anything is written.

## Module

`DispatchDesk.psm1` exports one command, `New-WorkflowShortcut`.

```powershell
Import-Module .\DispatchDesk.psm1
New-WorkflowShortcut -Owner mjaksn
```

To have PowerShell find it by name, copy the `.psm1` into a directory called `DispatchDesk` somewhere on `$env:PSModulePath`, such as `Documents\PowerShell\Modules`.

Three things differ from the script, and they are the three things that make it a module rather than a script with a different extension:

- **The owner is a parameter.** `-Owner` is mandatory and positional, so there is no line to edit before first use and no reason to keep one copy of the file per account.
- **A failure throws.** The script prints its message and calls `exit`, which in a module would close the session that called it. The command raises an ordinary terminating error instead, so `try`/`catch` works and the session you called it from is still there afterwards. Cleanup of half-written files happens first either way.
- **A successful run returns an object**, carrying `Owner`, `Repository`, `Branch`, `Workflow`, `WorkflowFile`, `ShortcutName`, `ShortcutPath`, `LauncherPath`, `RunnerPath` and `LogDirectory`.

```powershell
$shortcut = New-WorkflowShortcut -Owner mjaksn
Invoke-Item $shortcut.LauncherPath
```

Everything else, every prompt and every prerequisite check, is the script's behaviour unchanged. Importing the module does not touch the console encoding or the error preference; `New-WorkflowShortcut` sets both for the length of a call, where the script set them at load.

The two files are copies, deliberately, in the same way the `check.ps1` scripts around this repository are copies of each other. They may drift. The script stays a single file that can be copied to a machine and run with nothing else alongside it, which is the property that would be lost by making it a wrapper over the module, and the module stays importable without the script. A change to how a workflow is read or a shortcut is written needs making in both.

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

Before writing any files the setup verifies, with a specific error message for each failure:

- `gh` is on `PATH` and authenticated
- the configured owner exists
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
- Run the setup again to regenerate a shortcut after a workflow's inputs change. The generated files are not meant to be edited by hand.
- The input list is read from the workflow YAML with `powershell-yaml`. Unusual YAML constructs may not parse.

## Troubleshooting

**`gh` not found**: install with `winget install --id GitHub.cli`, then open a new PowerShell window so `PATH` is refreshed.

**Not authenticated**: run `gh auth login` and choose GitHub.com. For organizations that use SSO, run `gh auth refresh` and authorize the organization when prompted.

**powershell-yaml will not download**: the fetch is a plain HTTPS request to the PowerShell Gallery, so a proxy or a blocked host is the usual cause. The package is cached at `%TEMP%\powershell-yaml-<version>.zip`; delete it to force a fresh attempt.

**powershell-yaml did not match its recorded hash**: the script deletes the package and stops rather than using it. That is either a corrupted download, in which case running it again is enough, or the file served for that version has changed, which is worth understanding before retrying. The expected value is the `$YamlSha256` line in whichever of `New-WorkflowShortcut.ps1` and `DispatchDesk.psm1` you ran, and it pins that exact version. Each carries its own copy of the pin, so raising one and not the other leaves them disagreeing.

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

## Checks

`./check.ps1` runs PSScriptAnalyzer over this directory. It needs PowerShell
7.4.6 or later, which is the analyzer's own floor and well above the 5.1 the
tool itself supports, so it is a thing for whoever is editing this directory
rather than for whoever is running it. The `#Requires` line in `check.ps1` says 7.4,
because `#Requires` cannot express a patch version; below 7.4.6 the module
raises its own error saying so.

PSScriptAnalyzer is fetched from the gallery as a package file, checked against
a SHA256 recorded in `check.ps1`, and imported from where it was unpacked. It is
never installed, because `Install-Module -RequiredVersion` pins the version and
verifies nothing about what arrives. That is the same arrangement the tool uses
for powershell-yaml at run time, and raising either version means replacing a
version and a hash together.

`PSScriptAnalyzerSettings.psd1` turns three rules off, each with its reason
written beside it. The one worth knowing about is `PSAvoidUsingWriteHost`: the
console is this program's entire user interface, so `Write-Host` is the right
call here, and the rule fires sixty nine times across this directory saying
otherwise. `check.ps1` passes that file to the analyzer explicitly rather than
relying on it being found, which the analyzer would do anyway for a file of that
name, because a check should say what it is checking against.

The analyzer reads the setup script and the module, and no further than that in
either. The runtime script both of them generate lives in a here-string, which
is a string as far as PowerShell is concerned, so nothing checks that half.
Read it with that in mind, and remember it is the half that runs every time a
shortcut is used.

## History

This tool was its own repository until it moved into the shed, and that
repository has since been deleted. The commits it had before the move did not
come with it, so the history here starts at the move.

## A workflow to test it against

[`.github/workflows/dungeon-crawl.yml`](../.github/workflows/dungeon-crawl.yml) exists
so this tool has something real to be pointed at. It declares one required and one
optional input of every type `workflow_dispatch` supports, which is `string`, `number`,
`boolean`, `choice` and `environment`, so each of the prompting paths in the setup
can be exercised without inventing a workflow each time. The two environments it
offers, `the-undercroft` and `the-tavern`, exist in this repository for that reason
alone and carry no protection rules.

Set the shortcut up against it with `toolshed` as the repository and `Dungeon Crawl`
as the workflow, once this file is on `main`, since GitHub only registers a
`workflow_dispatch` workflow from the default branch. A run descends one room per second, so `rooms` is also how long you
want to watch `gh run watch` stream for. Anything that is not a whole number from 0
to 120 fails the run on purpose, with a message saying why, which is a useful thing
to try from the shortcut too.
