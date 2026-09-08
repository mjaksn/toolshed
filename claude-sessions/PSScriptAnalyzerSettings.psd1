# One rule switched off for this tool, because it is aimed at a module that
# exports cmdlets to other code and these are programs a person runs.
#
# Everything not named here stays on, including PSUseSingularNouns and
# PSReviewUnusedParameter. Both fired once when this check was added and both
# were answered in the code rather than here: a helper was renamed to a
# singular noun, and the transcript directory is passed to the function that
# uses it rather than reached for out of the enclosing scope.
@{
    ExcludeRules = @(
        # The console is the entire user interface. These scripts exist to
        # report what is open, what was written and what was reopened, so
        # Write-Host is the correct call rather than a lapse. The rule fires
        # fourteen times across this directory saying otherwise.
        'PSAvoidUsingWriteHost'
    )
}
