<#
.SYNOPSIS
    Creates a desktop shortcut that runs a GitHub Actions workflow (workflow_dispatch).

.DESCRIPTION
    Interactively collects a repository, workflow, branch and workflow inputs, verifies
    every prerequisite against GitHub, then writes a runner script pair
    (.cmd launcher + .ps1 runtime) under ~/.github_shortcuts/actions and places a
    .lnk shortcut on the desktop that launches it.

    Requirements:
      - Windows
      - GitHub CLI (gh) installed and authenticated with write access to the repository
      - PowerShell 5.1 or later
      - The powershell-yaml module (the script offers to install it on first run)

.NOTES
    Edit the $GitHubOwner value in the configuration section below before running.
#>

# =====================================================================
# Configuration
# =====================================================================
$GitHubOwner = 'your-user-or-org'
# =====================================================================

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'

$ShortcutRoot = Join-Path $HOME '.github_shortcuts'
$ActionsDir   = Join-Path $ShortcutRoot 'actions'
$LogsDir      = Join-Path $ShortcutRoot 'logs'
$script:CreatedFiles = @()

# ---------------------------------------------------------------------
# Output and error helpers
# ---------------------------------------------------------------------
function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Green
}

function Write-Info {
    param([string]$Message)
    Write-Host "    $Message" -ForegroundColor Gray
}

function Stop-WithError {
    param([string[]]$Lines)
    Write-Host ""
    Write-Host "ERROR: $($Lines[0])" -ForegroundColor Red
    if ($Lines.Count -gt 1) {
        $Lines[1..($Lines.Count - 1)] | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    }
    if ($script:CreatedFiles.Count -gt 0) {
        Write-Host ""
        Write-Host "Cleaning up partially created files..." -ForegroundColor Yellow
        foreach ($f in $script:CreatedFiles) {
            if (Test-Path -LiteralPath $f) {
                Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue
                Write-Host "  removed $f" -ForegroundColor Yellow
            }
        }
    }
    exit 1
}

# ---------------------------------------------------------------------
# gh helpers
# ---------------------------------------------------------------------
function Invoke-Gh {
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & gh @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    $stdout = @($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { [string]$_ }) -join "`n"
    $stderr = @($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() }) -join "`n"
    [pscustomobject]@{ ExitCode = $code; StdOut = $stdout; StdErr = $stderr }
}

function Invoke-GhApi {
    # Returns @{ Ok; NotFound; Data; Error }
    param([string]$Endpoint, [string[]]$ExtraArguments = @())
    $result = Invoke-Gh (@('api', $Endpoint) + $ExtraArguments)
    if ($result.ExitCode -eq 0) {
        return [pscustomobject]@{ Ok = $true; NotFound = $false; Data = $result.StdOut; Error = '' }
    }
    $notFound = ($result.StdErr -match 'HTTP 404') -or ($result.StdOut -match 'HTTP 404')
    $message = if ($result.StdErr) { $result.StdErr } else { $result.StdOut }
    return [pscustomobject]@{ Ok = $false; NotFound = $notFound; Data = $result.StdOut; Error = $message }
}

# ---------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------
function Read-Text {
    param(
        [string]$Prompt,
        [string]$Default = '',
        [switch]$Required
    )
    while ($true) {
        $suffix = if ($Default) { " [$Default]" } else { '' }
        $answer = Read-Host "$Prompt$suffix"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            if ($Default) { return $Default }
            if (-not $Required) { return '' }
            Write-Host "    A value is required." -ForegroundColor Yellow
            continue
        }
        return $answer.Trim()
    }
}

function Read-YesNo {
    param([string]$Prompt, [bool]$Default = $true)
    $hint = if ($Default) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $answer = Read-Host "$Prompt $hint"
        if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
        if ($answer -match '^(y|yes)$') { return $true }
        if ($answer -match '^(n|no)$') { return $false }
        Write-Host "    Please answer y or n." -ForegroundColor Yellow
    }
}

function Select-FromList {
    # Displays a numbered list and returns the chosen zero-based index.
    param(
        [string]$Prompt,
        [string[]]$Items,
        [int]$DefaultIndex = -1
    )
    for ($i = 0; $i -lt $Items.Count; $i++) {
        $marker = if ($i -eq $DefaultIndex) { ' (default)' } else { '' }
        Write-Host ("    {0,3}. {1}{2}" -f ($i + 1), $Items[$i], $marker)
    }
    while ($true) {
        $hint = if ($DefaultIndex -ge 0) { " [Enter for $($DefaultIndex + 1)]" } else { '' }
        $answer = Read-Host "$Prompt (1-$($Items.Count))$hint"
        if ([string]::IsNullOrWhiteSpace($answer) -and $DefaultIndex -ge 0) { return $DefaultIndex }
        $number = 0
        if ([int]::TryParse($answer, [ref]$number) -and $number -ge 1 -and $number -le $Items.Count) {
            return $number - 1
        }
        Write-Host "    Enter a number between 1 and $($Items.Count)." -ForegroundColor Yellow
    }
}

