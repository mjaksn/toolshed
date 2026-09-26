#!/usr/bin/env bash
# The check CI runs for easy-systemd, and the way to run it by hand.
#
# It installs and removes real services, because what matters is what systemd
# makes of the unit files, and nothing short of systemd can say. So it needs
# root and a machine booted with systemd, and it runs itself again under sudo
# if started without root. Every service it creates is named es-check-something
# and is removed on the way out, pass or fail.
set -uo pipefail

if [[ ! -d /run/systemd/system ]]; then
    echo "check.sh: this machine is not running systemd" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi

cd -- "$(dirname -- "$0")" || exit 1
# Under sudo this names whoever ran it, and install.sh would run every service
# as them. Each test says which user it wants instead.
unset SUDO_USER

tmp=$(mktemp -d)
# The service user has to reach the working directory, and mktemp makes its
# directory private. The space and the % are there to be escaped.
work="$tmp/work dir%x"
mkdir "$work"
chmod 755 "$tmp" "$work"
# Copies nobody can read wherever the checkout is, for the tests that run the
# scripts without root.
cp install.sh uninstall.sh "$tmp"
chmod 644 "$tmp/install.sh" "$tmp/uninstall.sh"

cleanup() {
    local path
    for path in /etc/systemd/system/es-check-*.service; do
        [[ -e $path ]] || continue
        systemctl disable --now --quiet -- "$(basename -- "$path")" 2>/dev/null
        rm -f -- "$path"
    done
    systemctl daemon-reload
    systemctl reset-failed 'es-check-*' 2>/dev/null
    rm -rf -- "$tmp"
}
trap cleanup EXIT

failures=0
ok() { echo "ok    $1"; }
not_ok() {
    echo "FAIL  $1"
    failures=$((failures + 1))
}

# expect DESCRIPTION COMMAND...: passes when the command succeeds.
expect() {
    local desc=$1
    shift
    if "$@"; then ok "$desc"; else not_ok "$desc"; fi
}

# refuses DESCRIPTION MESSAGE COMMAND...: passes when the command fails and
# says MESSAGE on the way.
refuses() {
    local desc=$1 message=$2 output
    shift 2
    if output=$("$@" 2>&1); then
        not_ok "$desc (it succeeded)"
    elif [[ $output != *"$message"* ]]; then
        not_ok "$desc (it said: $output)"
    else
        ok "$desc"
    fi
}

# eventually COMMAND...: waits up to ten seconds for the command to succeed.
eventually() {
    local i
    for ((i = 0; i < 50; i++)); do
        "$@" && return 0
        sleep 0.2
    done
    return 1
}

run_install() { bash install.sh "$@" >/dev/null; }
run_uninstall() { bash uninstall.sh "$@" >/dev/null; }
as_nobody() { (cd "$tmp" && runuser -u nobody -- bash "$@"); }
unit_file() { echo "/etc/systemd/system/$1.service"; }
main_pid() { systemctl show -p MainPID --value -- "$1.service"; }
running() { [[ $(systemctl is-active -- "$1.service") == active ]]; }
restarted() { local now; now=$(main_pid "$1"); [[ $now != 0 && $now != "$2" ]]; }
runs() { ps -o args= -p "$(main_pid "$1")" | grep -qxF -- "$2"; }
first_line_is_marker() { [[ $(head -n1 "$(unit_file "$1")") == "# Created by easy-systemd"* ]]; }
holds() { [[ -s $1 && $(cat "$1") == "$2" ]]; }
lists() { bash uninstall.sh | grep -qx "  $1"; }
dead() { ! kill -0 "$1" 2>/dev/null; }
gone() {
    [[ ! -e $(unit_file "$1") ]] && ! systemctl is-enabled --quiet -- "$1.service" 2>/dev/null
}

# Anything already installed by install.sh on this machine belongs to someone,
# and --all would take it.
mapfile -t existing < <(bash uninstall.sh | sed -n 's/^  //p' | grep -v '^es-check-')

echo "Refusals"
refuses "a name with a space" "NAME may hold" run_install 'es check' -- sleep 1
refuses "a template name" "NAME may hold" run_install 'es-check@x' -- sleep 1
refuses "an unknown restart policy" "unknown restart policy" run_install -r sometimes es-check-x -- sleep 1
refuses "a fractional restart delay" "whole number" run_install -s 1.5 es-check-x -- sleep 1
refuses "a user that does not exist" "no such user" run_install -u es-check-nobody es-check-x -- sleep 1
refuses "a directory that does not exist" "no such directory" run_install -d "$tmp/none" es-check-x -- sleep 1
refuses "no command" "no command given" run_install es-check-x
refuses "a command not on PATH" "not found on PATH" run_install es-check-x -- es-check-nosuch
refuses "a path that is not executable" "not an executable" run_install -d "$work" es-check-x -- ./none.sh
refuses "-e without =" "-e takes KEY=VALUE" run_install -e NOEQUALS es-check-x -- sleep 1
refuses "-e with a bad key" "-e takes KEY=VALUE" run_install -e 'A-B=1' es-check-x -- sleep 1
refuses "a line break in an argument" "line break" run_install es-check-x -- sleep $'1\n2'
refuses "running without root" "must be run as root" as_nobody install.sh es-check-x -- sleep 1
refuses "a name the system already uses" "already exists elsewhere" run_install systemd-journald -- sleep 1
printf '[Service]\nExecStart=/bin/true\n' >"$(unit_file es-check-foreign)"
refuses "a unit install.sh did not create" "was not created by install.sh" run_install es-check-foreign -- sleep 1
expect "and leaves that unit as it was" grep -qx 'ExecStart=/bin/true' "$(unit_file es-check-foreign)"
expect "nothing was installed by any of these" test ! -e "$(unit_file es-check-x)"

