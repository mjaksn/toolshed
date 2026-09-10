# RemoveNewline

A PowerShell module with one command, `Remove-Newline`, which strips every
line break from a text file and joins what is left into a single line. It
knows every form a line break takes: CRLF, LF, a bare CR, and the Unicode NEL,
LINE SEPARATOR and PARAGRAPH SEPARATOR characters.

```
Remove-Newline .\notes.txt
Remove-Newline .\notes.txt -Separator ' '
Remove-Newline .\notes.txt -Destination .\notes.oneline.txt
Get-ChildItem *.log | Remove-Newline -Destination .\flattened
```

The file is rewritten in place unless `-Destination` names somewhere else,
which can be a file path or an existing directory; given a directory, each
result lands there under its source name. `-Separator` puts text where each
line break was, and one CRLF counts as one break. Wildcards work, and file
objects can be piped in. It supports `-WhatIf`.

The encoding survives. A file that opens with a byte order mark, for UTF-8,
UTF-16 in either byte order or UTF-32 in either, is read and written in that
encoding with the mark kept. A file without one is treated as UTF-8 and
written back without one, so nothing gains a mark it did not have.

## What it needs

Windows PowerShell 5.1 or later, PowerShell 7 included. It uses only what
PowerShell and .NET ship with.

## Installing it

Copy this directory, or only `RemoveNewline.psm1` inside a directory of the
same name, into any directory on `$env:PSModulePath`: `Documents\PowerShell\Modules`
for PowerShell 7, or `Documents\WindowsPowerShell\Modules` for Windows
PowerShell 5.1, which are separate and neither of which is read by the other.
PowerShell then loads it on first use. Or import it by path from anywhere:

```
Import-Module .\RemoveNewline.psm1
```

## Tests

```
pwsh -File ./test.ps1
powershell -File ./test.ps1     # and under Windows PowerShell 5.1
```

20 cases, in plain PowerShell with no test framework, so there is nothing to
install. Each writes a file with known bytes, runs the command, and compares
the bytes that came out. They pass under both PowerShells, which is what backs
the version claim above rather than leaving it as an assertion.

`check.ps1` runs PSScriptAnalyzer over the directory and then those tests, and
is what CI runs, on `windows-latest`, per `ci.json`. The analyzer is fetched
from the gallery as a package, verified against a recorded hash and imported
from where it was unpacked, never installed; the two rules it is not applying,
and why, are in `PSScriptAnalyzerSettings.psd1`. The first run takes about a
minute for the fetch and later runs use the cached package.

## History

This lived in my PowerShell profile's module directory, where it is still
used, and is committed here as written.
