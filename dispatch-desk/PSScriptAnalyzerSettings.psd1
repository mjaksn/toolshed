# Rules switched off for this tool, each because it is aimed at a module that
# exports cmdlets to other code and this is a program a person runs.
#
# Everything not named here stays on, including PSReviewUnusedParameter, which
# is the rule that earns its keep in a script this size.
@{
    ExcludeRules = @(
        # The console is the entire user interface. This program exists to
        # print a numbered list, colour a warning and ask a question, so
        # Write-Host is the correct call rather than a lapse, and the rule
        # fires thirty six times saying otherwise.
        'PSAvoidUsingWriteHost',

        # Get-WorkflowDispatchInputs returns every input a workflow declares.
        # The plural is what the function does.
        'PSUseSingularNouns',

        # Stop-WithError prints a message and exits. The rule reads the Stop
        # verb as a cmdlet that changes system state and asks for ShouldProcess
        # support, which would put a confirmation prompt in front of an error
        # message.
        'PSUseShouldProcessForStateChangingFunctions'
    )
}