function Read-ParameterValue {
    # Prompts for a single workflow input value honoring its type.
    # Returns $null when the user leaves an optional value empty (meaning: omit it).
    param(
        [string]$Name,
        [string]$Type,
        [string[]]$Options,
        [string]$Default,
        [bool]$Required,
        [string]$Prompt = 'Value'
    )
    $options = @($Options | Where-Object { $null -ne $_ })
    if ($options.Count -gt 0) {
        $defaultIndex = -1
        if ($Default) { $defaultIndex = [array]::IndexOf($options, $Default) }
        for ($i = 0; $i -lt $options.Count; $i++) {
            $marker = if ($i -eq $defaultIndex) { ' (default)' } else { '' }
            Write-Host ("    {0,3}. {1}{2}" -f ($i + 1), $options[$i], $marker)
        }
        while ($true) {
            $hint = ''
            if ($defaultIndex -ge 0) { $hint = " [Enter for $($defaultIndex + 1)]" }
            elseif (-not $Required) { $hint = ' [Enter to leave unset]' }
            $answer = Read-Host "$Prompt (1-$($options.Count))$hint"
            if ([string]::IsNullOrWhiteSpace($answer)) {
                if ($defaultIndex -ge 0) { return $options[$defaultIndex] }
                if (-not $Required) { return $null }
                Write-Host "    A choice is required." -ForegroundColor Yellow
                continue
            }
            $number = 0
            if ([int]::TryParse($answer, [ref]$number) -and $number -ge 1 -and $number -le $options.Count) {
                return $options[$number - 1]
            }
            if ($options -ccontains $answer.Trim()) { return $answer.Trim() }
            Write-Host "    Enter a number between 1 and $($options.Count)." -ForegroundColor Yellow
        }
    }

    while ($true) {
        $hint = ''
        if ($Default) { $hint = " [$Default]" }
        elseif (-not $Required) { $hint = ' [Enter to leave unset]' }
        $answer = Read-Host "$Prompt$hint"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            if ($Default) { return $Default }
            if (-not $Required) { return $null }
            Write-Host "    A value is required." -ForegroundColor Yellow
            continue
        }
        $answer = $answer.Trim()
        if ($Type -eq 'number') {
            $parsed = 0.0
            if (-not [double]::TryParse($answer, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
                Write-Host "    This input is numeric. Enter a number." -ForegroundColor Yellow
                continue
            }
        }
        return $answer
    }
}

# ---------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------
# powershell-yaml, pinned by version and by hash. Raising the version means
# replacing both, and the hash is the SHA256 of the .nupkg the gallery serves for
# that version. Install-Module cannot express this: -RequiredVersion pins the
# version and nothing checks what actually arrived.
$YamlVersion = '0.4.12'
$YamlSha256  = 'd4602bc7a4a093766520422d53ca8b09acde162286fae11e2ee6c8edfea07810'

