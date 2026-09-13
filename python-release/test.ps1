#Requires -Version 7.4
<#
.SYNOPSIS
    Tests for python-release.ps1. Run by check.ps1, and runnable by hand.

.DESCRIPTION
    Plain PowerShell with no test framework, so there is nothing to install.

    Each case builds a small Python project in a temporary directory, commits
    it, and pushes it to a bare repository standing in for origin, so fetching,
    pushing branches and pushing tags all happen for real and offline. Nothing
    talks to GitHub: gh is replaced with a function that records what it was
    asked, and every prompt is answered from a queue.

    Git reads a throwaway global configuration for the length of the run, with
    the system one switched off, so an identity, a signing setting or a hook on
    the machine running the tests cannot change what they see.

    Exits non-zero if any case failed.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:passed = 0
$script:failed = 0

function Assert-Equal {
    param([string]$Case, $Expected, $Actual)
    if ("$Expected" -ceq "$Actual") {
        $script:passed++
        Write-Output "ok   $Case"
    }
    else {
        $script:failed++
        Write-Output "FAIL $Case"
        Write-Output "     expected [$Expected], got [$Actual]"
    }
}

function Assert-True {
    param([string]$Case, $Condition, [string]$Detail = '')
    if ($Condition) {
        $script:passed++
        Write-Output "ok   $Case"
    }
    else {
        $script:failed++
        Write-Output "FAIL $Case"
        if ($Detail) { Write-Output "     $Detail" }
    }
}

function Assert-Throw {
    param([string]$Case, [scriptblock]$Action, [string]$Contains)
    try {
        & $Action | Out-Null
        $script:failed++
        Write-Output "FAIL $Case"
        Write-Output '     nothing was thrown'
    }
    catch {
        $message = $_.Exception.Message
        if ($message.Contains($Contains)) {
            $script:passed++
            Write-Output "ok   $Case"
        }
        else {
            $script:failed++
            Write-Output "FAIL $Case"
            Write-Output "     expected a message containing [$Contains], got [$message]"
        }
    }
}

# Runs a case that is expected to succeed, so that an unexpected throw is
# reported as that case failing rather than ending the whole run.
function Invoke-Scenario {
    param([string]$Case, [scriptblock]$Action)
    try {
        & $Action
    }
    catch {
        $script:failed++
        Write-Output "FAIL $Case"
        Write-Output "     threw: $($_.Exception.Message)"
    }
}

# ---------------------------------------------------------------------------
# The script under test, with its outside world replaced
# ---------------------------------------------------------------------------

$work = Join-Path ([System.IO.Path]::GetTempPath()) "python-release-test-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $work | Out-Null

. (Join-Path $PSScriptRoot 'python-release.ps1') -RepositoryPath $work

$script:Answers = [Collections.Generic.Queue[string]]::new()
$script:Said = [Collections.Generic.List[string]]::new()
$script:GhCalls = [Collections.Generic.List[string]]::new()
$script:GhFound = $true
$script:GhAuthExit = 0
$script:GhCreateExit = 0
$script:PrBody = $null

function Read-Host {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidOverwritingBuiltInCmdlets', '',
        Justification = 'Shadowed on purpose so prompts come from the queue instead of a person.')]
    param($Prompt)
    if ($script:Answers.Count -eq 0) { throw "Unexpected prompt: $Prompt" }
    return $script:Answers.Dequeue()
}

function Write-Host {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidOverwritingBuiltInCmdlets', '',
        Justification = 'Shadowed on purpose so the script prints nothing and what it says can be checked.')]
    param([Parameter(ValueFromRemainingArguments)]$Object)
    $script:Said.Add("$Object")
}

function Find-Gh {
    if ($script:GhFound) { return [pscustomobject]@{ Source = 'gh' } }
}

