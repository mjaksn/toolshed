#Requires -Version 7.4
<#
.SYNOPSIS
    Run PSScriptAnalyzer over this tool. Called by CI, and runnable by hand.

.DESCRIPTION
    The module is fetched straight from the gallery as a package file, checked
    against a recorded SHA256, and imported from where it was unpacked. It is
    never installed. That is the house rule about pinning a dependency by both
    version and hash, and Install-Module cannot do it: -RequiredVersion pins the
    version and nothing checks what arrived.

    The package is cached under the temporary directory, so running this twice
    in a row downloads once. The hash is checked on every run, including
    against the cached file, and a package that fails is deleted rather than
    left where the next run would reuse it. What is not rechecked is the
    unpacked copy beside it: that is trusted once its package verified, which
    is fine on a runner that is thrown away and worth knowing on a machine
    that is not.

.PARAMETER Refresh
    Ignore any cached package and fetch it again.
#>
[CmdletBinding()]
param(
    [switch]$Refresh
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Pinned by version and by hash. Raising the version means replacing both, and
# the hash is the SHA256 of the .nupkg the gallery serves for that version.
$Version = '1.25.0'
$Sha256  = '14e634c828eb98efb9f40b2918ba90f139ed5eccdf663a2a747736d996995d60'

$here  = $PSScriptRoot
$root  = Join-Path ([System.IO.Path]::GetTempPath()) "psscriptanalyzer-$Version"
$pkg   = "$root.zip"

if ($Refresh -and (Test-Path $root)) { Remove-Item $root -Recurse -Force }
if ($Refresh -and (Test-Path $pkg))  { Remove-Item $pkg -Force }

if (-not (Test-Path $pkg)) {
    Write-Host "fetching PSScriptAnalyzer $Version"
    # Written to a .zip name because that is what it is, and because
    # Expand-Archive has been particular about the extension in the past.
    Invoke-WebRequest -MaximumRedirection 5 -OutFile $pkg `
        -Uri "https://www.powershellgallery.com/api/v2/package/PSScriptAnalyzer/$Version"
}

$actual = (Get-FileHash $pkg -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $Sha256) {
    Remove-Item $pkg -Force
    throw "PSScriptAnalyzer $Version did not match its pin. Expected $Sha256, got $actual. The package has been deleted rather than left where a later run would use it."
}

if (-not (Test-Path $root)) {
    Expand-Archive -Path $pkg -DestinationPath $root
}

Import-Module (Join-Path $root 'PSScriptAnalyzer.psd1')

$settings = Join-Path $here 'PSScriptAnalyzerSettings.psd1'
$findings = @(Invoke-ScriptAnalyzer -Path $here -Recurse -Settings $settings)

if ($findings.Count -eq 0) {
    Write-Host "PSScriptAnalyzer $Version found nothing."
    exit 0
}

$findings | ForEach-Object {
    "{0}:{1}:{2} {3} {4}" -f (Split-Path $_.ScriptName -Leaf), $_.Line, $_.Column, $_.Severity, $_.RuleName
    "    {0}" -f $_.Message
}
Write-Host ""
throw "PSScriptAnalyzer $Version reported $($findings.Count) finding(s)."