function Initialize-YamlModule {
    # Fetched from the gallery as a package file, checked against the recorded
    # hash, and imported from where it was unpacked. It is never installed, so
    # whatever else is on the machine is neither used nor disturbed.
    $loaded = @(Get-Module -Name powershell-yaml)[0]
    if ($loaded -and $loaded.Version -eq [version]$YamlVersion) { return }

    $root = Join-Path ([System.IO.Path]::GetTempPath()) "powershell-yaml-$YamlVersion"
    $pkg  = "$root.zip"
    $psd1 = Join-Path $root 'powershell-yaml.psd1'

    if (-not (Test-Path -LiteralPath $pkg)) {
        Write-Host ""
        Write-Host "The powershell-yaml module is needed to read workflow inputs and is not here yet." -ForegroundColor Yellow
        if (-not (Read-YesNo "Fetch powershell-yaml $YamlVersion from the PowerShell Gallery now?")) {
            Stop-WithError @(
                'powershell-yaml is required.',
                'Answering yes installs nothing. The package is cached at',
                "  $pkg",
                'and can be deleted at any time.'
            )
        }
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            Write-Info "Fetching powershell-yaml $YamlVersion..."
            # Saved under a .zip name because that is what a .nupkg is, and
            # because Expand-Archive has been particular about the extension.
            Invoke-WebRequest -UseBasicParsing -MaximumRedirection 5 -OutFile $pkg `
                -Uri "https://www.powershellgallery.com/api/v2/package/powershell-yaml/$YamlVersion"
        }
        catch {
            Stop-WithError @(
                "Could not fetch powershell-yaml $YamlVersion : $($_.Exception.Message)",
                'Check the network and any proxy, then run this script again.'
            )
        }
    }

    # Checked on every run, including against an already cached file. A package
    # that fails is deleted rather than left where the next run would reuse it.
    $actual = (Get-FileHash -LiteralPath $pkg -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $YamlSha256) {
        Remove-Item -LiteralPath $pkg -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $root) {
            Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
        }
        Stop-WithError @(
            "powershell-yaml $YamlVersion did not match its recorded hash.",
            "  expected $YamlSha256",
            "  got      $actual",
            'The package has been deleted. Do not run this again until you know why it changed.'
        )
    }

    if (-not (Test-Path -LiteralPath $psd1)) {
        try { Expand-Archive -LiteralPath $pkg -DestinationPath $root -Force }
        catch {
            Stop-WithError @(
                "Could not unpack powershell-yaml $YamlVersion : $($_.Exception.Message)",
                "Delete $pkg and $root, then run this script again."
            )
        }
    }

    try { Import-Module -Name $psd1 -Force -ErrorAction Stop }
    catch {
        Stop-WithError @(
            "Could not import powershell-yaml $YamlVersion : $($_.Exception.Message)",
            "Delete $root and run this script again to unpack it afresh."
        )
    }
    Write-Ok "powershell-yaml $YamlVersion verified and loaded."
}

function Get-YamlKey {
    # Case-insensitive key lookup on a parsed YAML mapping. Also treats a boolean
    # $true key as "on", because some YAML parsers read the bare word "on" as a boolean.
    param($Map, [string]$Key)
    if ($null -eq $Map -or -not ($Map -is [System.Collections.IDictionary])) { return $null }
    foreach ($k in $Map.Keys) {
        if ($k -is [string] -and $k -ieq $Key) { return $Map[$k] }
        if ($Key -eq 'on' -and $k -is [bool] -and $k) { return $Map[$k] }
    }
    return $null
}

function ConvertTo-YamlScalarString {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [bool]) { return $Value.ToString().ToLowerInvariant() }
    return [string]$Value
}

function Test-YamlTruthy {
    param($Value)
    if ($null -eq $Value) { return $false }
    if ($Value -is [bool]) { return $Value }
    return ([string]$Value) -ieq 'true'
}

function Get-WorkflowDispatchInputs {
    # Returns @{ HasDispatch = bool; Inputs = list of input descriptors }
    param([string]$YamlText)
    try {
        $doc = ConvertFrom-Yaml -Yaml $YamlText -Ordered
    }
    catch {
        Stop-WithError @("The workflow file could not be parsed as YAML: $($_.Exception.Message)")
    }
    $on = Get-YamlKey $doc 'on'
    $hasDispatch = $false
    $dispatch = $null
    if ($on -is [string]) {
        $hasDispatch = ($on -eq 'workflow_dispatch')
    }
    elseif ($on -is [System.Collections.IDictionary]) {
        foreach ($k in $on.Keys) {
            if (([string]$k) -eq 'workflow_dispatch') { $hasDispatch = $true; $dispatch = $on[$k] }
        }
    }
    elseif ($on -is [System.Collections.IEnumerable]) {
        foreach ($item in $on) { if (([string]$item) -eq 'workflow_dispatch') { $hasDispatch = $true } }
    }

    $inputs = @()
    $inputMap = Get-YamlKey $dispatch 'inputs'
    if ($inputMap -is [System.Collections.IDictionary]) {
        foreach ($name in $inputMap.Keys) {
            $spec = $inputMap[$name]
            $type = ConvertTo-YamlScalarString (Get-YamlKey $spec 'type')
            if (-not $type) { $type = 'string' }
            $type = $type.ToLowerInvariant()
            $rawOptions = Get-YamlKey $spec 'options'
            $options = @()
            if ($rawOptions -is [System.Collections.IEnumerable] -and -not ($rawOptions -is [string])) {
                $options = @($rawOptions | ForEach-Object { ConvertTo-YamlScalarString $_ })
            }
            if ($type -eq 'boolean') { $options = @('true', 'false') }
            $inputs += [pscustomobject]@{
                Name        = [string]$name
                Description = ConvertTo-YamlScalarString (Get-YamlKey $spec 'description')
                Type        = $type
                Required    = Test-YamlTruthy (Get-YamlKey $spec 'required')
                Default     = ConvertTo-YamlScalarString (Get-YamlKey $spec 'default')
                Options     = $options
            }
        }
    }
    return [pscustomobject]@{ HasDispatch = $hasDispatch; Inputs = $inputs }
}

# ---------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------
function ConvertTo-SafeFileName {
    param([string]$Name)
    $s = $Name
    foreach ($c in [System.IO.Path]::GetInvalidFileNameChars()) { $s = $s.Replace($c, '_') }
    $s = ($s -replace '\s+', '_').Trim('_', '.')
    if (-not $s) { $s = 'workflow' }
    return $s
}

function ConvertTo-PsLiteral {
    param($Value)
    if ($null -eq $Value) { return '$null' }
    if ($Value -is [bool]) { if ($Value) { return '$true' } else { return '$false' } }
    if ($Value -is [string]) { return "'" + $Value.Replace("'", "''") + "'" }
    if ($Value -is [System.Collections.IEnumerable]) {
        $parts = @($Value | ForEach-Object { ConvertTo-PsLiteral $_ })
        return '@(' + ($parts -join ', ') + ')'
    }
    return "'" + ([string]$Value).Replace("'", "''") + "'"
}

function Get-DesktopPath {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if (-not $desktop -or -not (Test-Path -LiteralPath $desktop)) {
        Stop-WithError @('Could not determine the Desktop folder for the current user.')
    }
    return $desktop
}

# =====================================================================
# Runtime script template
# =====================================================================
$RuntimeTemplate = @'
# Generated by New-WorkflowShortcut.ps1 on __GENERATED_AT__
# Runs the workflow "__WORKFLOW_DISPLAY__" in __REPO_DISPLAY__ from branch "__BRANCH_DISPLAY__".
# Re-run the setup script to regenerate this file rather than editing it by hand.

$Owner        = __OWNER__
$Repo         = __REPO__
$WorkflowFile = __WORKFLOW_FILE__
$WorkflowName = __WORKFLOW_NAME__
$Branch       = __BRANCH__
$Parameters   = @(
__PARAMETERS__
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$RepoSlug = "$Owner/$Repo"

# ---------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------
$LogsDir = Join-Path $HOME '.github_shortcuts\logs'
if (-not (Test-Path -LiteralPath $LogsDir)) { New-Item -ItemType Directory -Path $LogsDir -Force | Out-Null }

function ConvertTo-SafeFileName {
    param([string]$Name)
    $s = $Name
    foreach ($c in [System.IO.Path]::GetInvalidFileNameChars()) { $s = $s.Replace($c, '_') }
    $s = ($s -replace '\s+', '_').Trim('_', '.')
    if (-not $s) { $s = 'workflow' }
    return $s
}

$Stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$LogFile = Join-Path $LogsDir ('{0}_{1}_{2}_{3}.txt' -f $Stamp, (ConvertTo-SafeFileName $Owner), (ConvertTo-SafeFileName $Repo), (ConvertTo-SafeFileName $WorkflowName))

function Write-Log {
    param([string]$Message, [string]$Color = 'Gray', [switch]$NoConsole)
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    if (-not $NoConsole) { Write-Host $Message -ForegroundColor $Color }
}

function Stop-WithError {
    param([string[]]$Lines)
    Write-Host ""
    Write-Log "ERROR: $($Lines[0])" 'Red'
    if ($Lines.Count -gt 1) {
        $Lines[1..($Lines.Count - 1)] | ForEach-Object { Write-Log "  $_" 'Yellow' }
    }
    Write-Host ""
    Write-Host "Log file: $LogFile" -ForegroundColor Gray
    exit 1
}

function Invoke-Gh {
    param([string[]]$Arguments)
    $output = & gh @Arguments 2>&1
    $code = $LASTEXITCODE
    $stdout = @($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { [string]$_ }) -join "`n"
    $stderr = @($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() }) -join "`n"
    [pscustomobject]@{ ExitCode = $code; StdOut = $stdout; StdErr = $stderr }
}

