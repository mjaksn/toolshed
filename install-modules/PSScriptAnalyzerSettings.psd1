# Two rules switched off for this tool, each with the reason. The installer
# itself, Install-ToolshedModule.ps1, passes every rule with nothing excluded:
# it returns objects rather than printing, and the one function in it that
# changes anything supports ShouldProcess. Both rules below are about the check
# and the tests beside it.
#
# Everything not named here stays on. PSUseShouldProcessForStateChangingFunctions
# in particular, because a script whose job is writing into Documents is exactly
# the one that should not lose that rule; the single false positive it raises is
# suppressed where it happens, on the function itself.
@{
    ExcludeRules = @(
        # check.ps1 reports progress to whoever is watching the run, and
        # Write-Host is the correct call for that in a script whose console is
        # its only interface. It fires three times, all in check.ps1.
        'PSAvoidUsingWriteHost',

        # test.ps1 calls its assertion helpers once per case with the case
        # name, then the expected value, then the actual one, so that each case
        # is one readable line. Naming three parameters on every one of them
        # would double the length of the file for no clarity gained. It fires
        # only there.
        'PSAvoidUsingPositionalParameters'
    )
}
