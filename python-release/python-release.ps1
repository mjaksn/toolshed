#Requires -Version 7.4
<#
.SYNOPSIS
    Bump a Python project's version and open the release pull request, or tag
    the merged result so the project's release workflow publishes it.

.DESCRIPTION
    Written for projects shaped like nettail, netflume, lanname and
    readerboard: a static version in pyproject.toml, the same version in the
    package's __init__.py as __version__, a CHANGELOG.md with a
    "## [X.Y.Z] - YYYY-MM-DD" section per release, and a release workflow that
    fires on a pushed "vX.Y.Z" tag.

    It asks which of two things to do.

    Open a version bump pull request. The checkout must have no staged or
    unstaged changes to tracked files, must be on main, and main must be at the
    same commit as origin's. The version must agree everywhere it is recorded.
    It then asks for the new version, which must be higher, writes it into
    every place, makes sure CHANGELOG.md has a section for it, commits the lot
    to a new release-X.Y.Z branch as "Release X.Y.Z", and asks before pushing
    the branch and again before opening the pull request, whose body is the
    changelog section.

    Tag and push to trigger the release. The checkout must have no staged or
    unstaged changes to tracked files, the version must agree everywhere and be
    of the X.Y.Z form, CHANGELOG.md must have a section for it, no tag vX.Y.Z
    may exist locally or on origin, the checkout must be on main, and main must
    be at the same commit as origin's. It then asks before creating the
    annotated tag and pushing it to origin.

    Any check that fails prints what was wrong and exits 1 before anything is
    changed. A push or a pull request that fails after the commit also exits
    1, and its message says what is already done. Declining a confirmation
    stops there and exits 0, saying what was and was not done.

    The places a version is recorded:

      pyproject.toml          version = "X.Y.Z"                   required
      <package>/__init__.py   __version__ = "X.Y.Z"               required
                              (or src/<package>/__init__.py, never both)
      README.md               <package>.__version__  # "X.Y.Z"    if present
      docs/openapi.json       "version": "X.Y.Z"                  if the file exists

    The package directory is the project name from pyproject.toml, lower cased,
    with hyphens and dots turned into underscores.

.PARAMETER RepositoryPath
    The local checkout of the Python project. A directory inside it also
    works; the root of the repository is used either way.

.EXAMPLE
    ./python-release.ps1 ~/repos/netflume
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$RepositoryPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# Every native command's exit code is checked by hand below, so a profile that
# turns this on must not turn a refused rev-parse into a terminating error.
$PSNativeCommandUseErrorActionPreference = $false

$script:Remote = 'origin'
$script:MainBranch = 'main'

# ---------------------------------------------------------------------------
# Wrappers around the outside world, kept small so the tests can replace them
# ---------------------------------------------------------------------------

