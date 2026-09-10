#Requires -Version 7
<#
.SYNOPSIS
    Tests for DispatchDesk. Run by check.ps1, and runnable by hand.

.DESCRIPTION
    Plain PowerShell with no test framework, so there is nothing to install or
    pin beyond what the module already fetches for itself.

    The whole suite runs inside the module's own session state, through
    & (Get-Module DispatchDesk) { ... }, for two reasons. It reaches the
    private helpers, which are most of the module and are not exported. And a
    function defined in that scriptblock shadows the one a module function
    would otherwise call, because a module function called from there gets the
    scriptblock as its parent scope. That is how gh, the console and the
    prompts are replaced with canned answers, and it is what lets the flow be
    driven end to end without a network, a GitHub token, or a shortcut landing
    on somebody's desktop.

    Since that trick is the foundation everything else rests on, the first
    thing the suite does is prove it is in effect rather than assume it.

    Nothing is written outside a temporary directory: the module's own
    ActionsDir, LogsDir and Get-DesktopPath are all pointed there for the
    duration. The one thing that is real is powershell-yaml, fetched and hash
    checked exactly as a user's first run would do it, because parsing a
    workflow file is the part worth testing against the real parser.

    Exits non-zero if any case failed.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'DispatchDesk.psd1') -Force
$module = Get-Module DispatchDesk

$work = Join-Path ([System.IO.Path]::GetTempPath()) "dispatchdesk-test-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $work | Out-Null

