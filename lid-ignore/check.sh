#!/usr/bin/env bash
# The check CI runs for lid-ignore, and the way to run it by hand.
#
# The file helpers are tried on files of their own first, and the whole script
# is then run for real, because what matters is what lands in /etc and in a
# user's home, and nothing short of running it can say. So it needs root and a
# machine booted with systemd, and it runs itself again under sudo if started
# without root. It writes the logind drop-in the script writes, restarts
# logind as the script does, and configures a throwaway user named lid-check
# whose desktop tools are stand-ins that record what they were told. On the
# way out, pass or fail, it puts every file in /etc back as it found it,
# restarts logind again if the drop-in changed, and removes the user.
set -uo pipefail

if [[ ! -d /run/systemd/system ]]; then
    echo "check.sh: this machine is not running systemd" >&2
    exit 1
fi
if ! command -v dbus-run-session >/dev/null 2>&1; then
    echo "check.sh: dbus-run-session is not installed, and the throwaway user's desktop tools run on it" >&2
    exit 1
fi
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi

cd -- "$(dirname -- "$0")" || exit 1
script=$PWD/lid-ignore.sh
user=lid-check
# The cleanup removes this user and their home, so neither may exist already.
if id "$user" >/dev/null 2>&1 || [[ -e /home/$user ]]; then
    echo "check.sh: the user $user or /home/$user already exists, and the cleanup would remove them" >&2
    exit 1
fi
# Under sudo this names whoever ran it, and the script would configure their
# desktop as well. Every run below says which user it wants instead.
unset SUDO_USER

dropin=/etc/systemd/logind.conf.d/99-ignore-lid.conf
dropin_dir=$(dirname "$dropin")
# Every file in /etc the script can touch. Each is copied aside now and put
# back, or removed if it was not there, by the cleanup.
etc_files=("$dropin" /etc/UPower/UPower.conf /etc/default/acpi-support)

tmp=$(mktemp -d)
# The throwaway user has to reach the stand-ins, and nobody the script copy,
# and mktemp makes its directory private.
chmod 755 "$tmp"
backup=$tmp/backup
stub=$tmp/stub
unitbin=$tmp/unitbin
state=$tmp/state
mkdir "$backup" "$stub" "$unitbin"
mkdir -m 1777 "$state"
made_dropin_dir=0
[[ -d $dropin_dir ]] || made_dropin_dir=1
for f in "${etc_files[@]}"; do
    [[ -f $f ]] || continue
    # The cleanup takes a missing copy to mean the file was not there and
    # removes it, so a copy that failed must stop everything here, before
    # anything is changed.
    if ! cp -p "$f" "$backup/${f//\//_}"; then
        echo "check.sh: could not copy $f aside, so it could not be put back; nothing was changed" >&2
        rm -rf -- "$tmp"
        exit 1
    fi
done
# A copy nobody can read wherever the checkout is, for the test that runs the
# script without root.
cp lid-ignore.sh "$tmp"
chmod 644 "$tmp/lid-ignore.sh"

# restart SERVICE: reports a restart that fails, since the cleanup must not
# claim a setting is back when the service that holds it never reread it.
restart() {
    systemctl restart "$1" && return 0
    echo "check.sh: could not restart $1 after putting its configuration back" >&2
    return 1
}