function Read-ParameterValue {
    param($Parameter)
    $name     = $Parameter.Name
    $required = [bool]$Parameter.Required
    $default  = $Parameter.Default
    $options  = @($Parameter.Options | Where-Object { $null -ne $_ })
    $reqText  = if ($required) { 'required' } else { 'optional' }

    Write-Host ""
    Write-Host "Input: $name ($($Parameter.Type), $reqText)" -ForegroundColor White
    if ($Parameter.Description) { Write-Host "  $($Parameter.Description)" -ForegroundColor Gray }

    if ($options.Count -gt 0) {
        $defaultIndex = -1
        if ($default) { $defaultIndex = [array]::IndexOf($options, $default) }
        for ($i = 0; $i -lt $options.Count; $i++) {
            $marker = if ($i -eq $defaultIndex) { ' (default)' } else { '' }
            Write-Host ("  {0,3}. {1}{2}" -f ($i + 1), $options[$i], $marker)
        }
        while ($true) {
            $hint = ''
            if ($defaultIndex -ge 0) { $hint = " [Enter for $($defaultIndex + 1)]" }
            elseif (-not $required) { $hint = ' [Enter to leave unset]' }
            $answer = Read-Host "Choose (1-$($options.Count))$hint"
            if ([string]::IsNullOrWhiteSpace($answer)) {
                if ($defaultIndex -ge 0) { return $options[$defaultIndex] }
                if (-not $required) { return $null }
                Write-Host "  A choice is required." -ForegroundColor Yellow
                continue
            }
            $number = 0
            if ([int]::TryParse($answer, [ref]$number) -and $number -ge 1 -and $number -le $options.Count) {
                return $options[$number - 1]
            }
            if ($options -ccontains $answer.Trim()) { return $answer.Trim() }
            Write-Host "  Enter a number between 1 and $($options.Count)." -ForegroundColor Yellow
        }
    }

    while ($true) {
        $hint = ''
        if ($default) { $hint = " [$default]" }
        elseif (-not $required) { $hint = ' [Enter to leave unset]' }
        $answer = Read-Host "Value$hint"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            if ($default) { return $default }
            if (-not $required) { return $null }
            Write-Host "  A value is required." -ForegroundColor Yellow
            continue
        }
        $answer = $answer.Trim()
        if ($Parameter.Type -eq 'number') {
            $parsed = 0.0
            if (-not [double]::TryParse($answer, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) {
                Write-Host "  This input is numeric. Enter a number." -ForegroundColor Yellow
                continue
            }
        }
        return $answer
    }
}

# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------
Write-Host ""
Write-Log "Workflow : $WorkflowName ($WorkflowFile)" 'Cyan'
Write-Log "Repo     : $RepoSlug" 'Cyan'
Write-Log "Branch   : $Branch" 'Cyan'
Write-Log "Log file : $LogFile" 'Gray'

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Stop-WithError @(
        'The GitHub CLI (gh) was not found on PATH.',
        'Install it with:  winget install --id GitHub.cli',
        'or download it from https://cli.github.com, then open a new window and try again.'
    )
}

$auth = Invoke-Gh @('auth', 'status')
if ($auth.ExitCode -ne 0) {
    Stop-WithError @(
        'The GitHub CLI is not authenticated.',
        'Run:  gh auth login',
        'and follow the prompts, then run this shortcut again.'
    )
}

$who = Invoke-Gh @('api', 'user', '--jq', '.login')
if ($who.ExitCode -ne 0 -or -not $who.StdOut.Trim()) {
    Stop-WithError @('Could not determine the authenticated GitHub user.', $who.StdErr)
}
$Login = $who.StdOut.Trim()
Write-Log "Authenticated as $Login" 'Gray'

$fieldArgs = @()
foreach ($p in $Parameters) {
    $value = $null
    if ($p.Mode -eq 'fixed') {
        $value = $p.Value
        if ($null -ne $value) { Write-Log "Input $($p.Name) = $value (fixed)" 'Gray' } else { Write-Log "Input $($p.Name) left unset (workflow default applies)" 'Gray' }
    }
    else {
        $value = Read-ParameterValue -Parameter $p
        if ($null -ne $value) { Write-Log "Input $($p.Name) = $value" 'Gray' -NoConsole } else { Write-Log "Input $($p.Name) left unset (workflow default applies)" 'Gray' -NoConsole }
    }
    if ($null -ne $value) {
        $fieldArgs += '-f'
        $fieldArgs += ('{0}={1}' -f $p.Name, $value)
    }
}

Write-Host ""
Write-Log "Dispatching workflow..." 'Cyan'
$since = [DateTime]::UtcNow.AddMinutes(-1)
$dispatch = Invoke-Gh (@('workflow', 'run', $WorkflowFile, '--repo', $RepoSlug, '--ref', $Branch) + $fieldArgs)
if ($dispatch.ExitCode -ne 0) {
    Stop-WithError @('gh workflow run failed.', $dispatch.StdErr, $dispatch.StdOut)
}
Write-Log "Workflow dispatched. Locating the run..." 'Gray'

$run = $null
for ($attempt = 0; $attempt -lt 30 -and $null -eq $run; $attempt++) {
    Start-Sleep -Seconds 2
    $list = Invoke-Gh @('run', 'list', '--repo', $RepoSlug, '--workflow', $WorkflowFile, '--branch', $Branch, '--user', $Login, '--event', 'workflow_dispatch', '--limit', '10', '--json', 'databaseId,url,createdAt')
    if ($list.ExitCode -eq 0 -and $list.StdOut.Trim()) {
        # Windows PowerShell 5.1 emits a JSON array as a single object, so piping
        # straight into @() nests it. Assign first, then wrap.
        $parsed = $list.StdOut | ConvertFrom-Json
        $runs = @($parsed)
        $candidates = @($runs | Where-Object { ([DateTime]$_.createdAt).ToUniversalTime() -ge $since } | Sort-Object { [DateTime]$_.createdAt } -Descending)
        if ($candidates.Count -gt 0) { $run = $candidates[0] }
    }
}
if ($null -eq $run) {
    Stop-WithError @(
        'The workflow was dispatched but the run could not be located within 60 seconds.',
        "Check it manually at https://github.com/$RepoSlug/actions"
    )
}

$RunId = $run.databaseId
Write-Log "Run URL  : $($run.url)" 'Cyan'
Write-Host ""
Write-Log "Waiting for run $RunId to complete..." 'Gray'
& gh run watch $RunId --repo $RepoSlug
Write-Host ""

$view = Invoke-Gh @('run', 'view', "$RunId", '--repo', $RepoSlug, '--json', 'status,conclusion,url,displayTitle,startedAt,updatedAt')
if ($view.ExitCode -ne 0) {
    Stop-WithError @('Could not read the final run status.', $view.StdErr, "Run URL: $($run.url)")
}
$info = $view.StdOut | ConvertFrom-Json
$conclusion = if ($info.conclusion) { $info.conclusion } else { $info.status }
$color = switch ($conclusion) {
    'success'   { 'Green' }
    'failure'   { 'Red' }
    'cancelled' { 'Yellow' }
    default     { 'Yellow' }
}
Write-Log ("Run {0} finished with conclusion: {1}" -f $RunId, $conclusion.ToUpperInvariant()) $color
Write-Log "Run URL  : $($info.url)" 'Cyan'
Write-Host ""
Write-Host "Log file: $LogFile" -ForegroundColor Gray

if ($conclusion -eq 'success') { exit 0 } else { exit 1 }
'@

$CmdTemplate = @'
@echo off
setlocal
title __TITLE__
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0__PS1_NAME__"
echo.
pause
'@

# =====================================================================
# Main setup flow
# =====================================================================
Write-Host ""
Write-Host "GitHub Workflow Shortcut Setup" -ForegroundColor White
Write-Host "==============================" -ForegroundColor White