try {
    $cases = & $module {
        param($work)

        $results = [System.Collections.Generic.List[object]]::new()

        function Add-Result {
            param([string]$Name, [bool]$Ok, [string]$Detail = '')
            $results.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail })
        }

        function Assert-Equal {
            param([string]$Name, $Expected, $Actual)
            $same = if ($null -eq $Expected) { $null -eq $Actual } else { "$Expected" -ceq "$Actual" }
            Add-Result $Name $same "expected [$Expected], got [$Actual]"
        }

        function Assert-True {
            param([string]$Name, $Condition, [string]$Detail = '')
            Add-Result $Name ([bool]$Condition) $Detail
        }

        function Assert-Throws {
            param([string]$Name, [scriptblock]$Action, [string]$Contains)
            try {
                & $Action | Out-Null
                Add-Result $Name $false 'nothing was thrown'
            }
            catch {
                $message = $_.Exception.Message
                Add-Result $Name ($message -like "*$Contains*") "expected a message containing [$Contains], got [$message]"
            }
        }

        # -------------------------------------------------------------
        # The shadows, and the proof that they are in effect
        # -------------------------------------------------------------
        $script:Answers = [System.Collections.Queue]::new()
        $script:GhRules = @()
        $script:GhCalls = [System.Collections.Generic.List[string]]::new()
        $script:TestDesktop = Join-Path $work 'desktop'

        # Overwriting a built-in is the whole technique here rather than an
        # oversight, so the rule against it is suppressed at each of the three
        # sites rather than switched off for the directory, which would stop it
        # watching the module.
        function Read-Host {
            [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidOverwritingBuiltInCmdlets', '',
                Justification = 'Shadowed on purpose so prompts come from the queue instead of a person.')]
            param($Prompt)
            if ($script:Answers.Count -eq 0) { throw "Unexpected prompt: $Prompt" }
            return $script:Answers.Dequeue()
        }

        # The console is the module's user interface and says nothing a test
        # needs, so it is swallowed rather than mixed into the results. The
        # parameter exists to absorb whatever the call passed and is meant to
        # go nowhere.
        function Write-Host {
            [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidOverwritingBuiltInCmdlets', '',
                Justification = 'Shadowed on purpose so the module prints nothing during a test run.')]
            [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', '',
                Justification = 'Discarding the arguments is the point of this one.')]
            param([Parameter(ValueFromRemainingArguments)]$Ignored)
        }

        # ErrorAction is declared only so the module's own
        # Get-Command gh -ErrorAction SilentlyContinue binds; the answer is the
        # same either way.
        function Get-Command {
            [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidOverwritingBuiltInCmdlets', '',
                Justification = 'Shadowed on purpose so the tests do not need gh installed.')]
            [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSReviewUnusedParameter', '',
                Justification = 'Declared to absorb the caller argument, not to be read.')]
            param($Name, $ErrorAction)
            if ($Name -eq 'gh') { return [pscustomobject]@{ Source = 'C:\test\gh.exe' } }
            return Microsoft.PowerShell.Core\Get-Command -Name $Name -ErrorAction SilentlyContinue
        }

        function Invoke-Gh {
            param([string[]]$Arguments)
            $key = $Arguments -join ' '
            $script:GhCalls.Add($key)
            foreach ($rule in $script:GhRules) {
                if ($key -like $rule.Match) {
                    return [pscustomobject]@{
                        ExitCode = $rule.ExitCode
                        StdOut   = $rule.StdOut
                        StdErr   = $rule.StdErr
                    }
                }
            }
            throw "Unexpected gh call: $key"
        }

        function Get-DesktopPath { return $script:TestDesktop }

        # Prove the shadow reaches a module function before anything relies on
        # it. Read-Text is module code; if it sees the queue, so will the rest.
        $script:Answers.Enqueue('probe-value')
        $probe = Read-Text -Prompt 'probe'
        Assert-Equal 'shadow reaches module code' 'probe-value' $probe
        Assert-Equal 'shadow consumed exactly one answer' 0 $script:Answers.Count
        if ($probe -ne 'probe-value') {
            throw 'The Read-Host shadow is not in effect, so nothing below would mean anything.'
        }

        # -------------------------------------------------------------
        # ConvertTo-SafeFileName
        # -------------------------------------------------------------
        Assert-Equal 'safe name replaces invalid characters' 'a_b_c' (ConvertTo-SafeFileName 'a/b\c')
        Assert-Equal 'safe name collapses a run of spaces' 'a_b' (ConvertTo-SafeFileName 'a   b')
        # A tab is both whitespace and an invalid file name character, and the
        # invalid character pass runs first, so it becomes an underscore rather
        # than being collapsed with the spaces around it.
        Assert-Equal 'safe name replaces a tab before collapsing' 'a_b' (ConvertTo-SafeFileName "a`tb")
        Assert-Equal 'safe name collapses spaces around a tab separately' 'a___b' (ConvertTo-SafeFileName "a `t b")
        Assert-Equal 'safe name trims underscores and dots' 'name' (ConvertTo-SafeFileName '__name._')
        Assert-Equal 'safe name falls back when nothing is left' 'workflow' (ConvertTo-SafeFileName '   ')
        Assert-Equal 'safe name keeps an ordinary name' 'deploy' (ConvertTo-SafeFileName 'deploy')

        # -------------------------------------------------------------
        # ConvertTo-PsLiteral
        # -------------------------------------------------------------
        Assert-Equal 'literal of null' '$null' (ConvertTo-PsLiteral $null)
        Assert-Equal 'literal of true' '$true' (ConvertTo-PsLiteral $true)
        Assert-Equal 'literal of false' '$false' (ConvertTo-PsLiteral $false)
        Assert-Equal 'literal of a string' "'plain'" (ConvertTo-PsLiteral 'plain')
        Assert-Equal 'literal doubles a single quote' "'it''s'" (ConvertTo-PsLiteral "it's")
        Assert-Equal 'literal of an array' "@('a', 'b')" (ConvertTo-PsLiteral @('a', 'b'))
        Assert-Equal 'literal of a number' "'7'" (ConvertTo-PsLiteral 7)

        # -------------------------------------------------------------
        # YAML scalar helpers
        # -------------------------------------------------------------
        Assert-Equal 'scalar of a true boolean is lowercase' 'true' (ConvertTo-YamlScalarString $true)
        Assert-Equal 'scalar of a false boolean is lowercase' 'false' (ConvertTo-YamlScalarString $false)
        Assert-Equal 'scalar of null stays null' $null (ConvertTo-YamlScalarString $null)
        Assert-Equal 'scalar of a string is the string' 'hello' (ConvertTo-YamlScalarString 'hello')

        Assert-Equal 'truthy of a true boolean' 'True' (Test-YamlTruthy $true)
        Assert-Equal 'truthy of a false boolean' 'False' (Test-YamlTruthy $false)
        Assert-Equal 'truthy of the word true' 'True' (Test-YamlTruthy 'true')
        Assert-Equal 'truthy ignores case' 'True' (Test-YamlTruthy 'TRUE')
        Assert-Equal 'truthy of the word false' 'False' (Test-YamlTruthy 'false')
        Assert-Equal 'truthy of null' 'False' (Test-YamlTruthy $null)

        # -------------------------------------------------------------
        # Get-YamlKey
        # -------------------------------------------------------------
        $map = [ordered]@{ Name = 'value'; other = 1 }
        Assert-Equal 'yaml key exact match' 'value' (Get-YamlKey $map 'Name')
        Assert-Equal 'yaml key ignores case' 'value' (Get-YamlKey $map 'name')
        Assert-Equal 'yaml key missing is null' $null (Get-YamlKey $map 'absent')
        Assert-Equal 'yaml key on a non-dictionary is null' $null (Get-YamlKey 'a string' 'name')
        Assert-Equal 'yaml key on null is null' $null (Get-YamlKey $null 'name')

        # A YAML parser is entitled to read the bare word "on" as a boolean, so
        # the true key has to answer to the name "on".
        $booleanKeyed = @{ $true = 'the on block' }
        Assert-Equal 'yaml key finds a boolean true key as on' 'the on block' (Get-YamlKey $booleanKeyed 'on')

        # -------------------------------------------------------------
        # Resolve-Workflow
        # -------------------------------------------------------------
        $workflows = @(
            [pscustomobject]@{ name = 'CI'; path = '.github/workflows/ci.yml'; state = 'active' },
            [pscustomobject]@{ name = 'Dungeon Crawl'; path = '.github/workflows/dungeon-crawl.yml'; state = 'active' }
        )
        Assert-Equal 'workflow resolves by file name' 'Dungeon Crawl' (Resolve-Workflow -Workflows $workflows -Name 'dungeon-crawl.yml').name
        Assert-Equal 'workflow resolves by display name' 'Dungeon Crawl' (Resolve-Workflow -Workflows $workflows -Name 'Dungeon Crawl').name
        Assert-Equal 'workflow resolves ignoring case' 'CI' (Resolve-Workflow -Workflows $workflows -Name 'ci.YML').name
        Assert-Equal 'workflow that matches nothing is null' $null (Resolve-Workflow -Workflows $workflows -Name 'absent.yml')

        $ambiguous = @(
            [pscustomobject]@{ name = 'deploy.yml'; path = '.github/workflows/one.yml'; state = 'active' },
            [pscustomobject]@{ name = 'Deploy'; path = '.github/workflows/deploy.yml'; state = 'active' }
        )
        Assert-Throws 'two workflows answering to one name gives up' {
            Resolve-Workflow -Workflows $ambiguous -Name 'deploy.yml'
        } 'More than one workflow answers'

        # -------------------------------------------------------------
        # Stop-WithError
        # -------------------------------------------------------------
        $leftover = Join-Path $work 'leftover.txt'
        Set-Content -LiteralPath $leftover -Value 'half written'
        $script:CreatedFiles = @($leftover)
        Assert-Throws 'error message carries every line' {
            Stop-WithError @('First line.', 'Second line.')
        } 'Second line.'
        Assert-True 'error removes a partially created file' (-not (Test-Path -LiteralPath $leftover))
        Assert-Equal 'error resets the created file list' 0 $script:CreatedFiles.Count

        $script:CreatedFiles = @()
        Assert-Throws 'error with nothing written says only its own lines' {
            Stop-WithError @('Just this.')
        } 'Just this.'

        # -------------------------------------------------------------
        # Get-WorkflowDispatchInputs, against the real parser
        # -------------------------------------------------------------
        # Answered yes in case the package is not cached yet. Doing this here
        # rather than inside the flow tests keeps the answer queues below exact
        # whether or not powershell-yaml was already on the machine.
        $script:Answers.Enqueue('y')
        Initialize-YamlModule
        $script:Answers.Clear()

        $full = @'