cleanup() {
    local status=$? f saved left="" was now
    for f in "${etc_files[@]}"; do
        saved=$backup/${f//\//_}
        was=$(cat "$saved" 2>/dev/null)
        now=$(cat "$f" 2>/dev/null)
        if [[ -f $saved ]]; then
            cp -p "$saved" "$f"
        else
            rm -f "$f"
        fi
        [[ $was == "$now" ]] && continue
        # The effective setting goes back to what it was, not just the file,
        # so the service that read it is restarted as the script restarted it,
        # where it is running.
        case $f in
            "$dropin") restart systemd-logind || status=1 ;;
            /etc/UPower/UPower.conf)
                ! systemctl is-active --quiet upower || restart upower || status=1 ;;
            /etc/default/acpi-support)
                ! systemctl is-active --quiet acpid || systemctl reload acpid 2>/dev/null \
                    || restart acpid || status=1 ;;
        esac
    done
    [[ $made_dropin_dir -eq 1 ]] && rmdir "$dropin_dir" 2>/dev/null
    pkill -u "$user" 2>/dev/null
    id "$user" >/dev/null 2>&1 && userdel -r "$user" 2>/dev/null
    # Judged by what is left rather than by each step. A run that leaves the
    # user or an unexpected drop-in behind fails, however its tests went.
    [[ -e $dropin && ! -f $backup/${dropin//\//_} ]] && left+="$dropin"$'\n'
    id "$user" >/dev/null 2>&1 && left+="user $user"$'\n'
    [[ -e /home/$user ]] && left+="/home/$user"$'\n'
    rm -rf -- "$tmp"
    if [[ -n $left ]]; then
        echo "check.sh: the cleanup left these behind:" >&2
        printf '%s' "$left" >&2
        status=1
    fi
    exit "$status"
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

# in_script FUNCTION ARG...: runs one of the script's own functions, with the
# script sourced in a subshell so that its set -u and its ok and fail stay out
# of this one. The script resets DRY_RUN when sourced, so it is set after.
in_script() { ( source "$script"; DRY_RUN=${DRY:-0}; "$@" ); }
dry() { DRY=1 in_script "$@"; }
# in_home HOME FUNCTION ARG...: in_script with the user's home set, for the
# functions that work inside one.
in_home() { ( source "$script"; DRY_RUN=0; U_HOME=$1; U_UID=$uid; shift; "$@" ); }
# users_after SETUP...: runs collect_users after the given command, and prints
# the users it settled on, one per line.
users_after() { ( source "$script"; "$@"; collect_users; printf '%s\n' ${USERS[@]+"${USERS[@]}"} ); }
# holds FILE CONTENT: the file holds exactly CONTENT, escapes and all.
holds() { printf '%b' "$2" | cmp -s - "$1"; }
says() { grep -qF -- "$1" <<<"$2"; }
lacks() { ! grep -qF -- "$1" <<<"$2"; }
snapshot() {
    find "$dropin_dir" /etc/UPower/UPower.conf /etc/default/acpi-support "/home/$user" \
        -printf '%p %T@ %s %U\n' 2>/dev/null | sort
}

# The throwaway user, with the files that make the script take them for a KDE,
# LXQt and XFCE user at once. The KDE profile holds one of the three sections
# already, with the lid set to do something, so the test sees both an edit in
# place and two additions.
useradd -m -s /bin/bash "$user" 2>/dev/null || { echo "check.sh: could not create $user" >&2; exit 1; }
uid=$(id -u "$user")
home=$(getent passwd "$user" | cut -d: -f6)
cfg=$home/.config
mkdir -p "$cfg/xfce4" "$cfg/lxqt"
printf '[AC][SuspendAndShutdown]\nLidAction=1\nPowerButtonAction=16\n' >"$cfg/powerdevilrc"
chown -R "$user:" "$cfg"

# Stand-ins for the desktop tools, found first on PATH when the script runs.
# Each keeps what it is told under $state, and notes who ran it, which should
# be the user and never root, and on which bus, which should be a private one
# since the user has no session.
power=org.gnome.settings-daemon.plugins.power
cat >"$stub/xfconf-query" <<EOF
#!/usr/bin/env bash
# Stands in for xfconf-query: -p PROP reads, and -s VALUE writes, one file per
# property. A property never set reads as nothing, as the real one does.
state='$state'
prop= value= set=0
while [[ \$# -gt 0 ]]; do
    case \$1 in
        -p) prop=\$2; shift ;;
        -s) value=\$2; set=1; shift ;;
    esac
    shift
done
file=\$state/xfconf\${prop//\//.}
if [[ \$set -eq 1 ]]; then
    printf '%s\n' "\$value" >"\$file"
    id -un >"\$state/ran-as"
    printf '%s\n' "\${DBUS_SESSION_BUS_ADDRESS:-none}" >"\$state/bus"
else
    cat "\$file" 2>/dev/null || exit 1
fi
EOF
cat >"$stub/gsettings" <<EOF
#!/usr/bin/env bash
# Stands in for gsettings, with the GNOME power schema and one more schema
# that mentions the lid, whose keys the script should list for review, one
# of them caught by its own filter.
state='$state'
power=$power
current() { cat "\$state/gsettings.\$1.\$2" 2>/dev/null || echo "'suspend'"; }
case \$1 in
    list-schemas) printf '%s\n' "\$power" org.example.lid ;;
    list-keys) [[ \$2 == "\$power" ]] && printf '%s\n' lid-close-ac-action lid-close-battery-action ;;
    get) current "\$2" "\$3" ;;
    set)
        printf '%s\n' "\$4" >"\$state/gsettings.\$2.\$3"
        id -un >"\$state/ran-as"
        printf '%s\n' "\${DBUS_SESSION_BUS_ADDRESS:-none}" >"\$state/bus"
        ;;
    list-recursively)
        printf '%s %s %s\n' \\
            "\$power" lid-close-ac-action "\$(current "\$power" lid-close-ac-action)" \\
            "\$power" lid-close-battery-action "\$(current "\$power" lid-close-battery-action)" \\
            org.example.lid close-action "'suspend'" \\
            org.example.lid valid-closes true
        ;;
