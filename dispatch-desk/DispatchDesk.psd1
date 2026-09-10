# Hand written rather than produced by New-ModuleManifest, which emits a
# hundred lines of commented placeholder for the six fields that matter here.
@{
    RootModule        = 'DispatchDesk.psm1'
    ModuleVersion     = '1.0.0'
    GUID              = '06cc2441-2446-4d9d-875c-69df92adb20d'
    Author            = 'mjaksn'
    Copyright         = 'MIT'
    Description       = 'Creates Windows desktop shortcuts that dispatch GitHub Actions workflows and watch the run to its conclusion.'

    # 5.1 and not 7. The shortcut this writes is launched by a .cmd that calls
    # powershell.exe, so the generated runtime runs under Windows PowerShell
    # whatever the machine has otherwise, and the two halves are kept to the
    # same floor deliberately.
    PowerShellVersion = '5.1'

    # Named rather than left to a wildcard, so the manifest says what is
    # exported instead of promising to find out at import time. This has to
    # agree with the Export-ModuleMember line at the foot of the .psm1.
    FunctionsToExport = @('New-WorkflowShortcut')
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()

    PrivateData = @{
        PSData = @{
            Tags       = @('GitHub', 'Actions', 'workflow', 'shortcut', 'Windows')
            LicenseUri = 'https://github.com/mjaksn/toolshed/blob/main/LICENSE'
            ProjectUri = 'https://github.com/mjaksn/toolshed/tree/main/dispatch-desk'
        }
    }
}
