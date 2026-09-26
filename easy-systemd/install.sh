#!/usr/bin/env bash
# Installs a command as a systemd service that starts at boot and is restarted
# when it stops. See README.md, or run with -h.
set -euo pipefail

# The line uninstall.sh looks for before it will touch a unit. It must match
# the copy in uninstall.sh exactly.
MARKER='# Created by easy-systemd install.sh; uninstall.sh removes only units carrying this line.'
UNIT_DIR=/etc/systemd/system

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [options] NAME -- COMMAND [ARG...]

Installs COMMAND as the systemd service NAME, starts it now, and starts it
again at every boot. Running it again with the same NAME replaces the
service with the new settings and restarts it.

Options:
  -d DIR      working directory (default: the current directory)
  -u USER     user to run as (default: the user who ran sudo, else root)
  -r POLICY   when to restart: always, on-failure or no (default: always);
              the other values systemd takes for Restart= are accepted too
  -s SECONDS  wait before restarting (default: 5)
  -e KEY=VAL  set an environment variable; repeat for more
  -h          show this help

A relative COMMAND path is taken from the working directory, and a bare
name is looked up on root's PATH. Arguments are passed exactly as given,
with no shell in between; for pipes or redirection, run a shell:
  sudo ./install.sh myjob -- /bin/sh -c 'mytool | logger'
EOF
}

die() {
    echo "install.sh: $*" >&2
    exit 1
}

# Escapes a value for double quotes in a unit file. Inside them systemd still
# undoes C escapes and expands %-specifiers, so both are escaped to arrive as
# written.
escape() {
    local s=$1
    s=${s//'\'/'\\'}
    s=${s//'"'/'\"'}
    printf '%s' "${s//'%'/'%%'}"
}

# Quotes one argument for ExecStart=, which also expands $VARIABLES. A lone
# semicolon separates commands unless escaped, and quoting alone does not stop
# that.
quote_arg() {
    if [[ $1 == ';' ]]; then
        printf '%s' '\;'
        return
    fi
    local s
    s=$(escape "$1")
    printf '"%s"' "${s//'$'/'$$'}"
}

workdir=$PWD
user=${SUDO_USER:-root}
restart=always
restart_sec=5
envs=()

while getopts ':d:u:r:s:e:h' opt; do
    case $opt in
        d) workdir=$OPTARG ;;
        u) user=$OPTARG ;;
        r) restart=$OPTARG ;;
        s) restart_sec=$OPTARG ;;
        e) envs+=("$OPTARG") ;;
        h) usage; exit 0 ;;
        :) die "-$OPTARG needs a value" ;;
        *) die "unknown option -$OPTARG (see -h)" ;;
    esac
done
shift $((OPTIND - 1))

[[ $# -ge 1 ]] || { usage >&2; exit 1; }
name=${1%.service}
shift
[[ ${1:-} == '--' ]] && shift
[[ $# -ge 1 ]] || die "no command given after NAME (see -h)"

[[ $EUID -eq 0 ]] || die "must be run as root, for example with sudo"
[[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] \
    || die "NAME may hold only letters, digits, '_', '.' and '-': $name"
case $restart in
    no|always|on-success|on-failure|on-abnormal|on-abort|on-watchdog) ;;
    *) die "unknown restart policy: $restart" ;;
esac
[[ $restart_sec =~ ^[0-9]+$ ]] || die "-s takes a whole number of seconds: $restart_sec"
id -u -- "$user" >/dev/null 2>&1 || die "no such user: $user"
for env in "${envs[@]}"; do
    [[ $env == *=* && ${env%%=*} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        || die "-e takes KEY=VALUE, the KEY made of letters, digits and '_': $env"
done

[[ -d $workdir ]] || die "no such directory: $workdir"
workdir=$(cd -- "$workdir" && pwd)

exe=$1
shift
if [[ $exe != */* ]]; then
    found=$(type -P -- "$exe") || die "not found on PATH: $exe"
    exe=$found
elif [[ $exe != /* ]]; then
    exe=$workdir/$exe
fi
[[ -f $exe && -x $exe ]] || die "not an executable file: $exe"
exe=$(cd -- "$(dirname -- "$exe")" && pwd)/$(basename -- "$exe")

for arg in "$exe" "$@" "$workdir" "${envs[@]}"; do
    [[ $arg != *$'\n'* ]] || die "a line break cannot be written into a unit file"
done

unit=$name.service
path=$UNIT_DIR/$unit
if [[ -e $path || -L $path ]]; then
    grep -qxF -- "$MARKER" "$path" 2>/dev/null \
        || die "$path exists and was not created by install.sh; leaving it alone"
elif systemctl cat -- "$unit" >/dev/null 2>&1; then
    die "a unit called $unit already exists elsewhere; choose another NAME"
fi

exec_start=$(quote_arg "$exe")
for arg in "$@"; do
    exec_start+=" $(quote_arg "$arg")"
done
environment=
for env in "${envs[@]}"; do
    environment+="Environment=\"$(escape "$env")\""$'\n'
done

cat >"$path" <<EOF
$MARKER
[Unit]
Description=$name (easy-systemd)
Wants=network-online.target
After=network-online.target
# Never stop restarting it, however often it fails.
StartLimitIntervalSec=0

[Service]
User=$user
WorkingDirectory=${workdir//'%'/'%%'}
${environment}ExecStart=$exec_start
Restart=$restart
RestartSec=$restart_sec

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --quiet -- "$unit"
systemctl restart -- "$unit"

printf -v shown '%q ' "$exe" "$@"
cat <<EOF
Installed $path and started it. It will start at every boot.
  runs:     ${shown% }
  as user:  $user
  in:       $workdir
  restart:  $restart, after ${restart_sec}s
EOF
[[ ${#envs[@]} -eq 0 ]] || echo "  env:      ${envs[*]%%=*}"
cat <<EOF

  systemctl status $name        how it is doing
  journalctl -u $name -f        follow its output
  sudo ./uninstall.sh $name     stop and remove it
EOF