function Invoke-Gh {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', 'WorkingDirectory',
        Justification = 'Declared so the call binds; the fake has no directory to be in.')]
    param([string]$WorkingDirectory, [string[]]$Arguments)
    $script:GhCalls.Add($Arguments -join ' ')
    if ($Arguments[0] -eq 'auth') {
        return [pscustomobject]@{ ExitCode = $script:GhAuthExit; Output = @('You are not logged into any GitHub hosts.') }
    }
    if ($Arguments[0] -eq 'pr') {
        $index = [array]::IndexOf($Arguments, '--body-file')
        $script:PrBody = [IO.File]::ReadAllText($Arguments[$index + 1])
        return [pscustomobject]@{ ExitCode = $script:GhCreateExit; Output = @('https://github.com/example/sample/pull/7') }
    }
    throw "Unexpected gh call: $($Arguments -join ' ')"
}

function Get-Today { '2026-09-12' }

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

$savedGlobal = $env:GIT_CONFIG_GLOBAL
$savedNoSystem = $env:GIT_CONFIG_NOSYSTEM
$gitConfig = Join-Path $work 'gitconfig'
[IO.File]::WriteAllText($gitConfig, "[user]`n`tname = python-release test`n`temail = test@example.invalid`n[init]`n`tdefaultBranch = main`n")
$env:GIT_CONFIG_GLOBAL = $gitConfig
$env:GIT_CONFIG_NOSYSTEM = '1'

