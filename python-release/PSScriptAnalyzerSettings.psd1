# Rules switched off for this tool, each with the reason. The script is an
# interactive program a person sits in front of, and its tests replace the
# console, gh and the clock with fakes.
#
# Everything not named here stays on, including PSReviewUnusedParameter and
# PSAvoidOverwritingBuiltInCmdlets, which test.ps1 suppresses at each site where
# it shadows a cmdlet on purpose rather than having the rule off for the
# directory.
@{
    ExcludeRules = @(
        # The console is the whole user interface. python-release.ps1 prints
        # what it found, shows the commit and the pull request body, and asks
        # before each step that leaves the machine, so Write-Host is the correct
        # call rather than a lapse. check.ps1 reports progress the same way.
        'PSAvoidUsingWriteHost',

        # test.ps1 calls its assertion helpers once per case with the case
        # name, then the expected value, then the actual one, and its git
        # helper with the repository then the git arguments, so that each case
        # is one readable line. Naming every parameter would double the length
        # of the file for no clarity gained. It fires only there.
        'PSAvoidUsingPositionalParameters'
    )
}