name: Everything
on:
  workflow_dispatch:
    inputs:
      rooms:
        description: How many
        type: number
        required: true
      torches:
        type: number
        default: 3
      hardcore:
        type: boolean
        required: true
      difficulty:
        type: choice
        default: normal
        options:
          - gentle
          - normal
          - brutal
      plain:
        description: No type given
'@
        $parsed = Get-WorkflowDispatchInputs -YamlText $full
        Assert-Equal 'dispatch trigger is found' 'True' $parsed.HasDispatch
        Assert-Equal 'every input is read' 5 @($parsed.Inputs).Count

        $byName = @{}
        foreach ($i in $parsed.Inputs) { $byName[$i.Name] = $i }
        Assert-Equal 'a number input keeps its type' 'number' $byName['rooms'].Type
        Assert-Equal 'a required input is required' 'True' $byName['rooms'].Required
        Assert-Equal 'an unmarked input is not required' 'False' $byName['torches'].Required
        Assert-Equal 'a default is read' '3' $byName['torches'].Default
        Assert-Equal 'a description is read' 'How many' $byName['rooms'].Description
        Assert-Equal 'an input with no type is a string' 'string' $byName['plain'].Type
        Assert-Equal 'a boolean input offers true and false' 'true,false' ($byName['hardcore'].Options -join ',')
        Assert-Equal 'a choice input keeps its options' 'gentle,normal,brutal' ($byName['difficulty'].Options -join ',')
        Assert-Equal 'a choice default is read' 'normal' $byName['difficulty'].Default

        $bareString = Get-WorkflowDispatchInputs -YamlText "name: X`non: workflow_dispatch`n"
        Assert-Equal 'on as a bare string is recognised' 'True' $bareString.HasDispatch

        $asList = Get-WorkflowDispatchInputs -YamlText "name: X`non:`n  - push`n  - workflow_dispatch`n"
        Assert-Equal 'on as a list is recognised' 'True' $asList.HasDispatch

        $noDispatch = Get-WorkflowDispatchInputs -YamlText "name: X`non:`n  push:`n    branches: [main]`n"
        Assert-Equal 'a workflow with no dispatch says so' 'False' $noDispatch.HasDispatch
        Assert-Equal 'a workflow with no dispatch has no inputs' 0 @($noDispatch.Inputs).Count

        # -------------------------------------------------------------
        # The whole flow, writing into the temporary directory
        # -------------------------------------------------------------
        $script:ActionsDir = Join-Path $work 'actions'
        $script:LogsDir    = Join-Path $work 'logs'
        New-Item -ItemType Directory -Path $script:TestDesktop -Force | Out-Null

        $flowYaml = @'