if (-not $GitHubOwner -or $GitHubOwner -eq 'your-user-or-org') {
    Stop-WithError @(
        'The GitHub owner is not configured.',
        "Open this script and set `$GitHubOwner to your GitHub username or organization name."
    )
}

if (-not ($env:OS -eq 'Windows_NT')) {
    Stop-WithError @('This script creates .cmd and .lnk files and only runs on Windows.')
}

# --- gh present and authenticated ---
Write-Step 'Checking the GitHub CLI'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Stop-WithError @(
        'The GitHub CLI (gh) was not found on PATH.',
        'Install it with:  winget install --id GitHub.cli',
        'or download it from https://cli.github.com',
        'Open a new PowerShell window after installing so PATH is refreshed, then run this script again.'
    )
}
Write-Ok "gh found at $((Get-Command gh).Source)"

$auth = Invoke-Gh @('auth', 'status')
if ($auth.ExitCode -ne 0) {
    Stop-WithError @(
        'The GitHub CLI is not authenticated.',
        'Run:  gh auth login',
        'Choose GitHub.com and follow the prompts. The token needs the "repo" scope',
        '(the default for gh auth login), then run this script again.'
    )
}
$who = Invoke-Gh @('api', 'user', '--jq', '.login')
if ($who.ExitCode -ne 0 -or -not $who.StdOut.Trim()) {
    Stop-WithError @('Authenticated, but the current user could not be read from the API.', $who.StdErr, 'Try:  gh auth refresh')
}
$Login = $who.StdOut.Trim()
Write-Ok "Authenticated as $Login"

# --- YAML module ---
Write-Step 'Checking the YAML parser'
Initialize-YamlModule
Write-Ok 'powershell-yaml is available'

# --- owner ---
Write-Step "Verifying owner '$GitHubOwner'"
$ownerResult = Invoke-GhApi "users/$GitHubOwner"
if (-not $ownerResult.Ok) {
    if ($ownerResult.NotFound) {
        Stop-WithError @(
            "No GitHub user or organization named '$GitHubOwner' was found.",
            "Check the `$GitHubOwner value at the top of this script."
        )
    }
    Stop-WithError @("Could not look up '$GitHubOwner'.", $ownerResult.Error)
}
$ownerInfo = $ownerResult.Data | ConvertFrom-Json
$GitHubOwner = $ownerInfo.login
Write-Ok "$($ownerInfo.type): $GitHubOwner"

# --- repository ---
Write-Step 'Repository'
$Repo = Read-Text -Prompt "Repository name (without the owner)" -Required
$Repo = $Repo -replace '^.*/', ''
$RepoSlug = "$GitHubOwner/$Repo"
$repoResult = Invoke-GhApi "repos/$RepoSlug"
if (-not $repoResult.Ok) {
    if ($repoResult.NotFound) {
        Stop-WithError @(
            "Repository '$RepoSlug' was not found, or your token cannot see it.",
            'Check the spelling. For private repositories make sure your gh login has access;',
            'for organizations with SSO, run:  gh auth refresh  and authorize the organization.'
        )
    }
    Stop-WithError @("Could not read repository '$RepoSlug'.", $repoResult.Error)
}
$repoInfo = $repoResult.Data | ConvertFrom-Json
$Repo = $repoInfo.name
$RepoSlug = $repoInfo.full_name
if (-not $repoInfo.permissions.push) {
    Stop-WithError @(
        "You can see '$RepoSlug' but do not have write access to it.",
        'Triggering a workflow requires write (push) permission on the repository.',
        'Ask a repository admin for write access, or authenticate gh as a user that has it.'
    )
}
Write-Ok "$RepoSlug (default branch: $($repoInfo.default_branch), write access confirmed)"

# --- branch ---
Write-Step 'Branch'
$Branch = Read-Text -Prompt 'Branch to run the workflow from' -Default $repoInfo.default_branch -Required
$branchResult = Invoke-GhApi "repos/$RepoSlug/git/ref/heads/$Branch"
if (-not $branchResult.Ok) {
    if ($branchResult.NotFound) {
        Stop-WithError @(
            "Branch '$Branch' does not exist in $RepoSlug.",
            "List branches with:  gh api repos/$RepoSlug/branches --jq '.[].name'"
        )
    }
    Stop-WithError @("Could not verify branch '$Branch'.", $branchResult.Error)
}
Write-Ok "Branch '$Branch' exists"

# --- workflow ---
Write-Step 'Workflow'
$wfListResult = Invoke-GhApi "repos/$RepoSlug/actions/workflows?per_page=100"
if (-not $wfListResult.Ok) {
    Stop-WithError @("Could not list workflows for $RepoSlug.", $wfListResult.Error)
}
$workflows = @(($wfListResult.Data | ConvertFrom-Json).workflows)
if ($workflows.Count -eq 0) {
    Stop-WithError @(
        "No workflows were found in $RepoSlug.",
        'Add a workflow file under .github/workflows on the default branch first.'
    )
}
$wfLabels = @($workflows | ForEach-Object { "{0}  ({1})  [{2}]" -f $_.name, $_.path, $_.state })
$wfIndex = Select-FromList -Prompt 'Choose the workflow' -Items $wfLabels
$workflow = $workflows[$wfIndex]
$WorkflowName = $workflow.name
$WorkflowPath = $workflow.path
$WorkflowFile = [System.IO.Path]::GetFileName($WorkflowPath)

