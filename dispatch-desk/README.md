# dispatch-desk

Create Windows desktop shortcuts that trigger GitHub Actions workflows.

Run the setup script once per workflow. It walks you through picking a repository, workflow, branch, and values for each `workflow_dispatch` input, verifies everything against GitHub, and drops a shortcut on your desktop. Double-clicking the shortcut opens a console window that prompts for any inputs you chose to leave open, dispatches the workflow, shows the run URL, waits for it to finish, and reports the result.

## Requirements

- Windows with PowerShell 5.1 or later (PowerShell 7 also works)
- [GitHub CLI](https://cli.github.com) installed and authenticated (`gh auth login`)
- Write (push) access to the target repository, which GitHub requires for dispatching workflows
- The [powershell-yaml](https://github.com/cloudbase/powershell-yaml) module, which the setup script fetches for itself on first run after asking. Nothing is installed: the package is downloaded from the gallery, checked against a SHA256 recorded in the script, unpacked under the temporary directory and imported from there. A copy of powershell-yaml already on the machine is neither used nor disturbed.

## Setup

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

The script confirms a summary before writing anything.

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

Before writing any files the setup script verifies, with a specific error message for each failure:

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
- Re-run the setup script to regenerate a shortcut after a workflow's inputs change. The generated files are not meant to be edited by hand.
- The input list is read from the workflow YAML with `powershell-yaml`. Unusual YAML constructs may not parse.

## Troubleshooting

**`gh` not found**: install with `winget install --id GitHub.cli`, then open a new PowerShell window so `PATH` is refreshed.

**Not authenticated**: run `gh auth login` and choose GitHub.com. For organizations that use SSO, run `gh auth refresh` and authorize the organization when prompted.

**powershell-yaml will not download**: the fetch is a plain HTTPS request to the PowerShell Gallery, so a proxy or a blocked host is the usual cause. The package is cached at `%TEMP%\powershell-yaml-<version>.zip`; delete it to force a fresh attempt.

**powershell-yaml did not match its recorded hash**: the script deletes the package and stops rather than using it. That is either a corrupted download, in which case running it again is enough, or the file served for that version has changed, which is worth understanding before retrying. The expected value is the `$YamlSha256` line near the top of `New-WorkflowShortcut.ps1`, and it pins that exact version.

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

## History

This tool was its own repository, [mjaksn/dispatch-desk](https://github.com/mjaksn/dispatch-desk),
until it moved into the shed. The commits it had before the move are still
there, so that is where to look for why something is the way it is.
