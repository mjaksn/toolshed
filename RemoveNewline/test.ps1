#Requires -Version 5.1
<#
.SYNOPSIS
    Tests for Remove-Newline. Run by check.ps1, and runnable by hand.

.DESCRIPTION
    Plain PowerShell with no test framework, so there is nothing to install or
    pin. Each case writes a file with known bytes into a temporary directory,
    runs the function over it, and compares the bytes that came out with the
    bytes expected. Exits non-zero if any case failed.

    Runs under Windows PowerShell 5.1 as well as 7, because 5.1 is the floor the
    module claims and a claim is worth checking rather than asserting.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Import-Module (Join-Path $PSScriptRoot 'RemoveNewline.psm1') -Force

$work = Join-Path ([System.IO.Path]::GetTempPath()) "removenewline-test-$([guid]::NewGuid())"
New-Item -ItemType Directory -Path $work | Out-Null

$script:failed = 0
$script:passed = 0

function Assert-Content {
    param([string]$Case, [byte[]]$Expected, [string]$Path)
    $actual = [System.IO.File]::ReadAllBytes($Path)
    $same = $Expected.Length -eq $actual.Length
    if ($same) {
        for ($i = 0; $i -lt $Expected.Length; $i++) {
            if ($Expected[$i] -ne $actual[$i]) { $same = $false; break }
        }
    }
    if ($same) {
        $script:passed++
        Write-Output "ok   $Case"
    }
    else {
        $script:failed++
        $show = { param($b) ($b | ForEach-Object { $_.ToString('x2') }) -join ' ' }
        Write-Output "FAIL $Case"
        Write-Output "     expected: $(& $show $Expected)"
        Write-Output "     actual:   $(& $show $actual)"
    }
}

function Write-Case {
    param([string]$Name, [byte[]]$Bytes)
    $path = Join-Path $work $Name
    [System.IO.File]::WriteAllBytes($path, $Bytes)
    return $path
}

$utf8 = [System.Text.UTF8Encoding]::new($false)
function Utf8 { param([string]$s) return $utf8.GetBytes($s) }

# Each line ending form, on its own.
$p = Write-Case 'crlf.txt' (Utf8 "a`r`nb`r`n")
Remove-Newline $p
Assert-Content 'CRLF is removed' (Utf8 'ab') $p

$p = Write-Case 'lf.txt' (Utf8 "a`nb`n")
Remove-Newline $p
Assert-Content 'LF is removed' (Utf8 'ab') $p

$p = Write-Case 'cr.txt' (Utf8 "a`rb`r")
Remove-Newline $p
Assert-Content 'bare CR is removed' (Utf8 'ab') $p

# Built from code points rather than written as `u{0085} and friends, because
# that escape is PowerShell 6 and later only, and these tests have to run on
# the 5.1 the module supports.
$separators = "a" + [char]0x0085 + "b" + [char]0x2028 + "c" + [char]0x2029 + "d"
$p = Write-Case 'unicode.txt' (Utf8 $separators)
Remove-Newline $p
Assert-Content 'NEL, LINE SEPARATOR and PARAGRAPH SEPARATOR are removed' (Utf8 'abcd') $p

$p = Write-Case 'mixed.txt' (Utf8 "a`r`nb`nc`rd")
Remove-Newline $p
Assert-Content 'mixed endings in one file are all removed' (Utf8 'abcd') $p

# The separator goes in once per line break, so CRLF counts as one.
$p = Write-Case 'separator.txt' (Utf8 "a`r`nb`nc")
Remove-Newline $p -Separator ' '
Assert-Content '-Separator replaces each line break once' (Utf8 'a b c') $p

# Encoding and byte order mark are preserved when present, and not invented
# when absent.
$bom8 = [byte[]](0xEF, 0xBB, 0xBF)
$p = Write-Case 'bom8.txt' ($bom8 + (Utf8 "a`r`nb"))
Remove-Newline $p
Assert-Content 'a UTF-8 byte order mark is kept' ($bom8 + (Utf8 'ab')) $p

$utf16 = [System.Text.UnicodeEncoding]::new($false, $true)
$p = Write-Case 'utf16le.txt' ($utf16.GetPreamble() + $utf16.GetBytes("a`r`nb"))
Remove-Newline $p
Assert-Content 'UTF-16 LE with a byte order mark stays UTF-16 LE' ($utf16.GetPreamble() + $utf16.GetBytes('ab')) $p

$p = Write-Case 'nobom.txt' (Utf8 "a`r`nb")
Remove-Newline $p
Assert-Content 'a file without a byte order mark does not gain one' (Utf8 'ab') $p

# Writing somewhere else leaves the source alone.
$outDir = Join-Path $work 'out'
New-Item -ItemType Directory -Path $outDir | Out-Null
$p = Write-Case 'todir.txt' (Utf8 "a`r`nb")
Remove-Newline $p -Destination $outDir
Assert-Content '-Destination as a directory keeps the source' (Utf8 "a`r`nb") $p
Assert-Content '-Destination as a directory writes the same leaf name there' (Utf8 'ab') (Join-Path $outDir 'todir.txt')

$p = Write-Case 'tofile.txt' (Utf8 "a`r`nb")
$target = Join-Path $work 'renamed.txt'
Remove-Newline $p -Destination $target
Assert-Content '-Destination as a file path keeps the source' (Utf8 "a`r`nb") $p
Assert-Content '-Destination as a file path writes there' (Utf8 'ab') $target

# -WhatIf describes and does nothing.
$p = Write-Case 'whatif.txt' (Utf8 "a`r`nb")
Remove-Newline $p -WhatIf
Assert-Content '-WhatIf leaves the file alone' (Utf8 "a`r`nb") $p

# Pipeline input from Get-ChildItem, and a wildcard, each reach every file.
$pipeDir = Join-Path $work 'pipe'
New-Item -ItemType Directory -Path $pipeDir | Out-Null
$p1 = Write-Case 'pipe\one.log' (Utf8 "a`r`nb")
$p2 = Write-Case 'pipe\two.log' (Utf8 "c`r`nd")
Get-ChildItem (Join-Path $pipeDir '*.log') | Remove-Newline
Assert-Content 'pipeline input from Get-ChildItem, first file' (Utf8 'ab') $p1
Assert-Content 'pipeline input from Get-ChildItem, second file' (Utf8 'cd') $p2

$p1 = Write-Case 'pipe\three.txt' (Utf8 "a`r`nb")
$p2 = Write-Case 'pipe\four.txt' (Utf8 "c`r`nd")
Remove-Newline (Join-Path $pipeDir '*.txt')
Assert-Content 'a wildcard path, first file' (Utf8 'ab') $p1
Assert-Content 'a wildcard path, second file' (Utf8 'cd') $p2

# A missing file is an error for that file and not for the run: the file named
# after it is still processed.
$p = Write-Case 'after-missing.txt' (Utf8 "a`r`nb")
$errors = @()
Remove-Newline (Join-Path $work 'absent.txt'), $p -ErrorAction SilentlyContinue -ErrorVariable errors
$reported = @($errors | Where-Object { $_.ToString() -like '*absent.txt*' }).Count -gt 0
if ($reported) {
    $script:passed++
    Write-Output 'ok   a missing file is reported'
}
else {
    $script:failed++
    Write-Output 'FAIL a missing file is reported'
}
Assert-Content 'a missing file does not stop the files after it' (Utf8 'ab') $p

Remove-Item -Recurse -Force $work

Write-Output ''
Write-Output "$script:passed passed, $script:failed failed"
if ($script:failed -gt 0) { exit 1 }
exit 0