if ($workflow.state -ne 'active') {
    Stop-WithError @(
        "Workflow '$WorkflowName' is not active (state: $($workflow.state)).",
        'Enable it with:',
        "  gh workflow enable `"$WorkflowFile`" --repo $RepoSlug",
        'or from the Actions tab on GitHub, then run this script again.'
    )
}
Write-Ok "Workflow '$WorkflowName' ($WorkflowPath) is active"

$escapedBranch = [Uri]::EscapeDataString($Branch)
$fileResult = Invoke-GhApi "repos/$RepoSlug/contents/$WorkflowPath`?ref=$escapedBranch" -ExtraArguments @('-H', 'Accept: application/vnd.github.raw')
if (-not $fileResult.Ok) {
    if ($fileResult.NotFound) {
        Stop-WithError @(
            "The workflow file '$WorkflowPath' does not exist on branch '$Branch'.",
            'workflow_dispatch runs use the workflow file from the branch you dispatch on.',
            "Either choose a branch that contains the file, or merge it into '$Branch'."
        )
    }
    Stop-WithError @("Could not read '$WorkflowPath' from branch '$Branch'.", $fileResult.Error)
}
Write-Ok "Workflow file found on branch '$Branch'"

$parsed = Get-WorkflowDispatchInputs -YamlText $fileResult.Data
if (-not $parsed.HasDispatch) {
    Stop-WithError @(
        "Workflow '$WorkflowName' on branch '$Branch' has no workflow_dispatch trigger.",
        'Add this to the workflow file and commit it to that branch:',
        '  on:',
        '    workflow_dispatch:',
        'Optionally declare inputs under workflow_dispatch to have them prompted here.'
    )
}
$inputs = @($parsed.Inputs)
Write-Ok ("workflow_dispatch trigger present with {0} input(s)" -f $inputs.Count)

# --- environments (only needed if an input has type environment) ---
$environmentNames = $null
if ($inputs | Where-Object { $_.Type -eq 'environment' }) {
    $envResult = Invoke-GhApi "repos/$RepoSlug/environments?per_page=100"
    if ($envResult.Ok) {
        $environmentNames = @((($envResult.Data | ConvertFrom-Json).environments) | ForEach-Object { $_.name })
    }
    else {
        Write-Info "Could not list environments (they will be entered as free text): $($envResult.Error)"
    }
}

# --- parameters ---
$ParameterConfigs = @()
if ($inputs.Count -gt 0) {
    Write-Step 'Workflow inputs'
    Write-Info 'For each input choose either a fixed value or a prompt at run time.'
}
foreach ($in in $inputs) {
    $options = @($in.Options)
    if ($in.Type -eq 'environment' -and $environmentNames -and $environmentNames.Count -gt 0) {
        $options = @($environmentNames)
    }
    $reqText = if ($in.Required) { 'required' } else { 'optional' }

    Write-Host ""
    Write-Host "Input: $($in.Name) ($($in.Type), $reqText)" -ForegroundColor White
    if ($in.Description) { Write-Host "    $($in.Description)" -ForegroundColor Gray }
    if ($in.Default) { Write-Host "    Workflow default: $($in.Default)" -ForegroundColor Gray }

    $mode = Select-FromList -Prompt 'How should this input be supplied' -Items @(
        'Use a fixed value on every run',
        'Prompt for a value each time the shortcut runs'
    ) -DefaultIndex 1

    $fixedValue = $null
    $promptDefault = $null
    if ($mode -eq 0) {
        Write-Host "    Enter the fixed value:" -ForegroundColor Gray
        $fixedValue = Read-ParameterValue -Name $in.Name -Type $in.Type -Options $options -Default $in.Default -Required $in.Required -Prompt '    Value'
        if ($null -eq $fixedValue) { Write-Info 'Left unset; the workflow default will apply on every run.' }
    }
    else {
        $offerDefault = Read-YesNo '    Offer a default value at the run time prompt?' -Default ([bool]$in.Default)
        if ($offerDefault) {
            Write-Host "    Enter the default value:" -ForegroundColor Gray
            $promptDefault = Read-ParameterValue -Name $in.Name -Type $in.Type -Options $options -Default $in.Default -Required $true -Prompt '    Default'
        }
    }

    $ParameterConfigs += [pscustomobject]@{
        Name        = $in.Name
        Type        = $in.Type
        Description = $in.Description
        Required    = [bool]$in.Required
        Mode        = if ($mode -eq 0) { 'fixed' } else { 'prompt' }
        Value       = $fixedValue
        Default     = $promptDefault
        Options     = $options
    }
}

# --- shortcut name ---
Write-Step 'Shortcut'
$defaultShortcutName = "$Repo - $WorkflowName"
$ShortcutName = Read-Text -Prompt 'Shortcut name' -Default $defaultShortcutName -Required
$BaseName = ConvertTo-SafeFileName $ShortcutName
$DesktopPath = Get-DesktopPath
$CmdPath = Join-Path $ActionsDir "$BaseName.cmd"
$Ps1Path = Join-Path $ActionsDir "$BaseName.ps1"
$LnkPath = Join-Path $DesktopPath "$ShortcutName.lnk"

$existing = @($CmdPath, $Ps1Path, $LnkPath | Where-Object { Test-Path -LiteralPath $_ })
if ($existing.Count -gt 0) {
    Write-Host "    These files already exist:" -ForegroundColor Yellow
    $existing | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
    if (-not (Read-YesNo '    Overwrite them?' -Default $false)) {
        Stop-WithError @('Aborted. Run the script again and choose a different shortcut name.')
    }
}

# --- summary ---
Write-Step 'Summary'
Write-Info "Owner     : $GitHubOwner"
Write-Info "Repository: $RepoSlug"
Write-Info "Branch    : $Branch"
Write-Info "Workflow  : $WorkflowName ($WorkflowFile)"
foreach ($p in $ParameterConfigs) {
    if ($p.Mode -eq 'fixed') {
        $v = if ($null -eq $p.Value) { '(unset, workflow default)' } else { $p.Value }
        Write-Info "Input $($p.Name): fixed = $v"
    }
    else {
        $d = if ($null -eq $p.Default) { 'no default' } else { "default = $($p.Default)" }
        Write-Info "Input $($p.Name): prompt at run time ($d)"
    }
}
Write-Info "Runner    : $CmdPath"
Write-Info "Shortcut  : $LnkPath"
if (-not (Read-YesNo 'Create the shortcut?')) {
    Stop-WithError @('Aborted by user. Nothing was written.')
}

# --- write files ---
Write-Step 'Writing files'
foreach ($dir in @($ActionsDir, $LogsDir)) {
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
}

$paramLines = @()
foreach ($p in $ParameterConfigs) {
    $paramLines += ('    @{{ Name = {0}; Type = {1}; Description = {2}; Required = {3}; Mode = {4}; Value = {5}; Default = {6}; Options = {7} }}' -f `
        (ConvertTo-PsLiteral $p.Name),
        (ConvertTo-PsLiteral $p.Type),
        (ConvertTo-PsLiteral $p.Description),
        (ConvertTo-PsLiteral $p.Required),
        (ConvertTo-PsLiteral $p.Mode),
        (ConvertTo-PsLiteral $p.Value),
        (ConvertTo-PsLiteral $p.Default),
        (ConvertTo-PsLiteral @($p.Options)))
}

$runtime = $RuntimeTemplate
$runtime = $runtime.Replace('__GENERATED_AT__', (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
$runtime = $runtime.Replace('__WORKFLOW_DISPLAY__', $WorkflowName)
$runtime = $runtime.Replace('__REPO_DISPLAY__', $RepoSlug)
$runtime = $runtime.Replace('__BRANCH_DISPLAY__', $Branch)
$runtime = $runtime.Replace('__OWNER__', (ConvertTo-PsLiteral $GitHubOwner))
$runtime = $runtime.Replace('__REPO__', (ConvertTo-PsLiteral $Repo))
$runtime = $runtime.Replace('__WORKFLOW_FILE__', (ConvertTo-PsLiteral $WorkflowFile))
$runtime = $runtime.Replace('__WORKFLOW_NAME__', (ConvertTo-PsLiteral $WorkflowName))
$runtime = $runtime.Replace('__BRANCH__', (ConvertTo-PsLiteral $Branch))
$runtime = $runtime.Replace('__PARAMETERS__', ($paramLines -join "`n"))

$cmd = $CmdTemplate
$cmd = $cmd.Replace('__TITLE__', ($ShortcutName -replace '[&|<>^%]', '_'))
$cmd = $cmd.Replace('__PS1_NAME__', "$BaseName.ps1")

try {
    $utf8Bom = New-Object System.Text.UTF8Encoding $true
    [System.IO.File]::WriteAllText($Ps1Path, $runtime, $utf8Bom)
    $script:CreatedFiles += $Ps1Path
    Write-Ok "Wrote $Ps1Path"

    [System.IO.File]::WriteAllText($CmdPath, ($cmd -replace "`r?`n", "`r`n"), [System.Text.Encoding]::ASCII)
    $script:CreatedFiles += $CmdPath
    Write-Ok "Wrote $CmdPath"
}
catch {
    Stop-WithError @("Failed to write runner files: $($_.Exception.Message)")
}

try {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LnkPath)
    $lnk.TargetPath = $CmdPath
    $lnk.WorkingDirectory = $ActionsDir
    $lnk.Description = "Run workflow '$WorkflowName' in $RepoSlug"
    $lnk.Save()
    $script:CreatedFiles += $LnkPath
    Write-Ok "Created $LnkPath"
}
catch {
    Stop-WithError @("Failed to create the desktop shortcut: $($_.Exception.Message)")
}

Write-Host ""
Write-Host "Done. Double-click '$ShortcutName' on your desktop to run the workflow." -ForegroundColor Green
Write-Host "Run logs will be written to $LogsDir" -ForegroundColor Gray
Write-Host ""