function Invoke-TestGit {
    param([string]$Repository, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    $output = @(& git -C $Repository @Arguments 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE -ne 0) { throw "test setup: git $($Arguments -join ' ') failed: $($output -join ' ')" }
    return $output
}

# Fixture text is written with LF endings whatever this file was checked out
# with, so a case that wants CRLF says so.
function Write-FixtureFile {
    param([string]$Path, [string]$Text, [switch]$Crlf)
    $Text = $Text -replace "`r`n", "`n"
    if ($Crlf) { $Text = $Text -replace "`n", "`r`n" }
    New-Item -ItemType Directory -Path (Split-Path $Path) -Force | Out-Null
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

$ChangelogUnreleased = @'
# Changelog

Notable changes to sample.

## [Unreleased]

### Added

- A new thing.

## [0.5.1] - 2026-09-01

### Fixed

- An old thing.

[Unreleased]: https://github.com/example/sample/compare/v0.5.1...HEAD
[0.5.1]: https://github.com/example/sample/releases/tag/v0.5.1
'@

$ChangelogReleased = @'
# Changelog

Notable changes to sample.

## [0.5.1] - 2026-09-01

### Fixed

- An old thing.

## [0.5.0] - 2026-08-20

- The first thing.

[0.5.1]: https://github.com/example/sample/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/example/sample/releases/tag/v0.5.0
'@

function Get-SampleFile {
    param([string]$Version = '0.5.1', [string]$Changelog = $ChangelogUnreleased)
    [ordered]@{
        'pyproject.toml'        = @"
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "sample-pkg"
version = "$Version"
description = "A sample"

[tool.ruff]
target-version = "py311"
"@
        'sample_pkg/__init__.py' = @"
"""A sample package."""

__version__ = "$Version"
"@
        'README.md'             = @"
# sample

``````python
import sample_pkg
sample_pkg.__version__          # "$Version"
``````
"@
        'docs/openapi.json'     = @"
{
  "openapi": "3.1.0",
  "components": {
    "schemas": {
      "Info": {
        "properties": {
          "version": {
            "title": "Version",
            "type": "string"
          }
        }
      }
    }
  },
  "info": {
    "title": "sample",
    "version": "$Version"
  }
}
"@
        'CHANGELOG.md'          = $Changelog
    }
}

# A committed project on main, pushed to a bare origin beside it.
function Build-Fixture {
    param([System.Collections.IDictionary]$Files = (Get-SampleFile), [string[]]$Crlf = @())
    $root = Join-Path $work ([guid]::NewGuid().ToString('N').Substring(0, 8))
    $origin = Join-Path $root 'origin.git'
    $repo = Join-Path $root 'repo'
    Invoke-TestGit $work init --quiet --bare $origin | Out-Null
    Invoke-TestGit $work init --quiet $repo | Out-Null
    foreach ($name in $Files.Keys) {
        Write-FixtureFile -Path (Join-Path $repo $name) -Text $Files[$name] -Crlf:($Crlf -contains $name)
    }
    Invoke-TestGit $repo add --all | Out-Null
    Invoke-TestGit $repo commit --quiet --message 'Initial' | Out-Null
    Invoke-TestGit $repo remote add origin $origin | Out-Null
    Invoke-TestGit $repo push --quiet --set-upstream origin main | Out-Null
    return $repo
}

function Read-Fixture {
    param([string]$Repository, [string]$Name)
    [IO.File]::ReadAllText((Join-Path $Repository $Name))
}

function Invoke-Release {
    param([string]$Repository, [string[]]$Answer)
    $script:Answers.Clear()
    foreach ($a in $Answer) { $script:Answers.Enqueue($a) }
    $script:Said.Clear()
    $script:GhCalls.Clear()
    $script:PrBody = $null
    Invoke-PythonRelease -RepositoryPath $Repository
}

function Get-RemoteRef {
    param([string]$Repository, [string]$Ref)
    "$(Invoke-TestGit $Repository ls-remote origin $Ref)"
}

function Get-Status {
    param([string]$Repository)
    "$(Invoke-TestGit $Repository status --porcelain --untracked-files=no)"
}

function Get-Branch {
    param([string]$Repository)
    "$(Invoke-TestGit $Repository branch --show-current)"
}

try {
    # -----------------------------------------------------------------------
    Write-Output '# version comparison'
    # -----------------------------------------------------------------------
    Invoke-Scenario 'a higher patch is accepted' {
        Assert-VersionHigher -CurrentVersion '0.5.1' -NewVersion '0.5.2'
        Assert-True 'a higher patch is accepted' $true
    }
    Invoke-Scenario 'versions compare as numbers, not text' {
        Assert-VersionHigher -CurrentVersion '0.9.9' -NewVersion '0.10.0'
        Assert-True 'versions compare as numbers, not text' $true
    }
    Assert-Throw 'an equal version is refused' { Assert-VersionHigher -CurrentVersion '0.5.1' -NewVersion '0.5.1' } 'not higher'
    Assert-Throw 'a lower version is refused' { Assert-VersionHigher -CurrentVersion '0.5.1' -NewVersion '0.4.9' } 'not higher'
    Assert-Throw 'two parts is refused' { Assert-VersionHigher -CurrentVersion '0.5.1' -NewVersion '0.6' } 'MAJOR.MINOR.PATCH'
    Assert-Throw 'a leading v is refused' { Assert-VersionHigher -CurrentVersion '0.5.1' -NewVersion 'v0.6.0' } 'MAJOR.MINOR.PATCH'

    # -----------------------------------------------------------------------
    Write-Output '# changelog reading'
    # -----------------------------------------------------------------------
    $released = $ChangelogReleased -replace "`r`n", "`n"
    Assert-Equal 'a section is lifted out without its heading' "### Fixed`n`n- An old thing." (Get-ChangelogSection -Text $released -Version '0.5.1')
    Assert-Equal 'the last section ends at the link references' '- The first thing.' (Get-ChangelogSection -Text $released -Version '0.5.0')
    Assert-Throw 'a missing section is refused' { Get-ChangelogSection -Text $released -Version '0.6.0' } "no '## [0.6.0]' section"
    Assert-Throw 'a repeated heading is ambiguous' {
        Get-ChangelogSection -Text ($released -replace '## \[0\.5\.0\]', '## [0.5.1]') -Version '0.5.1'
    } 'ambiguous'
    Assert-Throw 'an empty section is refused' {
        Get-ChangelogSection -Text "## [1.0.0] - 2026-01-01`n`n## [0.9.0] - 2025-01-01`n`n- x`n`n[1.0.0]: u`n" -Version '1.0.0'
    } 'empty'
    Assert-Throw 'a section with nothing after it is ambiguous' {
        Get-ChangelogSection -Text "## [1.0.0] - 2026-01-01`n`n- x`n" -Version '1.0.0'
    } 'ambiguous'

    Assert-True 'a compare link is copied from the newest one' `
        ((Add-ChangelogLink -Text $released -CurrentVersion '0.5.1' -NewVersion '0.6.0').Contains("`n[0.6.0]: https://github.com/example/sample/compare/v0.5.1...v0.6.0`n[0.5.1]: "))
    $tagStyle = "## [0.5.1] - x`n`n- y`n`n[0.5.1]: https://github.com/example/sample/releases/tag/v0.5.1`n"
    Assert-Equal 'a release link is copied from the newest one' `
        "## [0.5.1] - x`n`n- y`n`n[0.6.0]: https://github.com/example/sample/releases/tag/v0.6.0`n[0.5.1]: https://github.com/example/sample/releases/tag/v0.5.1`n" `
        (Add-ChangelogLink -Text $tagStyle -CurrentVersion '0.5.1' -NewVersion '0.6.0')
    Assert-Equal 'a changelog with no links gets none' "## [0.5.1] - x`n`n- y`n" `
        (Add-ChangelogLink -Text "## [0.5.1] - x`n`n- y`n" -CurrentVersion '0.5.1' -NewVersion '0.6.0')

    # -----------------------------------------------------------------------
    Write-Output '# before a goal is chosen'
    # -----------------------------------------------------------------------
    $repo = Build-Fixture
    $script:GhFound = $false
    Assert-Throw 'a missing gh is reported' { Invoke-Release $repo @('1') } 'gh, was not found'
    Assert-Equal 'and nothing was asked' 1 $script:Answers.Count
    $script:GhFound = $true

    $script:GhAuthExit = 1
    Assert-Throw 'an unauthenticated gh is reported' { Invoke-Release $repo @('1') } 'not signed in'
    $script:GhAuthExit = 0

    $plain = Join-Path $work 'not-a-repo'
    New-Item -ItemType Directory -Path $plain | Out-Null
    Assert-Throw 'a directory outside git is refused' { Invoke-Release $plain @('1') } 'not inside a git repository'
    Assert-Throw 'a path that does not exist is refused' { Invoke-Release (Join-Path $work 'nowhere') @('1') } 'does not exist'
    Assert-Throw 'an answer that is not a choice is refused' { Invoke-Release $repo @('3') } 'not one of the choices'

    # -----------------------------------------------------------------------
    Write-Output '# bump: the whole way through, from an [Unreleased] section'
    # -----------------------------------------------------------------------
    Invoke-Scenario 'bump from [Unreleased]' {
        $repo = Build-Fixture
        Write-FixtureFile -Path (Join-Path $repo 'scratch.txt') -Text 'untracked'
        Invoke-Release $repo @('1', '0.6.0', 'y', 'y')

        Assert-True 'pyproject.toml is bumped' ((Read-Fixture $repo 'pyproject.toml') -clike '*version = "0.6.0"*')
        Assert-True 'and its other quoted values are not' ((Read-Fixture $repo 'pyproject.toml') -clike '*target-version = "py311"*')
        Assert-True '__init__.py is bumped' ((Read-Fixture $repo 'sample_pkg/__init__.py') -clike '*__version__ = "0.6.0"*')
        Assert-True 'README.md is bumped' ((Read-Fixture $repo 'README.md') -clike '*sample_pkg.__version__          # "0.6.0"*')
        $openapi = Read-Fixture $repo 'docs/openapi.json'
        Assert-True 'openapi.json is bumped' ($openapi -clike '*"version": "0.6.0"*')
        Assert-True 'and its version schema is untouched' ($openapi -clike '*"version": {*"title": "Version"*')
        Assert-True 'no old version is left in them' (-not ((@('pyproject.toml', 'sample_pkg/__init__.py', 'README.md', 'docs/openapi.json') |
                    ForEach-Object { Read-Fixture $repo $_ }) -join '').Contains('0.5.1'))

        $changelog = Read-Fixture $repo 'CHANGELOG.md'
        Assert-True 'the [Unreleased] heading becomes the release' ($changelog.Contains("sample.`n`n## [0.6.0] - 2026-09-12`n`n### Added"))
        Assert-True 'its link becomes the release link' ($changelog.Contains("`n[0.6.0]: https://github.com/example/sample/releases/tag/v0.6.0`n[0.5.1]: "))
        Assert-True 'and no [Unreleased] is left' (-not $changelog.Contains('Unreleased'))

        Assert-Equal 'the commit is on a release branch' 'release-0.6.0' (Get-Branch $repo)
        Assert-Equal 'the commit is called Release 0.6.0' 'Release 0.6.0' "$(Invoke-TestGit $repo log -1 --format=%s)"
        Assert-Equal 'the commit holds the five files' 'CHANGELOG.md docs/openapi.json pyproject.toml README.md sample_pkg/__init__.py' `
            ((Invoke-TestGit $repo show --name-only --format= HEAD | Sort-Object) -join ' ')
        Assert-Equal 'nothing tracked is left uncommitted' '' (Get-Status $repo)
        Assert-True 'an untracked file did not stop it' (Test-Path (Join-Path $repo 'scratch.txt'))
        Assert-True 'the branch was pushed' ((Get-RemoteRef $repo 'refs/heads/release-0.6.0') -like "$(Invoke-TestGit $repo rev-parse HEAD)*")
        Assert-Equal 'gh opened the pull request against main' 'pr create --base main --head release-0.6.0 --title Release 0.6.0 --body-file' `
            (($script:GhCalls | Where-Object { $_ -like 'pr *' }) -replace ' [^ ]+$', '')
        Assert-Equal 'the body is the changelog section' "### Added`n`n- A new thing.`n" $script:PrBody
    }

    # -----------------------------------------------------------------------
    Write-Output '# bump: an entry typed in'
    # -----------------------------------------------------------------------
    Invoke-Scenario 'bump with a typed entry' {
        $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
        Invoke-Release $repo @('1', '0.5.2', '', '### Fixed', '', '- The widget.', '', '.', 'y', 'y')
        $changelog = Read-Fixture $repo 'CHANGELOG.md'
        Assert-True 'the entry goes above the newest release' ($changelog.Contains("sample.`n`n## [0.5.2] - 2026-09-12`n`n### Fixed`n`n- The widget.`n`n## [0.5.1] - 2026-09-01"))
        Assert-True 'a compare link is added' ($changelog.Contains("`n[0.5.2]: https://github.com/example/sample/compare/v0.5.1...v0.5.2`n[0.5.1]: "))
        Assert-Equal 'the body is the typed entry' "### Fixed`n`n- The widget.`n" $script:PrBody
    }

    Assert-Throw 'an empty typed entry is refused' {
        $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
        $script:repoForEmpty = $repo
        Invoke-Release $repo @('1', '0.5.2', '', '.')
    } 'No changelog entry'
    Assert-Equal 'and nothing was changed' '' (Get-Status $script:repoForEmpty)
    Assert-Equal 'nor was a branch made' 'main' (Get-Branch $script:repoForEmpty)

    Invoke-Scenario 'bump with an existing section' {
        $existing = $ChangelogReleased -replace '## \[0\.5\.1\] - 2026-09-01', "## [0.6.0] - 2026-09-11`n`n- Already written.`n`n## [0.5.1] - 2026-09-01"
        $repo = Build-Fixture -Files (Get-SampleFile -Changelog $existing)
        $before = Read-Fixture $repo 'CHANGELOG.md'
        Invoke-Release $repo @('1', '0.6.0', 'y', 'y')
        Assert-Equal 'an existing section is left as it is' $before (Read-Fixture $repo 'CHANGELOG.md')
        Assert-True 'and it is not in the commit' (-not ((Invoke-TestGit $repo show --name-only --format= HEAD) -contains 'CHANGELOG.md'))
        Assert-Equal 'the body is that section' "- Already written.`n" $script:PrBody
    }

    Invoke-Scenario 'bump with only the required places' {
        $files = Get-SampleFile
        $files.Remove('README.md')
        $files.Remove('docs/openapi.json')
        $repo = Build-Fixture -Files $files
        Invoke-Release $repo @('1', '1.0.0', 'y', 'y')
        Assert-Equal 'the optional places are not required' 'CHANGELOG.md pyproject.toml sample_pkg/__init__.py' `
            ((Invoke-TestGit $repo show --name-only --format= HEAD | Sort-Object) -join ' ')
    }

    Invoke-Scenario 'bump with a src layout' {
        $files = Get-SampleFile
        $files['src/sample_pkg/__init__.py'] = $files['sample_pkg/__init__.py']
        $files.Remove('sample_pkg/__init__.py')
        $repo = Build-Fixture -Files $files
        Invoke-Release $repo @('1', '0.6.0', 'y', 'y')
        Assert-True 'src/<package>/__init__.py is bumped' ((Read-Fixture $repo 'src/sample_pkg/__init__.py') -clike '*__version__ = "0.6.0"*')
    }

    Invoke-Scenario 'bump keeps CRLF' {
        $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased) -Crlf @('pyproject.toml', 'CHANGELOG.md')
        Invoke-Release $repo @('1', '0.5.2', '- The widget.', '.', 'y', 'y')
        $pyproject = Read-Fixture $repo 'pyproject.toml'
        Assert-True 'a CRLF file stays CRLF' ($pyproject.Contains("version = ""0.5.2""`r`n") -and -not ($pyproject -match '[^\r]\n'))
        $changelog = Read-Fixture $repo 'CHANGELOG.md'
        Assert-True 'and a typed entry is written with CRLF' ($changelog.Contains("## [0.5.2] - 2026-09-12`r`n`r`n- The widget.`r`n`r`n## [0.5.1]") -and -not ($changelog -match '[^\r]\n'))
        Assert-Equal 'the body is sent with LF' "- The widget.`n" $script:PrBody
    }

    Invoke-Scenario 'bump keeps the encoding' {
        $repo = Build-Fixture
        $readme = Join-Path $repo 'README.md'
        $openapi = Join-Path $repo 'docs/openapi.json'
        [IO.File]::WriteAllText($readme, [IO.File]::ReadAllText($readme), [Text.UnicodeEncoding]::new($false, $true))
        [IO.File]::WriteAllText($openapi, [IO.File]::ReadAllText($openapi), [Text.UTF8Encoding]::new($true))
        Invoke-TestGit $repo commit --quiet --all --message 'Encodings' | Out-Null
        Invoke-TestGit $repo push --quiet origin main | Out-Null
        Invoke-Release $repo @('1', '0.6.0', 'y', 'y')
        $bytes = [IO.File]::ReadAllBytes($readme)
        Assert-True 'a UTF-16 file keeps its byte order mark' ($bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE)
        Assert-True 'and stays UTF-16' ([Text.Encoding]::Unicode.GetString($bytes, 2, $bytes.Length - 2).Contains('# "0.6.0"'))
        $bytes = [IO.File]::ReadAllBytes($openapi)
        Assert-True 'a UTF-8 file keeps its byte order mark' ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF -and $bytes[3] -eq [byte][char]'{')
        $bytes = [IO.File]::ReadAllBytes((Join-Path $repo 'pyproject.toml'))
        Assert-Equal 'and a file with no mark gains none' ([byte][char]'[') $bytes[0]
    }

    # A push URL that goes nowhere fails the push while ls-remote, which reads
    # the fetch URL, still passes every check before it.
    $repo = Build-Fixture
    Invoke-TestGit $repo remote set-url --push origin (Join-Path $work 'no-such-remote.git') | Out-Null
    Assert-Throw 'a failed branch push says the commit is local' { Invoke-Release $repo @('1', '0.6.0', 'y') } 'only on the local branch release-0.6.0'
    Assert-Equal 'and it is' 'Release 0.6.0' "$(Invoke-TestGit $repo log -1 --format=%s)"

    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
    Invoke-TestGit $repo remote set-url --push origin (Join-Path $work 'no-such-remote.git') | Out-Null
    Assert-Throw 'a failed tag push says the tag is local' { Invoke-Release $repo @('2', 'y') } 'git tag --delete v0.5.1'
    Assert-Equal 'and it is' 'tag' "$(Invoke-TestGit $repo cat-file -t v0.5.1)"

    Invoke-Scenario 'bump declined before the push' {
        $repo = Build-Fixture
        Invoke-Release $repo @('1', '0.6.0', 'n')
        Assert-Equal 'declining the push leaves the commit local' 'Release 0.6.0' "$(Invoke-TestGit $repo log -1 --format=%s)"
        Assert-Equal 'and pushes nothing' '' (Get-RemoteRef $repo 'refs/heads/release-0.6.0')
        Assert-Equal 'and opens nothing' 0 @($script:GhCalls | Where-Object { $_ -like 'pr *' }).Count
        Assert-True 'and says how to push' (($script:Said -join "`n") -like '*git push -u origin release-0.6.0*')
    }

    Invoke-Scenario 'bump declined before the pull request' {
        $repo = Build-Fixture
        Invoke-Release $repo @('1', '0.6.0', 'y', 'no')
        Assert-True 'declining the pull request still pushed' ((Get-RemoteRef $repo 'refs/heads/release-0.6.0') -ne '')
        Assert-Equal 'and opens nothing' 0 @($script:GhCalls | Where-Object { $_ -like 'pr *' }).Count
    }

    $script:GhCreateExit = 1
    Assert-Throw 'a failed gh pr create is reported' { Invoke-Release (Build-Fixture) @('1', '0.6.0', 'y', 'y') } 'gh pr create failed'
    $script:GhCreateExit = 0

    # -----------------------------------------------------------------------
    Write-Output '# bump: refusals'
    # -----------------------------------------------------------------------
    $repo = Build-Fixture
    Add-Content -LiteralPath (Join-Path $repo 'README.md') -Value 'more'
    Assert-Throw 'an unstaged change is refused' { Invoke-Release $repo @('1') } 'README.md'

    $repo = Build-Fixture
    Write-FixtureFile -Path (Join-Path $repo 'new.py') -Text 'x = 1'
    Invoke-TestGit $repo add new.py | Out-Null
    Assert-Throw 'a staged change is refused' { Invoke-Release $repo @('1') } 'staged or unstaged'

    $files = Get-SampleFile
    $files['README.md'] = $files['README.md'] -replace '0\.5\.1', '0.5.0'
    $repo = Build-Fixture -Files $files
    Assert-Throw 'an inconsistent version is refused' { Invoke-Release $repo @('1') } 'README.md says 0.5.0'

    $files = Get-SampleFile
    $files['sample_pkg/__init__.py'] += "`n__version__ = ""0.5.1""`n"
    $repo = Build-Fixture -Files $files
    Assert-Throw 'two __version__ lines are ambiguous' { Invoke-Release $repo @('1') } 'ambiguous'

    $files = Get-SampleFile
    $files['pyproject.toml'] = $files['pyproject.toml'] -replace '(?m)^version = .*$', 'dynamic = ["version"]'
    $repo = Build-Fixture -Files $files
    Assert-Throw 'a pyproject.toml with no static version is refused' { Invoke-Release $repo @('1') } 'no version = "..." line'

    $repo = Build-Fixture
    Invoke-TestGit $repo switch --quiet --create feature | Out-Null
    Assert-Throw 'a checkout off main is refused' { Invoke-Release $repo @('1') } "on the branch 'feature'"

    $repo = Build-Fixture
    Invoke-TestGit $repo commit --quiet --allow-empty --message 'Ahead' | Out-Null
    Assert-Throw 'main ahead of origin is refused' { Invoke-Release $repo @('1') } "origin's main"

    $repo = Build-Fixture
    Assert-Throw 'a lower new version is refused' { Invoke-Release $repo @('1', '0.5.0') } 'not higher'
    Assert-Equal 'and nothing was changed' '' (Get-Status $repo)
    Assert-Equal 'nor was a branch made' 'main' (Get-Branch $repo)

    $repo = Build-Fixture
    Invoke-TestGit $repo push --quiet origin main:refs/heads/release-0.6.0 | Out-Null
    Assert-Throw 'a release branch already on origin is refused' { Invoke-Release $repo @('1', '0.6.0') } 'already exists on origin'

    $doubled = $ChangelogReleased -replace '## \[0\.5\.0\]', "## [0.6.0] - x`n`n- a`n`n## [0.6.0] - y`n`n- b`n`n## [0.5.0]"
    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $doubled)
    Assert-Throw 'an ambiguous changelog stops the bump' { Invoke-Release $repo @('1', '0.6.0') } 'ambiguous'
    Assert-Equal 'before anything is written' '' (Get-Status $repo)
    Assert-Equal 'or branched' 'main' (Get-Branch $repo)

    # -----------------------------------------------------------------------
    Write-Output '# tag'
    # -----------------------------------------------------------------------
    Invoke-Scenario 'tag the whole way through' {
        $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
        Invoke-Release $repo @('2', 'y')
        Assert-Equal 'an annotated tag is made' 'tag' "$(Invoke-TestGit $repo cat-file -t v0.5.1)"
        Assert-Equal 'on the tip of main' "$(Invoke-TestGit $repo rev-parse HEAD)" "$(Invoke-TestGit $repo rev-parse 'v0.5.1^{commit}')"
        Assert-True 'and pushed to origin' ((Get-RemoteRef $repo 'refs/tags/v0.5.1') -ne '')
        Assert-Equal 'without gh' 'auth status' ($script:GhCalls -join ';')
    }

    Invoke-Scenario 'tag declined' {
        $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
        Invoke-Release $repo @('2', '')
        Assert-Equal 'declining makes no tag' '' "$(Invoke-TestGit $repo tag --list)"
        Assert-Equal 'and pushes none' '' (Get-RemoteRef $repo 'refs/tags/v0.5.1')
    }

    Assert-Throw 'a version with no changelog section is refused' {
        Invoke-Release (Build-Fixture -Files (Get-SampleFile -Version '0.5.2' -Changelog $ChangelogReleased)) @('2')
    } "no '## [0.5.2]' section"

    $files = Get-SampleFile -Changelog $ChangelogReleased
    $files['docs/openapi.json'] = $files['docs/openapi.json'] -replace '"version": "0\.5\.1"', '"version": "0.5.2"'
    Assert-Throw 'an inconsistent version is refused before tagging' { Invoke-Release (Build-Fixture -Files $files) @('2') } 'docs/openapi.json says 0.5.2'

    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
    Invoke-TestGit $repo tag v0.5.1 | Out-Null
    Assert-Throw 'a local tag is refused' { Invoke-Release $repo @('2') } 'already exists locally'

    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
    Invoke-TestGit $repo tag v0.5.1 | Out-Null
    Invoke-TestGit $repo push --quiet origin v0.5.1 | Out-Null
    Invoke-TestGit $repo tag --delete v0.5.1 | Out-Null
    Assert-Throw 'a tag only on origin is refused' { Invoke-Release $repo @('2') } 'already exists on origin'

    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
    Invoke-TestGit $repo switch --quiet --detach HEAD | Out-Null
    Assert-Throw 'a detached checkout is refused' { Invoke-Release $repo @('2') } 'not on any branch'

    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
    Invoke-TestGit $repo commit --quiet --allow-empty --message 'Pushed' | Out-Null
    Invoke-TestGit $repo push --quiet origin main | Out-Null
    Invoke-TestGit $repo reset --quiet --hard HEAD~1 | Out-Null
    Assert-Throw 'main behind origin is refused' { Invoke-Release $repo @('2') } "origin's main"

    $repo = Build-Fixture -Files (Get-SampleFile -Changelog $ChangelogReleased)
    Add-Content -LiteralPath (Join-Path $repo 'pyproject.toml') -Value '# edited'
    Assert-Throw 'a changed tracked file is refused before tagging' { Invoke-Release $repo @('2') } 'pyproject.toml'
}
finally {
    $env:GIT_CONFIG_GLOBAL = $savedGlobal
    $env:GIT_CONFIG_NOSYSTEM = $savedNoSystem
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ''
Write-Output "$($script:passed) passed, $($script:failed) failed"
if ($script:failed -gt 0) { exit 1 }
