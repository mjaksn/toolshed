# Rules switched off for this tool, each with the reason. The module itself,
# RemoveNewline.psm1, passes every rule with nothing excluded; the two below
# are about the check and the tests beside it.
#
# Everything not named here stays on.
@{
    ExcludeRules = @(
        # check.ps1 reports progress to whoever is watching the run, and
        # Write-Host is the correct call for that in a script whose console is
        # its only interface. It fires three times, all in check.ps1.
        'PSAvoidUsingWriteHost',

        # test.ps1 calls its assertion helper once per case with the case name,
        # the expected bytes and the path, in that order, so that each case is
        # one readable line. Naming the three parameters on every call would
        # double the length of the file for no clarity gained.
        'PSAvoidUsingPositionalParameters'
    )
}