echo "Installing"
SUDO_USER=nobody run_install -d "$work" es-check-default -- sleep infinity
expect "the user defaults to whoever ran sudo" grep -qx 'User=nobody' "$(unit_file es-check-default)"
run_install -d "$work" es-check-default -- sleep infinity
expect "and to root without sudo" grep -qx 'User=root' "$(unit_file es-check-default)"
# Before any unit runs as nobody, because verify loads every unit there is and
# warns about that one.
expect "systemd-analyze verify accepts the unit" systemd-analyze verify "$(unit_file es-check-default)"
run_install -d "$work" -u nobody es-check-sleep -- sleep infinity
expect "the service is running" eventually running es-check-sleep
expect "it is enabled for boot" systemctl is-enabled --quiet es-check-sleep.service
expect "the marker is the first line" first_line_is_marker es-check-sleep
pid=$(main_pid es-check-sleep)
expect "it runs as the user asked for" test "$(ps -o user= -p "$pid")" = nobody
expect "in the directory asked for" test "$(readlink "/proc/$pid/cwd")" = "$work"
expect "the command is resolved to a full path" \
    grep -qx 'ExecStart="/[^"]*/sleep" "infinity"' "$(unit_file es-check-sleep)"

echo "Arguments and environment"
out=$tmp/out
run_install -u root -r no es-check-args -- /bin/sh -c 'printf "[%s]\n" "$@" >"$0"' "$out" \
    'a b' '%h' '$HOME' 'q"q' 'b\s' ';' '' 'x&y'
expect "every argument arrives as it was given" \
    eventually holds "$out" "$(printf '[%s]\n' 'a b' '%h' '$HOME' 'q"q' 'b\s' ';' '' 'x&y')"
rm -f "$out"
run_install -u root -r no -e 'A=a b' -e 'B=%h' -e 'C=$HOME' -e 'D=q"q' -e 'E=b\s' -e 'F=' -e 'G=x=y' \
    es-check-env -- /bin/sh -c 'printf "[%s]\n" "$A" "$B" "$C" "$D" "$E" "${F-unset}" "$G" >"$0"' "$out"
expect "every environment variable arrives as it was given" \
    eventually holds "$out" "$(printf '[%s]\n' 'a b' '%h' '$HOME' 'q"q' 'b\s' '' 'x=y')"

echo "Restarting"
run_install -d "$work" -s 1 es-check-restart -- sleep infinity
eventually running es-check-restart
first=$(main_pid es-check-restart)
kill "$first"
expect "a killed service is started again" eventually restarted es-check-restart "$first"

echo "Reinstalling"
printf '#!/bin/sh\nexec sleep 1000\n' >"$work/run.sh"
chmod 755 "$work/run.sh"
run_install -d "$work" -u nobody -r on-failure -s 2 es-check-sleep.service ./run.sh
expect "the unit carries the new command" \
    grep -qxF "ExecStart=\"${work//'%'/'%%'}/run.sh\"" "$(unit_file es-check-sleep)"
expect "and the new restart policy" grep -qx 'Restart=on-failure' "$(unit_file es-check-sleep)"
expect "and the running process is the new one" eventually runs es-check-sleep 'sleep 1000'
pid=$(main_pid es-check-sleep)

echo "Uninstalling"
expect "a bare run lists what install.sh created" lists es-check-sleep
expect "and not what it did not" eval '! lists es-check-foreign'
refuses "a name that is not installed" "no such unit" run_uninstall es-check-none
refuses "a list holding one foreign unit" "was not created by install.sh" \
    run_uninstall es-check-args es-check-foreign
expect "removes none of the others" test -e "$(unit_file es-check-args)"
refuses "running without root" "must be run as root" as_nobody uninstall.sh es-check-args
run_uninstall es-check-args.service
expect "a named unit is removed, suffix or not" gone es-check-args
run_uninstall es-check-sleep
expect "a running one is stopped as well" gone es-check-sleep
expect "and its process has ended" eventually dead "$pid"
if [[ ${#existing[@]} -eq 0 ]]; then
    run_uninstall --all
    expect "--all removes every unit install.sh created" gone es-check-env
    expect "all of them" gone es-check-restart
    expect "and leaves the foreign one" test -e "$(unit_file es-check-foreign)"
else
    echo "skip  --all, since install.sh has units of its own here: ${existing[*]}"
fi

echo
if [[ $failures -eq 0 ]]; then
    echo "All checks passed."
else
    echo "$failures failed."
    exit 1
fi
