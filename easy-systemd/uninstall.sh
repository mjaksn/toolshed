#!/usr/bin/env bash
# Stops and removes services that install.sh created, and nothing else. See
# README.md, or run with -h.
set -euo pipefail

# The line install.sh writes into every unit it creates. It must match the
# copy in install.sh exactly.
MARKER='# Created by easy-systemd install.sh; uninstall.sh removes only units carrying this line.'
UNIT_DIR=/etc/systemd/system

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh NAME...
       sudo ./uninstall.sh --all
       ./uninstall.sh

Stops, disables and deletes each named service. A unit that install.sh did
not create is refused, and nothing is removed if any NAME is refused.
--all removes every unit install.sh created. With no arguments, lists them.
EOF
}

die() {
    echo "uninstall.sh: $*" >&2
    exit 1
}

# Prints the name of every unit carrying the marker, one per line.
managed() {
    local path
    for path in "$UNIT_DIR"/*.service; do
        if [[ -f $path ]] && grep -qxF -- "$MARKER" "$path" 2>/dev/null; then
            basename -- "$path" .service
        fi
    done
}

case ${1:-} in
    -h|--help)
        usage
        exit 0
        ;;
    '')
        mapfile -t names < <(managed)
        if [[ ${#names[@]} -gt 0 ]]; then
            echo "Units created by install.sh:"
            printf '  %s\n' "${names[@]}"
        else
            echo "No units created by install.sh are installed."
        fi
        exit 0
        ;;
    --all)
        [[ $# -eq 1 ]] || die "--all takes no names"
        mapfile -t names < <(managed)
        if [[ ${#names[@]} -eq 0 ]]; then
            echo "No units created by install.sh are installed."
            exit 0
        fi
        ;;
    -*)
        die "unknown option $1 (see -h)"
        ;;
    *)
        names=("${@%.service}")
        ;;
esac

[[ $EUID -eq 0 ]] || die "must be run as root, for example with sudo"

# Check every name before removing any, so a typo in the middle of a list
# does not leave the job half done.
for name in "${names[@]}"; do
    path=$UNIT_DIR/$name.service
    [[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die "not a valid NAME: $name"
    [[ -e $path ]] || die "no such unit: $path"
    grep -qxF -- "$MARKER" "$path" 2>/dev/null \
        || die "$path was not created by install.sh; leaving it alone"
done

for name in "${names[@]}"; do
    systemctl disable --now --quiet -- "$name.service"
    rm -f -- "$UNIT_DIR/$name.service"
    echo "Removed $name"
done
systemctl daemon-reload
for name in "${names[@]}"; do
    systemctl reset-failed -- "$name.service" 2>/dev/null || true
done
