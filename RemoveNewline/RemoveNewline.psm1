#Requires -Version 5.1
function Get-BomEncoding {
    # Detects a byte order mark and returns a matching encoding.
    # Falls back to UTF-8 without BOM when no mark is present.
    param([string]$FilePath)

    $bytes = [byte[]]::new(4)
    $stream = [System.IO.File]::OpenRead($FilePath)
    try { $count = $stream.Read($bytes, 0, 4) } finally { $stream.Dispose() }

    if ($count -ge 4 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE -and $bytes[2] -eq 0x00 -and $bytes[3] -eq 0x00) {
        return [System.Text.UTF32Encoding]::new($false, $true)
    }
    if ($count -ge 4 -and $bytes[0] -eq 0x00 -and $bytes[1] -eq 0x00 -and $bytes[2] -eq 0xFE -and $bytes[3] -eq 0xFF) {
        return [System.Text.UTF32Encoding]::new($true, $true)
    }
    if ($count -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        return [System.Text.UTF8Encoding]::new($true)
    }
    if ($count -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        return [System.Text.UnicodeEncoding]::new($false, $true)
    }
    if ($count -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
        return [System.Text.UnicodeEncoding]::new($true, $true)
    }
    return [System.Text.UTF8Encoding]::new($false)
}

function Remove-Newline {
    <#
    .SYNOPSIS
    Removes all line breaks from one or more text files.

    .DESCRIPTION
    Strips every line-ending sequence from a text file, joining the content
    into a single line. Handles CRLF (Windows), LF (Unix), CR (classic Mac),
    and the Unicode NEL, LINE SEPARATOR, and PARAGRAPH SEPARATOR characters.

    By default the file is rewritten in place. Use -Destination to write the
    result somewhere else. The source file's encoding and BOM are preserved
    when a BOM is present; otherwise UTF-8 without BOM is used.

    .PARAMETER Path
    One or more file paths. Wildcards are supported, and file objects from
    Get-ChildItem can be piped in.

    .PARAMETER Destination
    Output path. If it is an existing directory, each result is written there
    using the source file name. Otherwise it is treated as a file path.
    Omit to overwrite the source file.

    .PARAMETER Separator
    Text to insert where each line break was, for example ' '. Defaults to
    nothing, so lines are joined directly together.

    .EXAMPLE
    Remove-Newline .\notes.txt

    .EXAMPLE
    Remove-Newline .\notes.txt -Destination .\notes.oneline.txt -Separator ' '

    .EXAMPLE
    Get-ChildItem *.log | Remove-Newline -Destination .\flattened
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory, Position = 0, ValueFromPipeline, ValueFromPipelineByPropertyName)]
        [Alias('FullName')]
        [string[]]$Path,

        [Parameter(Position = 1)]
        [string]$Destination,

        [string]$Separator = ''
    )

    begin {
        $pattern = '\r\n|[\r\n\u0085\u2028\u2029]'
        $destIsDir = $Destination -and (Test-Path -LiteralPath $Destination -PathType Container)
    }

    process {
        foreach ($item in $Path) {
            $resolved = Resolve-Path -Path $item -ErrorAction SilentlyContinue
            if (-not $resolved) {
                Write-Error "File not found: $item"
                continue
            }

            foreach ($entry in $resolved) {
                $source = $entry.ProviderPath
                if (Test-Path -LiteralPath $source -PathType Container) {
                    Write-Verbose "Skipping directory: $source"
                    continue
                }

                if (-not $Destination) {
                    $target = $source
                }
                elseif ($destIsDir) {
                    $target = Join-Path $Destination (Split-Path $source -Leaf)
                }
                else {
                    $target = $Destination
                }
                $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($target)

                if (-not $PSCmdlet.ShouldProcess($source, "Remove newlines, write to $target")) {
                    continue
                }

                $encoding = Get-BomEncoding -FilePath $source
                $text = [System.IO.File]::ReadAllText($source, $encoding)
                $result = [regex]::Replace($text, $pattern, $Separator)
                [System.IO.File]::WriteAllText($target, $result, $encoding)

                Write-Verbose "Wrote $target"
            }
        }
    }
}

Export-ModuleMember -Function Remove-Newline