function Find-Gh {
    Get-Command -Name gh -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Invoke-Gh {
    param([string]$WorkingDirectory, [string[]]$Arguments)
    if ($WorkingDirectory) { Push-Location -LiteralPath $WorkingDirectory }
    try {
        $output = @(& gh @Arguments 2>&1 | ForEach-Object { "$_" })
        [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
    }
    finally {
        if ($WorkingDirectory) { Pop-Location }
    }
}

function Invoke-Git {
    param([string]$Repository, [string[]]$Arguments, [switch]$AllowFailure)
    $stdout = [Collections.Generic.List[string]]::new()
    $stderr = [Collections.Generic.List[string]]::new()
    & git -C $Repository @Arguments 2>&1 | ForEach-Object {
        if ($_ -is [System.Management.Automation.ErrorRecord]) { $stderr.Add("$_") } else { $stdout.Add("$_") }
    }
    $code = $LASTEXITCODE
    if ($code -ne 0 -and -not $AllowFailure) {
        throw "git $($Arguments -join ' ') failed with exit code $code. $($stderr -join ' ')"
    }
    [pscustomobject]@{ ExitCode = $code; Output = $stdout.ToArray(); Errors = $stderr.ToArray() }
}

function Get-Today {
    Get-Date -Format 'yyyy-MM-dd'
}

function Read-TextFile {
    param([string]$Path)
    [IO.File]::ReadAllText($Path)
}

# Writes back in the encoding the file already had, byte order mark included:
# the reader detects a UTF-8, UTF-16 or UTF-32 mark and settles on UTF-8 with
# none when there is no mark. Line endings are never touched here, because
# every edit is a replacement inside the text as it was read.
function Write-TextFile {
    param([string]$Path, [string]$Text)
    $reader = [IO.StreamReader]::new($Path, [Text.UTF8Encoding]::new($false), $true)
    try {
        $null = $reader.ReadToEnd()
        $encoding = $reader.CurrentEncoding
    }
    finally {
        $reader.Dispose()
    }
    [IO.File]::WriteAllText($Path, $Text, $encoding)
}

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

function Read-Goal {
    Write-Host ''
    Write-Host 'What do you want to do?'
    Write-Host '  1  Open a pull request that bumps the version'
    Write-Host '  2  Tag main with the current version and push the tag, which triggers the release'
    $answer = "$(Read-Host 'Choose 1 or 2')".Trim()
    switch ($answer) {
        '1' { return 'bump' }
        '2' { return 'tag' }
        default { throw "'$answer' is not one of the choices. Run it again and answer 1 or 2." }
    }
}

function Confirm-Step {
    param([string]$Question)
    $answer = "$(Read-Host "$Question [y/N]")".Trim()
    return $answer -match '^(y|yes)$'
}

function Read-NewVersion {
    param([string]$CurrentVersion)
    $answer = "$(Read-Host "New version (the current one is $CurrentVersion)")".Trim()
    Assert-VersionHigher -CurrentVersion $CurrentVersion -NewVersion $answer
    return $answer
}

function Read-ChangelogEntry {
    param([string]$Version)
    Write-Host ''
    Write-Host "CHANGELOG.md has no section for $Version. Type the entry as Markdown, as it should"
    Write-Host 'appear under the heading, which is added for you. Finish with a line holding only a full stop.'
    $lines = [Collections.Generic.List[string]]::new()
    while ($true) {
        $line = Read-Host
        if ($null -eq $line -or $line -eq '.') { break }
        $lines.Add($line)
    }
    while ($lines.Count -gt 0 -and -not $lines[0].Trim()) { $lines.RemoveAt(0) }
    while ($lines.Count -gt 0 -and -not $lines[$lines.Count - 1].Trim()) { $lines.RemoveAt($lines.Count - 1) }
    if ($lines.Count -eq 0) {
        throw "No changelog entry was given for $Version, and a release needs one: its section becomes the pull request body and the release notes."
    }
    return , $lines.ToArray()
}

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

function Assert-GhReady {
    if (-not (Find-Gh)) {
        throw 'The GitHub CLI, gh, was not found on PATH. Install it from https://cli.github.com/ and sign in with "gh auth login", then run this again.'
    }
    $status = Invoke-Gh -Arguments @('auth', 'status')
    if ($status.ExitCode -ne 0) {
        throw "gh is installed but is not signed in to GitHub. Run ""gh auth login"", then run this again. gh said: $($status.Output -join ' ')"
    }
}

function Resolve-Repository {
    param([string]$Path)
    if (-not (Get-Command -Name git -CommandType Application -ErrorAction SilentlyContinue)) {
        throw 'git was not found on PATH.'
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "The repository path '$Path' does not exist or is not a directory."
    }
    $full = (Resolve-Path -LiteralPath $Path).ProviderPath
    $top = Invoke-Git -Repository $full -Arguments @('rev-parse', '--show-toplevel') -AllowFailure
    if ($top.ExitCode -ne 0 -or $top.Output.Count -eq 0) {
        throw "'$full' is not inside a git repository."
    }
    $root = [IO.Path]::GetFullPath($top.Output[0])
    if (-not (Test-Path -LiteralPath (Join-Path $root 'pyproject.toml') -PathType Leaf)) {
        throw "There is no pyproject.toml at the root of $root, so it does not look like a Python project."
    }
    return $root
}

function Assert-CleanCheckout {
    param([string]$Repository)
    $status = Invoke-Git -Repository $Repository -Arguments @('status', '--porcelain', '--untracked-files=no')
    if ($status.Output.Count -gt 0) {
        throw "The checkout has staged or unstaged changes to tracked files. Commit or stash them first. Changed: $(($status.Output | ForEach-Object { $_.Substring(3) }) -join ', ')"
    }
}

function Assert-OnMain {
    param([string]$Repository)
    $branch = "$((Invoke-Git -Repository $Repository -Arguments @('branch', '--show-current')).Output)"
    if ($branch -ne $script:MainBranch) {
        $where = if ($branch) { "on the branch '$branch'" } else { 'not on any branch' }
        throw "The checkout is $where. Switch to $($script:MainBranch) first."
    }
}

function Assert-MainMatchesRemote {
    param([string]$Repository)
    $main = $script:MainBranch
    $remote = $script:Remote
    $localSha = (Invoke-Git -Repository $Repository -Arguments @('rev-parse', "refs/heads/$main")).Output[0]
    $listed = Invoke-Git -Repository $Repository -Arguments @('ls-remote', '--heads', $remote, "refs/heads/$main")
    if ($listed.Output.Count -eq 0) {
        throw "$remote has no $main branch."
    }
    $remoteSha = ($listed.Output[0] -split '\s+')[0]
    if ($localSha -ne $remoteSha) {
        throw "Local $main is at $($localSha.Substring(0, 12)) but $remote's $main is at $($remoteSha.Substring(0, 12)). Pull or push until they are the same commit, then run this again."
    }
}

function Assert-TagAbsent {
    param([string]$Repository, [string]$Tag)
    $local = Invoke-Git -Repository $Repository -Arguments @('rev-parse', '--quiet', '--verify', "refs/tags/$Tag") -AllowFailure
    if ($local.ExitCode -eq 0) {
        throw "The tag $Tag already exists locally."
    }
    $remote = Invoke-Git -Repository $Repository -Arguments @('ls-remote', '--tags', $script:Remote, "refs/tags/$Tag")
    if ($remote.Output.Count -gt 0) {
        throw "The tag $Tag already exists on $($script:Remote)."
    }
}

function Assert-BranchAbsent {
    param([string]$Repository, [string]$Branch)
    $local = Invoke-Git -Repository $Repository -Arguments @('rev-parse', '--quiet', '--verify', "refs/heads/$Branch") -AllowFailure
    if ($local.ExitCode -eq 0) {
        throw "The branch $Branch already exists locally. Delete it or finish that release first."
    }
    $remote = Invoke-Git -Repository $Repository -Arguments @('ls-remote', '--heads', $script:Remote, "refs/heads/$Branch")
    if ($remote.Output.Count -gt 0) {
        throw "The branch $Branch already exists on $($script:Remote). Delete it or finish that release first."
    }
}

function Assert-VersionHigher {
    param([string]$CurrentVersion, [string]$NewVersion)
    $form = '^\d+\.\d+\.\d+$'
    if ($NewVersion -notmatch $form) {
        throw "'$NewVersion' is not a version of the MAJOR.MINOR.PATCH form these projects use, such as 1.4.0, with no leading v."
    }
    if ($CurrentVersion -notmatch $form) {
        throw "The current version '$CurrentVersion' is not of the MAJOR.MINOR.PATCH form, so there is nothing to compare $NewVersion against."
    }
    if ([version]$NewVersion -le [version]$CurrentVersion) {
        throw "$NewVersion is not higher than the current version, $CurrentVersion."
    }
}

# ---------------------------------------------------------------------------
# Where the version lives
# ---------------------------------------------------------------------------

# Each pattern captures three groups: what comes before the version, the
# version, and what comes after, so a replacement puts back the first and last
# exactly as they were.
function Find-VersionLocation {
    param(
        [string]$Repository,
        [string]$File,
        [string]$Pattern,
        [string]$Description,
        [switch]$Required,
        [switch]$RequireLine,
        [switch]$Single
    )
    $path = Join-Path $Repository $File
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        if ($Required) { throw "$File does not exist." }
        return
    }
    $found = [regex]::Matches((Read-TextFile $path), $Pattern)
    if ($found.Count -eq 0) {
        if ($Required -or $RequireLine) { throw "$File has no $Description line." }
        return
    }
    if ($Single -and $found.Count -gt 1) {
        throw "$File has $($found.Count) $Description lines, so which one is the version is ambiguous."
    }
    [pscustomobject]@{
        File     = $File
        Path     = $path
        Pattern  = $Pattern
        Versions = @($found | ForEach-Object { $_.Groups[2].Value })
    }
}

function Get-VersionLocation {
    param([string]$Repository)
    $pyproject = Read-TextFile (Join-Path $Repository 'pyproject.toml')
    $name = [regex]::Match($pyproject, '(?m)^name\s*=\s*"([^"]+)"')
    if (-not $name.Success) {
        throw 'pyproject.toml has no name = "..." line, so the package directory cannot be worked out.'
    }
    $package = $name.Groups[1].Value.ToLowerInvariant() -replace '[-.]', '_'

    $inits = @(@("$package/__init__.py", "src/$package/__init__.py") |
            Where-Object { Test-Path -LiteralPath (Join-Path $Repository $_) -PathType Leaf })
    if ($inits.Count -eq 0) {
        throw "Neither $package/__init__.py nor src/$package/__init__.py exists, and the version is expected in one of them as __version__."
    }
    if ($inits.Count -gt 1) {
        throw "Both $package/__init__.py and src/$package/__init__.py exist, so which one is the package is ambiguous."
    }
    $init = $inits[0]

    Find-VersionLocation -Repository $Repository -File 'pyproject.toml' -Required -Single `
        -Pattern '(?m)^(version\s*=\s*")([^"]*)(")' -Description 'version = "..."'
    Find-VersionLocation -Repository $Repository -File $init -Required -Single `
        -Pattern '(?m)^(__version__\s*=\s*")([^"]*)(")' -Description '__version__ = "..."'
    Find-VersionLocation -Repository $Repository -File 'README.md' `
        -Pattern ('(?m)^(\s*' + [regex]::Escape($package) + '\.__version__\s*#\s*")([^"]*)(")') `
        -Description "$package.__version__"
    # An OpenAPI description always carries info.version, so a file with no line
    # this pattern finds holds it in a shape this cannot edit, and skipping it
    # would leave it behind.
    Find-VersionLocation -Repository $Repository -File 'docs/openapi.json' -RequireLine -Single `
        -Pattern '(?m)^(\s*"version":\s*")([^"]*)(")' -Description '"version": "..."'
}

function Get-ConsistentVersion {
    param([object[]]$Location)
    $distinct = @($Location | ForEach-Object { $_.Versions } | Sort-Object -Unique)
    if ($distinct.Count -ne 1) {
        $detail = ($Location | ForEach-Object { "$($_.File) says $($_.Versions -join ' and ')" }) -join '; '
        throw "The version is not the same everywhere it is recorded: $detail."
    }
    return $distinct[0]
}

function Write-VersionLocation {
    param([object[]]$Location, [string]$NewVersion)
    foreach ($place in $Location) {
        $text = Read-TextFile $place.Path
        $updated = $text -creplace $place.Pattern, ('${1}' + $NewVersion + '${3}')
        Write-TextFile -Path $place.Path -Text $updated
    }
}

# ---------------------------------------------------------------------------
# The changelog, which is assumed to be well formatted
# ---------------------------------------------------------------------------

function Get-ChangelogHeadingCount {
    param([string]$Text, [string]$Version)
    [regex]::Matches($Text, '(?m)^## \[' + [regex]::Escape($Version) + '\]').Count
}

# Nearly the pattern the release workflows use to lift the release notes out,
# and the heading is left out, as it is there. The difference is the end: the
# workflows stop at a link reference that starts with a digit, and this stops
# at any link reference, so an [Unreleased]: line at the foot of the file is
# never taken for part of the last section.
function Get-ChangelogSection {
    param([string]$Text, [string]$Version)
    $count = Get-ChangelogHeadingCount -Text $Text -Version $Version
    if ($count -eq 0) {
        throw "CHANGELOG.md has no '## [$Version]' section."
    }
    if ($count -gt 1) {
        throw "CHANGELOG.md has $count '## [$Version]' headings, so which section belongs to the release is ambiguous."
    }
    $found = [regex]::Match($Text, '(?ms)^## \[' + [regex]::Escape($Version) + '\][^\n]*\n(.*?)(?=^## \[|^\[[^\]\r\n]+\]:)')
    if (-not $found.Success) {
        throw "The '## [$Version]' section of CHANGELOG.md has nothing after it, neither another '## [' heading nor a link reference, so where it ends is ambiguous."
    }
    $body = $found.Groups[1].Value.Trim()
    if (-not $body) {
        throw "The '## [$Version]' section of CHANGELOG.md is empty."
    }
    return $body
}

# Adds the reference link a "## [X.Y.Z]" heading needs, copying the shape of
# the newest link already there: a compare link from the previous version, or
# a link to the release page. A changelog with no version links gets none.
function Add-ChangelogLink {
    param([string]$Text, [string]$CurrentVersion, [string]$NewVersion)
    if ([regex]::IsMatch($Text, '(?m)^\[' + [regex]::Escape($NewVersion) + '\]:')) { return $Text }
    $newest = [regex]::Match($Text, '(?m)^\[\d+\.\d+\.\d+\]:[ \t]*([^\r\n]+)')
    if (-not $newest.Success) { return $Text }

    $url = $newest.Groups[1].Value.Trim()
    $compare = [regex]::Match($url, '^(?<base>.+/compare/)(?<v>v?)[^/]+\.\.\.[^/]+$')
    $release = [regex]::Match($url, '^(?<base>.+/releases/tag/)(?<v>v?)[^/]+$')
    if ($compare.Success) {
        $v = $compare.Groups['v'].Value
        $link = "$($compare.Groups['base'].Value)$v$CurrentVersion...$v$NewVersion"
    }
    elseif ($release.Success) {
        $link = "$($release.Groups['base'].Value)$($release.Groups['v'].Value)$NewVersion"
    }
    else {
        return $Text
    }
    $line = "[$NewVersion]: $link"

    $unreleased = [regex]::Match($Text, '(?m)^\[Unreleased\]:[^\r\n]*')
    if ($unreleased.Success) {
        return $Text.Remove($unreleased.Index, $unreleased.Length).Insert($unreleased.Index, $line)
    }
    $newline = if ($Text.Contains("`r`n")) { "`r`n" } else { "`n" }
    return $Text.Insert($newest.Index, $line + $newline)
}

# Returns the changelog as it should read for the new version. An existing
# section for the version is left alone. Otherwise an [Unreleased] section
# becomes the release, which is how these projects have always cut one, and
# failing both the user is asked for the entry.
function Get-UpdatedChangelog {
    param([string]$Text, [string]$CurrentVersion, [string]$NewVersion, [string]$Date)
    if ((Get-ChangelogHeadingCount -Text $Text -Version $NewVersion) -gt 0) {
        Write-Host "CHANGELOG.md already has a section for $NewVersion, so it is left as it is."
        return $Text
    }

    $heading = "## [$NewVersion] - $Date"
    $unreleased = [regex]::Matches($Text, '(?m)^## \[Unreleased\][^\r\n]*')
    if ($unreleased.Count -gt 1) {
        throw "CHANGELOG.md has $($unreleased.Count) '## [Unreleased]' headings, so which one becomes $NewVersion is ambiguous."
    }
    if ($unreleased.Count -eq 1) {
        Write-Host "The [Unreleased] section of CHANGELOG.md becomes the section for $NewVersion."
        $Text = $Text.Remove($unreleased[0].Index, $unreleased[0].Length).Insert($unreleased[0].Index, $heading)
    }
    else {
        $entry = Read-ChangelogEntry -Version $NewVersion
        $first = [regex]::Match($Text, '(?m)^## ')
        if (-not $first.Success) {
            throw "CHANGELOG.md has no '## ' version heading, so where the section for $NewVersion goes is ambiguous."
        }
        $newline = if ($Text.Contains("`r`n")) { "`r`n" } else { "`n" }
        $block = $heading + $newline + $newline + ($entry -join $newline) + $newline + $newline
        $Text = $Text.Insert($first.Index, $block)
    }
    return Add-ChangelogLink -Text $Text -CurrentVersion $CurrentVersion -NewVersion $NewVersion
}

# ---------------------------------------------------------------------------
# The two goals
# ---------------------------------------------------------------------------

function Invoke-VersionBump {
    param([string]$Repository)
    Assert-CleanCheckout -Repository $Repository

    $locations = @(Get-VersionLocation -Repository $Repository)
    $current = Get-ConsistentVersion -Location $locations
    Write-Host "The current version is $current, recorded in $(($locations | ForEach-Object File) -join ', ')."

    Assert-OnMain -Repository $Repository
    Assert-MainMatchesRemote -Repository $Repository

    $changelogPath = Join-Path $Repository 'CHANGELOG.md'
    if (-not (Test-Path -LiteralPath $changelogPath -PathType Leaf)) {
        throw 'There is no CHANGELOG.md at the root of the repository.'
    }

    $new = Read-NewVersion -CurrentVersion $current
    $branch = "release-$new"
    Assert-BranchAbsent -Repository $Repository -Branch $branch

    # Everything is worked out in memory first, so a changelog that cannot be
    # read unambiguously stops the run before a single file has changed.
    $changelog = Read-TextFile $changelogPath
    $updatedChangelog = Get-UpdatedChangelog -Text $changelog -CurrentVersion $current -NewVersion $new -Date (Get-Today)
    $body = (Get-ChangelogSection -Text $updatedChangelog -Version $new) -replace "`r`n", "`n"

    Invoke-Git -Repository $Repository -Arguments @('switch', '--quiet', '--create', $branch) | Out-Null
    Write-VersionLocation -Location $locations -NewVersion $new
    $files = @($locations | ForEach-Object File)
    if ($updatedChangelog -cne $changelog) {
        Write-TextFile -Path $changelogPath -Text $updatedChangelog
        $files += 'CHANGELOG.md'
    }
    Invoke-Git -Repository $Repository -Arguments (@('add', '--') + $files) | Out-Null
    Invoke-Git -Repository $Repository -Arguments @('commit', '--quiet', '--message', "Release $new") | Out-Null

    Write-Host ''
    Write-Host "Committed ""Release $new"" on the new branch ${branch}:"
    (Invoke-Git -Repository $Repository -Arguments @('show', '--stat', '--format=', 'HEAD')).Output |
        ForEach-Object { Write-Host "  $_" }
    Write-Host ''

    if (-not (Confirm-Step "Push $branch to $($script:Remote)?")) {
        Write-Host "Stopped before pushing. The commit is on the local branch $branch; push it with: git push -u $($script:Remote) $branch"
        return
    }
    try {
        Invoke-Git -Repository $Repository -Arguments @('push', '--quiet', '--set-upstream', $script:Remote, $branch) | Out-Null
    }
    catch {
        throw "Pushing $branch failed, so the commit ""Release $new"" is only on the local branch $branch, which is still checked out. Running this again would stop because the checkout is not on $($script:MainBranch). Push it by hand with: git push -u $($script:Remote) $branch. $($_.Exception.Message)"
    }
    Write-Host "Pushed $branch."

    Write-Host ''
    Write-Host 'The pull request body will be the changelog section:'
    Write-Host ''
    $body -split "`n" | ForEach-Object { Write-Host "  $_" }
    Write-Host ''
    if (-not (Confirm-Step "Open a pull request from $branch into $($script:MainBranch)?")) {
        Write-Host "Stopped before opening the pull request. $branch is pushed; open one with: gh pr create --base $($script:MainBranch) --head $branch"
        return
    }

    $bodyFile = [IO.Path]::GetTempFileName()
    try {
        [IO.File]::WriteAllText($bodyFile, $body + "`n", [Text.UTF8Encoding]::new($false))
        $created = Invoke-Gh -WorkingDirectory $Repository -Arguments @(
            'pr', 'create', '--base', $script:MainBranch, '--head', $branch, '--title', "Release $new", '--body-file', $bodyFile)
        if ($created.ExitCode -ne 0) {
            throw "gh pr create failed with exit code $($created.ExitCode). $branch is pushed, so the pull request can be opened by hand. gh said: $($created.Output -join ' ')"
        }
    }
    finally {
        Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Opened the pull request: $($created.Output -join ' ')"
}

function Invoke-ReleaseTag {
    param([string]$Repository)
    # The versions are read from the files on disk, so they are only the
    # versions of the commit being tagged when nothing tracked has changed.
    Assert-CleanCheckout -Repository $Repository

    $locations = @(Get-VersionLocation -Repository $Repository)
    $version = Get-ConsistentVersion -Location $locations
    Write-Host "The current version is $version, recorded in $(($locations | ForEach-Object File) -join ', ')."
    if ($version -notmatch '^\d+\.\d+\.\d+$') {
        throw "The version '$version' is not of the MAJOR.MINOR.PATCH form, so v$version would not be the vX.Y.Z tag the release workflows expect."
    }

    $changelogPath = Join-Path $Repository 'CHANGELOG.md'
    if (-not (Test-Path -LiteralPath $changelogPath -PathType Leaf)) {
        throw 'There is no CHANGELOG.md at the root of the repository.'
    }
    Get-ChangelogSection -Text (Read-TextFile $changelogPath) -Version $version | Out-Null

    $tag = "v$version"
    Assert-TagAbsent -Repository $Repository -Tag $tag
    Assert-OnMain -Repository $Repository
    Assert-MainMatchesRemote -Repository $Repository

    $head = (Invoke-Git -Repository $Repository -Arguments @('log', '-1', '--format=%h %s')).Output[0]
    Write-Host "$($script:MainBranch) is at $head, the same commit as on $($script:Remote)."
    if (-not (Confirm-Step "Tag it $tag and push the tag to $($script:Remote), which triggers the release?")) {
        Write-Host 'Stopped. No tag was created.'
        return
    }
    Invoke-Git -Repository $Repository -Arguments @('tag', '--annotate', $tag, '--message', $tag) | Out-Null
    try {
        Invoke-Git -Repository $Repository -Arguments @('push', '--quiet', $script:Remote, "refs/tags/$tag") | Out-Null
    }
    catch {
        throw "Pushing $tag failed, so the tag exists only locally. Running this again would stop because the tag already exists locally. Push it by hand with: git push $($script:Remote) refs/tags/$tag, or remove it with: git tag --delete $tag. $($_.Exception.Message)"
    }
    Write-Host "Pushed $tag to $($script:Remote)."
}

function Invoke-PythonRelease {
    param([string]$RepositoryPath)
    Assert-GhReady
    $repository = Resolve-Repository -Path $RepositoryPath
    $goal = Read-Goal
    if ($goal -eq 'bump') {
        Invoke-VersionBump -Repository $repository
    }
    else {
        Invoke-ReleaseTag -Repository $repository
    }
}

# Dot-sourcing, which is what the tests do, defines the functions and stops.
if ($MyInvocation.InvocationName -eq '.') { return }

try {
    Invoke-PythonRelease -RepositoryPath $RepositoryPath
}
catch {
    $Host.UI.WriteErrorLine("python-release: $($_.Exception.Message)")
    exit 1
}