name: Test Workflow
on:
  workflow_dispatch:
    inputs:
      target:
        description: Where to
        type: choice
        required: true
        options:
          - alpha
          - beta
      note:
        description: Anything to add
        type: string
'@

        function Set-GhRules {
            param([string]$WorkflowYaml)
            if (-not $WorkflowYaml) { $WorkflowYaml = $flowYaml }
            $script:GhCalls.Clear()
            $script:GhRules = @(
                @{ Match = 'auth status'; ExitCode = 0; StdOut = 'ok'; StdErr = '' },
                @{ Match = 'api user *'; ExitCode = 0; StdOut = 'mjaksn'; StdErr = '' },
                @{ Match = 'api users/*'; ExitCode = 0; StdOut = '{"login":"mjaksn","type":"User"}'; StdErr = '' },
                @{ Match = 'api repos/mjaksn/toolshed'; ExitCode = 0; StdErr = ''
                   StdOut = '{"name":"toolshed","full_name":"mjaksn/toolshed","default_branch":"main","permissions":{"push":true}}' },
                @{ Match = 'api repos/*/git/ref/heads/*'; ExitCode = 0; StdOut = '{"ref":"refs/heads/main"}'; StdErr = '' },
                @{ Match = 'api repos/*/actions/workflows*'; ExitCode = 0; StdErr = ''
                   StdOut = '{"workflows":[{"name":"CI","path":".github/workflows/ci.yml","state":"active"},{"name":"Test Workflow","path":".github/workflows/test.yml","state":"active"}]}' },
                @{ Match = 'api repos/*/contents/*'; ExitCode = 0; StdOut = $WorkflowYaml; StdErr = '' }
            )
        }

        # target is given a fixed value (mode 1, option 2 = beta); note is left
        # to prompt at run time (mode 2) with no default (n); then yes to write.
        Set-GhRules
        $script:Answers.Clear()
        @('1', '2', '2', 'n', 'y') | ForEach-Object { $script:Answers.Enqueue($_) }

        $made = New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main `
            -Workflow test.yml -ShortcutName 'Test Shortcut'

        Assert-Equal 'the flow consumes exactly the answers it needs' 0 $script:Answers.Count
        Assert-Equal 'the result names the owner' 'mjaksn' $made.Owner
        Assert-Equal 'the result names the repository' 'mjaksn/toolshed' $made.Repository
        Assert-Equal 'the result names the branch' 'main' $made.Branch
        Assert-Equal 'the result names the workflow' 'Test Workflow' $made.Workflow
        Assert-Equal 'the result names the workflow file' 'test.yml' $made.WorkflowFile
        Assert-Equal 'the result names the shortcut' 'Test Shortcut' $made.ShortcutName

        Assert-True 'the launcher is written' (Test-Path -LiteralPath $made.LauncherPath)
        Assert-True 'the runtime is written' (Test-Path -LiteralPath $made.RunnerPath)
        Assert-True 'the desktop shortcut is written' (Test-Path -LiteralPath $made.ShortcutPath)
        Assert-Equal 'the runner files are named after the shortcut' 'Test_Shortcut.ps1' (Split-Path $made.RunnerPath -Leaf)
        Assert-Equal 'the desktop shortcut keeps the unsafe name' 'Test Shortcut.lnk' (Split-Path $made.ShortcutPath -Leaf)
        Assert-Equal 'nothing is left queued for cleanup' 0 $script:CreatedFiles.Count

        $runtime = Get-Content -LiteralPath $made.RunnerPath -Raw
        Assert-True 'the runtime carries the owner' ($runtime -match "\`$Owner\s+= 'mjaksn'")
        Assert-True 'the runtime carries the repository' ($runtime -match "\`$Repo\s+= 'toolshed'")
        Assert-True 'the runtime carries the branch' ($runtime -match "\`$Branch\s+= 'main'")
        Assert-True 'the runtime carries the workflow file' ($runtime -match "\`$WorkflowFile\s+= 'test\.yml'")
        Assert-True 'the runtime carries the fixed input' ($runtime -match "Name = 'target'.+Mode = 'fixed'.+Value = 'beta'")
        Assert-True 'the runtime carries the prompted input' ($runtime -match "Name = 'note'.+Mode = 'prompt'")
        Assert-True 'no placeholder is left unreplaced' ($runtime -notmatch '__[A-Z_]+__')

        $launcher = Get-Content -LiteralPath $made.LauncherPath -Raw
        Assert-True 'the launcher points at the runtime' ($launcher -match 'Test_Shortcut\.ps1')
        Assert-True 'the launcher sets a title' ($launcher -match 'title Test Shortcut')

        # -------------------------------------------------------------
        # The flow, refused at the summary
        # -------------------------------------------------------------
        Set-GhRules
        $script:Answers.Clear()
        @('1', '2', '2', 'n', 'n') | ForEach-Object { $script:Answers.Enqueue($_) }
        Assert-Throws 'declining the summary stops' {
            New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main `
                -Workflow test.yml -ShortcutName 'Refused'
        } 'Nothing was written'
        Assert-True 'declining writes no launcher' (-not (Test-Path -LiteralPath (Join-Path $script:ActionsDir 'Refused.cmd')))
        Assert-True 'declining writes no shortcut' (-not (Test-Path -LiteralPath (Join-Path $script:TestDesktop 'Refused.lnk')))

        # -------------------------------------------------------------
        # The parameters that answer a question ahead of time
        # -------------------------------------------------------------
        # Naming the workflow by its display name rather than its file. This is
        # the case that would fail if the chosen workflow were assigned back to
        # a variable called $workflow, since that is the -Workflow parameter
        # under another capitalisation.
        Set-GhRules
        $script:Answers.Clear()
        @('1', '2', '2', 'n', 'y') | ForEach-Object { $script:Answers.Enqueue($_) }
        $byDisplayName = New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main `
            -Workflow 'Test Workflow' -ShortcutName 'By Display Name'
        Assert-Equal 'a workflow named by display name resolves' 'test.yml' $byDisplayName.WorkflowFile

        # An owner/name pair in -Repository has its owner half dropped.
        Set-GhRules
        $script:Answers.Clear()
        @('1', '2', '2', 'n', 'y') | ForEach-Object { $script:Answers.Enqueue($_) }
        $slug = New-WorkflowShortcut -Owner mjaksn -Repository 'mjaksn/toolshed' -Branch main `
            -Workflow test.yml -ShortcutName 'From A Slug'
        Assert-Equal 'a slug in the repository name is accepted' 'mjaksn/toolshed' $slug.Repository
        Assert-True 'the repository was looked up by name alone' ($script:GhCalls -contains 'api repos/mjaksn/toolshed')

        # A workflow the repository does not have.
        Set-GhRules
        $script:Answers.Clear()
        Assert-Throws 'an unknown workflow name is refused' {
            New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main `
                -Workflow absent.yml -ShortcutName 'Never'
        } "No workflow in mjaksn/toolshed is called 'absent.yml'"

        # -------------------------------------------------------------
        # Prerequisites that fail
        # -------------------------------------------------------------
        Set-GhRules
        $script:GhRules = @(@{ Match = 'auth status'; ExitCode = 1; StdOut = ''; StdErr = 'not logged in' })
        $script:Answers.Clear()
        Assert-Throws 'an unauthenticated gh is refused' {
            New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main -Workflow test.yml -ShortcutName 'Never'
        } 'not authenticated'

        Set-GhRules
        $script:GhRules = @($script:GhRules | Where-Object { $_.Match -ne 'api repos/mjaksn/toolshed' }) + @(
            @{ Match = 'api repos/mjaksn/toolshed'; ExitCode = 0; StdErr = ''
               StdOut = '{"name":"toolshed","full_name":"mjaksn/toolshed","default_branch":"main","permissions":{"push":false}}' }
        )
        $script:Answers.Clear()
        Assert-Throws 'a repository without write access is refused' {
            New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main -Workflow test.yml -ShortcutName 'Never'
        } 'do not have write access'

        Set-GhRules -WorkflowYaml "name: Test Workflow`non:`n  push:`n    branches: [main]`n"
        $script:Answers.Clear()
        Assert-Throws 'a workflow with no dispatch trigger is refused' {
            New-WorkflowShortcut -Owner mjaksn -Repository toolshed -Branch main -Workflow test.yml -ShortcutName 'Never'
        } 'no workflow_dispatch trigger'

        , $results
    } $work

    $script:failed = 0
    foreach ($case in $cases) {
        if ($case.Ok) {
            Write-Output "ok   $($case.Name)"
        }
        else {
            $script:failed++
            Write-Output "FAIL $($case.Name)"
            if ($case.Detail) { Write-Output "     $($case.Detail)" }
        }
    }

    Write-Output ''
    Write-Output "$($cases.Count) cases, $($cases.Count - $script:failed) passed, $($script:failed) failed."
}
finally {
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Exited explicitly, and outside the finally, so that check.ps1 has a
# $LASTEXITCODE to read. A script that simply runs off its end leaves the
# variable untouched, which under Set-StrictMode is an error rather than a nil.
if ($script:failed -gt 0) { exit 1 }
exit 0
