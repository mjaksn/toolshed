# Rules switched off for this tool. Each is aimed at code that other code
# calls, and both halves of this tool, the script and the module, are an
# interactive program a person sits in front of. The module form does export a
# command, so these are not switched off for want of a manifest; they are
# switched off because a numbered list and a Read-Host are what the command is.
#
# Everything not named here stays on, including PSReviewUnusedParameter, which
# is the rule that earns its keep in a script this size.
@{
    ExcludeRules = @(
        # The console is the entire user interface. This program exists to
        # print a numbered list, colour a warning and ask a question, so
        # Write-Host is the correct call rather than a lapse, and the rule
        # fires sixty nine times across this directory saying otherwise,
        # thirty six of them in the setup script, thirty in the module and
        # three in check.ps1.
        'PSAvoidUsingWriteHost',

        # Get-WorkflowDispatchInputs returns every input a workflow declares.
        # The plural is what the function does. It fires twice, once for the
        # copy in each file.
        'PSUseSingularNouns',

        # Three functions trip this. Stop-WithError, in both files, prints a
        # message and gives up; the rule reads the Stop verb as a cmdlet that
        # changes system state and asks for ShouldProcess support, which would
        # put a confirmation prompt in front of an error message.
        # New-WorkflowShortcut, in the module, does change state, but it shows
        # a summary and asks before writing anything, so a -WhatIf would be a
        # second confirmation bolted onto a flow that already has one.
        'PSUseShouldProcessForStateChangingFunctions'
    )
}
