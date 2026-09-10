# install-modules

Put the shed's PowerShell modules where PowerShell will find them, in one
command.

```powershell
./Install-ToolshedModule.ps1 -WhatIf     # say what it would do, write nothing
./Install-ToolshedModule.ps1             # link them for PowerShell 7
./Install-ToolshedModule.ps1 -Edition Both
```

After that, every module in the shed is autoloaded by name in any session. No
`Import-Module` by path, and no copy to keep in step with the checkout.

## Which PowerShell

The two PowerShells keep separate user module directories and neither reads the
other's, so this is a choice rather than something to guess at:

| `-Edition` | PowerShell | Directory |
| --- | --- | --- |
| `Core` (default) | PowerShell 7 | `Documents\PowerShell\Modules` |
| `Desktop` | Windows PowerShell 5.1 | `Documents\WindowsPowerShell\Modules` |
| `Both` | both of the above | |

Windows PowerShell's directory usually does not exist until something puts a
module in it. It gets created.

A module that declares it needs a newer PowerShell than the edition asked for is
reported and left out, rather than installed somewhere it would fail on import.
The declaration is `PowerShellVersion` in the module's `.psd1`, or a
`#Requires -Version` line in its `.psm1`, and a module that declares neither is
installed for either edition. `-Force` installs it anyway.

Desktop is 5.1, which is where Windows PowerShell stopped. Core is the version
of the PowerShell running the script rather than a flat 7.0, so a module asking
for 7.4 is fine on a 7.6 and would not be on a 7.2.

The Documents folder is resolved through Windows rather than built from `$HOME`,
so a redirected Documents, OneDrive being the usual cause, is handled.

## Links, not copies, and never a move

By default each module gets a symbolic link pointing at its tool directory in
this checkout, so editing a module is live in the next session with no
reinstall step.

Nothing is moved. The files stay where git can see them, which is the only
sensible reading of "install" for a module that lives in a repository.

Creating a symbolic link on Windows needs Developer Mode turned on or an
elevated shell. Without either, the link fails and says so, and `-Copy` is the
way through:

```powershell
./Install-ToolshedModule.ps1 -Copy
```

A copy takes the `.psm1` and, if there is one beside it with the same base name,
the `.psd1`, and nothing else. A check script and a test script are for whoever
edits the module, not for whoever runs it. A link, being a view of the checkout,
brings the whole tool directory along; the extra files in it are inert to the
module loader.

## The name is the module's, not the directory's

This is the whole reason the script exists rather than a line in the README of
each module.

A module's name is the base name of its `.psm1`, and that is not always the name
of the tool directory holding it. `dispatch-desk` holds `DispatchDesk.psm1`. A
module directory called `dispatch-desk` would never be autoloaded as
`DispatchDesk`, so copying the tool directory under its own name quietly
produces a module PowerShell will not find.

Linking solves it for free, because PowerShell reads the name of the link and
not of its target: the link is called `DispatchDesk` and points at
`dispatch-desk`.

Nothing here knows the name of any tool. Modules are found by walking the top
level of the shed for `.psm1` files, one directory down, which is what the
layout guarantees. A `.psd1` counts as a manifest only when it sits beside a
`.psm1` of the same base name, because the shed is full of
`PSScriptAnalyzerSettings.psd1` files, including one in this very directory,
and none of them is a manifest.

## Not replacing things

A destination that already holds a real directory, or a link pointing somewhere
else, is left alone and reported. `-Force` is the way past, and it is the only
thing here that could destroy something that is not a copy.

A link that already points at the right place is reported as `AlreadyLinked` and
nothing is written, so running this repeatedly is free.

## Everything it takes

| Parameter | What it does |
| --- | --- |
| `-Edition` | `Core`, `Desktop` or `Both`. Defaults to `Core`. Windows only, since both directories are. |
| `-Destination` | Install into these directories instead, whatever they are. No version is assumed for them, so nothing is held back on compatibility grounds. |
| `-ShedPath` | The root of the shed. Defaults to the directory above this script. |
| `-Name` | Install only the named modules. A name matching nothing is an error, because a typo that silently installs nothing is worse than a stop. |
| `-Copy` | Copy instead of link. |
| `-Force` | Replace what is there, and install a module into an edition older than it asks for. |
| `-WhatIf` | Say what would happen and write nothing. |

It returns one object per module per destination, carrying `Name`, `Edition`,
`Action`, `Destination`, `Source` and `Reason`, so a result can be acted on
rather than read off the console. `Action` is one of `Linked`, `Copied`,
`AlreadyLinked`, `Skipped`, `Incompatible`, `WouldLink` or `WouldCopy`.

## What it needs

Windows, and PowerShell 7 to run it in, whichever editions it installs for. It
uses only what PowerShell and .NET ship with.

The Windows part is not incidental. `-Edition` resolves the user module
directories under Documents, and Windows PowerShell 5.1 exists nowhere else. On
another platform that switch stops and says so, rather than building a path
whose separator is an ordinary character in a directory name; `-Destination`
names a directory outright and is the way through.

## Tests

```powershell
pwsh -File ./test.ps1
```

46 cases, in plain PowerShell with no test framework, so there is nothing to
install.

Everything runs against a synthetic shed built in a temporary directory and
installs into another temporary directory, so a script whose whole job is
writing into Documents never writes into Documents while being tested. The
synthetic shed also carries the cases the real shed happens not to have: a tool
directory whose name matches its module, a hidden directory, a `.psd1` with no
`.psm1` beside it, and a module that declares it needs PowerShell 7.

The edition cases are the exception and run against the real Documents paths,
because working those out is the thing being tested. They run under `-WhatIf`,
which an earlier case proves writes nothing. The `What if:` lines they print
cannot be redirected, since `ShouldProcess` writes them straight to the host,
and they are the proof that the `-WhatIf` path really ran.

The last two cases are the point of all of it: a module is installed through a
link whose name differs from its target, and then found by name from both
PowerShell 7 and Windows PowerShell 5.1.

Symbolic links need Developer Mode or an elevated shell. Where they cannot be
created, the link cases report as skipped rather than passing quietly, and the
copy cases still run.

Three cases cover the Windows guard by setting `$env:OS` to something else for
the length of them, which is what the guard reads, and putting it back
afterwards.

`check.ps1` runs PSScriptAnalyzer over this directory and then those tests, and
is what CI runs, on `windows-latest`, per `ci.json`. The analyzer is fetched
from the gallery as a package, verified against a recorded hash and imported
from where it was unpacked, never installed, which is the same arrangement the
other PowerShell tools here use. `PSScriptAnalyzerSettings.psd1` turns two rules
off, both about the check and the tests rather than the installer, each with its
reason written beside it.

## Why this is a tool and not a root script

`AGENTS.md` says there is no repository-wide setup command, and that a
root-level harness reaching into every tool is the shared-library mistake
wearing different clothes. That rule is right and this does not break it.

This is a tool like any other: its own directory, its own README, its own tests,
its own `ci.json`. The root README and `AGENTS.md` gained an inventory line
each, as they would for any tool, and nothing in the workflow changed, which is
the part the rule is about. It imports nothing from another tool and no other
tool imports it. It reads the top level
by convention, the same way `.github/changed_tools.py` finds the tools with a
`ci.json`, and like that script it names none of them.

Running it is not a setup step for the shed. A checkout is still ready to read,
and a tool is still set up by following its own README. This one is for the
separate matter of having the modules on hand in every session.