esac
EOF
# Stand-ins for the helper tests only, kept off the PATH of the real runs.
printf '#!/bin/sh\nprintf "systemd 255 (255.4-1ubuntu8.4)\\n+PAM +AUDIT\\n"\n' >"$unitbin/systemctl"
printf '#!/bin/sh\ncat "%s/users.txt"\n' "$unitbin" >"$unitbin/loginctl"
chmod 755 "$stub"/* "$unitbin"/*

echo "write_file"
f=$tmp/w/deep/f
out=$(printf 'a\nb' | in_script write_file "$f")
expect "creates the file and its directory, ending it with a line break" holds "$f" 'a\nb\n'
expect "and says so" says "[ set ] write $f" "$out"
out=$(printf 'a\nb' | in_script write_file "$f")
expect "the same content again is reported as already correct" says "$f already correct" "$out"
printf 'c' | in_script write_file "$f" >/dev/null
expect "different content replaces it" holds "$f" 'c\n'
out=$(printf 'd' | dry write_file "$f")
expect "a dry run says what it would write" says "[would] write $f" "$out"
expect "and writes nothing" holds "$f" 'c\n'

echo "kv_set"
f=$tmp/kv
printf 'FOO=1\nLID_SLEEP_X=true\n' >"$f"
in_script kv_set "$f" LID_SLEEP false >/dev/null
expect "appends a key that is missing, and does not mistake a longer one for it" \
    holds "$f" 'FOO=1\nLID_SLEEP_X=true\nLID_SLEEP=false\n'
printf '  LID_SLEEP=true\nOTHER=x\n' >"$f"
in_script kv_set "$f" LID_SLEEP false >/dev/null
expect "replaces one that is there, however indented, and keeps the rest" holds "$f" 'LID_SLEEP=false\nOTHER=x\n'
out=$(in_script kv_set "$f" LID_SLEEP false)
expect "says when it is already set" says "$f: LID_SLEEP=false already set" "$out"
expect "and leaves the file alone" holds "$f" 'LID_SLEEP=false\nOTHER=x\n'
out=$(dry kv_set "$f" LID_SLEEP true)
expect "a dry run says what it would set" says "[would] $f: LID_SLEEP=true" "$out"
expect "and sets nothing" holds "$f" 'LID_SLEEP=false\nOTHER=x\n'
in_script kv_set "$tmp/kv-new" LID_SLEEP false >/dev/null
expect "a file that is not there is created" holds "$tmp/kv-new" 'LID_SLEEP=false\n'

echo "ini_set"
f=$tmp/ini/UPower.conf
in_script ini_set "$f" UPower IgnoreLid true >/dev/null
expect "creates a missing file with the section and the key" holds "$f" '[UPower]\nIgnoreLid=true\n'
printf '[UPower]\nEnableWattsUpPro=false\n' >"$f"
in_script ini_set "$f" UPower IgnoreLid true >/dev/null
expect "adds the key to a section that lacks it" holds "$f" '[UPower]\nEnableWattsUpPro=false\nIgnoreLid=true\n'
printf '[UPower]\n# a comment\nIgnoreLid=false\nOther=1\n' >"$f"
in_script ini_set "$f" UPower IgnoreLid true >/dev/null
expect "replaces a key that is there, in its place, keeping the lines around it" \
    holds "$f" '[UPower]\n# a comment\nIgnoreLid=true\nOther=1\n'
printf '[A]\nk=1\n\n[B]\nk=1\n' >"$f"
in_script ini_set "$f" B k 2 >/dev/null
expect "leaves the same key in another section alone" holds "$f" '[A]\nk=1\n\n[B]\nk=2\n'
printf '[A]\nk=1\n\n[B]\nz=1\n' >"$f"
in_script ini_set "$f" A j 2 >/dev/null
expect "puts a new key before the blank lines that end its section" holds "$f" '[A]\nk=1\nj=2\n\n[B]\nz=1\n'
printf '[A]\nk=1\n\n' >"$f"
in_script ini_set "$f" A j 2 >/dev/null
expect "also when that section is the last" holds "$f" '[A]\nk=1\nj=2\n\n'
printf '[A]\nk=1\n' >"$f"
in_script ini_set "$f" B k 2 >/dev/null
expect "appends a missing section after a blank line" holds "$f" '[A]\nk=1\n\n[B]\nk=2\n'
printf '[A]\nLidActionX=1\n' >"$f"
in_script ini_set "$f" A LidAction 0 >/dev/null
expect "does not mistake a longer key for the one asked for" holds "$f" '[A]\nLidActionX=1\nLidAction=0\n'
rm "$f"
in_script ini_set "$f" 'AC][SuspendAndShutdown' LidAction 0 >/dev/null
expect "takes the nested groups KDE uses" holds "$f" '[AC][SuspendAndShutdown]\nLidAction=0\n'
cp -p "$f" "$f.before"
out=$(in_script ini_set "$f" 'AC][SuspendAndShutdown' LidAction 0)
expect "says when the key is already set" says "[AC][SuspendAndShutdown] LidAction=0 already set" "$out"
expect "and leaves the file byte for byte" cmp -s "$f" "$f.before"
out=$(dry ini_set "$tmp/ini/none" X y z)
expect "a dry run says what it would set" says "[would] $tmp/ini/none: [X] y=z" "$out"
expect "and creates nothing" test ! -e "$tmp/ini/none"

echo "user_ini_set"
home2=$tmp/home2
mkdir "$home2"
chown "$user:" "$home2"
f=$home2/.config/lxqt/lxqt-powermanagement.conf
in_home "$home2" user_ini_set "$user" "$f" General lidWatcherEnabled false >/dev/null
expect "writes the file" holds "$f" '[General]\nlidWatcherEnabled=false\n'
expect "as the user, so the file and every directory made on the way are theirs" \
    test "$(stat -c %U "$home2/.config" "$home2/.config/lxqt" "$f" | sort -u)" = "$user"
# Links the user could plant, to a file and to a directory that only root can
# write. An edit made as the user fails at them; one made as root would not.
printf 'outside\n' >"$tmp/outside"
ln -s "$tmp/outside" "$home2/.config/powerdevilrc"
out=$(in_home "$home2" user_ini_set "$user" "$home2/.config/powerdevilrc" 'AC][SuspendAndShutdown' LidAction 0 2>&1)
expect "cannot be led through a link to a file the user cannot write" \
    says "[FAIL] could not edit $home2/.config/powerdevilrc" "$out"
expect "and leaves that file alone" holds "$tmp/outside" 'outside\n'
mkdir "$tmp/elsewhere"
ln -s "$tmp/elsewhere" "$home2/.config/kde"
out=$(in_home "$home2" user_ini_set "$user" "$home2/.config/kde/powerdevilrc" 'AC][SuspendAndShutdown' LidAction 0 2>&1)
expect "nor into a directory the user cannot write" says "[FAIL] could not edit $home2/.config/kde/powerdevilrc" "$out"
expect "writing nothing there" test -z "$(ls -A "$tmp/elsewhere")"
expect "and the failure is counted" test "$(in_home "$home2" eval \
    'user_ini_set "$user" "$home2/.config/powerdevilrc" X k v >/dev/null 2>&1; echo "$FAILURES"')" = 1
expect "a dry run as the user reports and writes nothing" \
    test "$(DRY=1 in_home "$home2" eval 'DRY_RUN=1; user_ini_set "$user" "$home2/.config/new.conf" X k v' 2>&1)" = "[would] $home2/.config/new.conf: [X] k=v"
expect "not even the file" test ! -e "$home2/.config/new.conf"

echo "systemd_version"
expect "reads the number from the first line of systemctl --version" \
    test "$(PATH="$unitbin:$PATH" in_script systemd_version)" = 255

echo "collect_users"
expect "users given with --user are kept as given" \
    test "$(users_after eval 'USERS=(alice bob)')" = $'alice\nbob'
printf '1000 alice seat0\n   0 root\n1001 bob\n1000 alice\n' >"$unitbin/users.txt"
expect "otherwise the sudo user and the logged-in users, once each, without root" \
    test "$(SUDO_USER=alice PATH="$unitbin:$PATH" users_after :)" = $'alice\nbob'
: >"$unitbin/users.txt"
expect "a sudo user of root and nobody logged in leaves none" \
    test -z "$(SUDO_USER=root PATH="$unitbin:$PATH" users_after :)"
all=$(users_after eval 'ALL_USERS=1')
expect "--all-users names every account with a home directory, so the throwaway user" says "$user" "$all"
expect "and not root" lacks root "$all"

echo "user_env"
# Disowned, and with runuser's stderr dropped, so that neither bash nor
# runuser announces its end when it is killed below.
runuser -u "$user" -- env DBUS_SESSION_BUS_ADDRESS=unix:path=/nowhere DISPLAY=:9 sleep 60 2>/dev/null &
sleeper=$!
disown
sleep 0.5
out=$(in_script user_env "$uid")
expect "finds the session bus of a process of the user" says "DBUS_SESSION_BUS_ADDRESS=unix:path=/nowhere" "$out"
expect "and the display" says "DISPLAY=:9" "$out"
pkill -u "$user" 2>/dev/null
while kill -0 "$sleeper" 2>/dev/null; do sleep 0.1; done
expect "says nothing for a uid with no processes" test -z "$(in_script user_env 59999)"

echo "xfce_pm_running"
# Processes with the names, as the user; exec -a puts the name on the command
# line, which is where the match has to happen, since the process name field
# holds only its first 15 characters.
runuser -u "$user" -- bash -c 'exec -a xfce4-power-manager-settings sleep 60' 2>/dev/null &
disown
sleep 0.5
expect "does not take the settings dialog for the manager" eval '! in_script xfce_pm_running "$uid"'
runuser -u "$user" -- bash -c 'exec -a xfce4-power-manager sleep 60' 2>/dev/null &
manager=$!
disown
sleep 0.5
expect "finds the manager by its full name" in_script xfce_pm_running "$uid"
pkill -u "$user" 2>/dev/null
while kill -0 "$manager" 2>/dev/null; do sleep 0.1; done

echo "Options"
expect "--help prints the usage and exits 0" \
    bash -c 'bash "$1" --help | grep -q "^Usage: sudo ./lid-ignore.sh"' _ "$script"
refuses "an unknown option" "unknown option: --bogus" bash "$script" --bogus
refuses "--user without a name" "Usage:" bash "$script" --user
refuses "running without root" "must run as root" runuser -u nobody -- bash "$tmp/lid-ignore.sh" --dry-run

echo "Dry run"
before=$(snapshot)
out=$(PATH="$stub:$PATH" bash "$script" --dry-run --user "$user" --user lid-check-missing 2>&1)
status=$?
expect "exits 0" test "$status" -eq 0
expect "says nothing will be written" says "dry run: nothing will be written" "$out"
expect "would write the logind drop-in" says "[would] write $dropin" "$out"
expect "and the user's KDE profile" says "[would] $cfg/powerdevilrc: [AC][SuspendAndShutdown] LidAction=0" "$out"
expect "and their LXQt setting" says "[would] $cfg/lxqt/lxqt-powermanagement.conf: [General] lidWatcherEnabled=false" "$out"
expect "and their xfconf properties" says "[would] $user: xfconf /xfce4-power-manager/lid-action-on-ac -> 0" "$out"
expect "and their GNOME keys" says "[would] $user: $power lid-close-ac-action -> 'nothing'" "$out"
expect "skips a user that does not exist" says "user lid-check-missing not found; skipping" "$out"
expect "and changes nothing" test "$(snapshot)" = "$before"
expect "not even through the desktop tools" test -z "$(ls -A "$state")"

echo "Running"
out=$(PATH="$stub:$PATH" bash "$script" --user "$user" 2>&1)
status=$?
expect "exits 0" test "$status" -eq 0
expect "and reports all changes applied" says "all changes applied" "$out"
expect "the logind drop-in is written" holds "$dropin" \
    '# Installed by lid-ignore.sh: this machine runs with the lid closed.\n[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n'
expect "where logind reads it" bash -c 'systemd-analyze cat-config systemd/logind.conf 2>/dev/null | grep -qx HandleLidSwitch=ignore'
expect "and logind is restarted" says "systemd-logind restarted" "$out"
expect "and still running" systemctl is-active --quiet systemd-logind
if [[ -f $backup/_etc_UPower_UPower.conf ]]; then
    expect "UPower is told to ignore the lid" grep -qx 'IgnoreLid=true' /etc/UPower/UPower.conf
else
    echo "skip  UPower, which is not installed here"
fi
if [[ -f $backup/_etc_default_acpi-support ]]; then
    expect "acpi-support is told not to sleep on the lid" grep -qx 'LID_SLEEP=false' /etc/default/acpi-support
else
    echo "skip  acpi-support, which is not installed here"
fi
expect "the user has no session bus, so each tool gets a private one" \
    says "no session bus for $user; running each desktop tool on a private bus" "$out"
expect "and the tools did run on one" grep -q '^unix:' "$state/bus"
expect "the KDE profiles all get LidAction=0, the one that was there edited in place" holds "$cfg/powerdevilrc" \
    '[AC][SuspendAndShutdown]\nLidAction=0\nPowerButtonAction=16\n\n[Battery][SuspendAndShutdown]\nLidAction=0\n\n[LowBattery][SuspendAndShutdown]\nLidAction=0\n'
expect "the LXQt setting is written" holds "$cfg/lxqt/lxqt-powermanagement.conf" '[General]\nlidWatcherEnabled=false\n'
expect "and that new file belongs to the user" test "$(stat -c %U "$cfg/lxqt/lxqt-powermanagement.conf")" = "$user"
expect "the xfconf lid action on AC is set to 0" holds "$state/xfconf.xfce4-power-manager.lid-action-on-ac" '0\n'
expect "and on battery" holds "$state/xfconf.xfce4-power-manager.lid-action-on-battery" '0\n'
expect "and xfce is told to leave the lid to logind" holds "$state/xfconf.xfce4-power-manager.logind-handle-lid-switch" 'false\n'
expect "the GNOME lid action on AC is set to nothing" holds "$state/gsettings.$power.lid-close-ac-action" "'nothing'\n"
expect "and on battery" holds "$state/gsettings.$power.lid-close-battery-action" "'nothing'\n"
expect "the desktop tools ran as the user, not as root" holds "$state/ran-as" "$user\n"
expect "other settings that mention the lid are listed for review" says "org.example.lid close-action 'suspend'" "$out"
expect "and the ones its filter rules out are not" lacks "valid-closes" "$out"

echo "Running again"
cp -p "$dropin" "$tmp/dropin.first"
out=$(PATH="$stub:$PATH" bash "$script" --user "$user" 2>&1)
status=$?
expect "exits 0" test "$status" -eq 0
expect "and sets nothing" lacks "[ set ]" "$out"
expect "finding the drop-in already correct" says "$dropin already correct" "$out"
expect "and the KDE profile" says "[LowBattery][SuspendAndShutdown] LidAction=0 already set" "$out"
expect "and the xfconf properties" says "xfconf /xfce4-power-manager/lid-action-on-ac already 0" "$out"
expect "and the GNOME keys" says "$power lid-close-ac-action already 'nothing'" "$out"
expect "and the drop-in is byte for byte the same" cmp -s "$dropin" "$tmp/dropin.first"

echo
if [[ $failures -eq 0 ]]; then
    echo "All checks passed."
else
    echo "$failures failed."
    exit 1
fi
