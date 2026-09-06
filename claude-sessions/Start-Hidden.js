// Start a command with no console window, wait for it, and exit with its exit code.
//
// Task Scheduler cannot start a console program hidden. pwsh.exe already owns a
// console by the time it reads -WindowStyle Hidden, and when Windows Terminal is
// the default terminal it takes that console over and shows a window for a moment
// before pwsh gets to hide it. This script runs under wscript.exe, which has no
// console of its own, and asks for the child to be created hidden from the start,
// so no window ever exists. conhost.exe --headless also works but always reports
// exit code 0, which would hide a failing run from the scheduler.
//
// The first argument is the program, the rest are its arguments. Anything with a
// space or a quote in it is quoted again, because the script host strips the
// quotes it was given. As a scheduled task action, with //B so a script error
// exits instead of showing a dialog:
//
//   wscript.exe //B //Nologo "Start-Hidden.js" "C:\Program Files\PowerShell\7\pwsh.exe" -NoProfile -File "script.ps1"

var args = WScript.Arguments;
if (args.length === 0) {
    WScript.Quit(2);
}

var parts = [];
for (var i = 0; i < args.length; i++) {
    var a = args(i);
    if (a === "" || /[\s"]/.test(a)) {
        a = '"' + a.replace(/"/g, '\\"') + '"';
    }
    parts.push(a);
}

var shell = new ActiveXObject("WScript.Shell");
WScript.Quit(shell.Run(parts.join(" "), 0, true));
