#!/usr/bin/env bash
#
# retention.sh
#
# Prunes, compacts and checks every client repository held by a borgserver
# container, run on the machine the container runs on. Clients push in
# append-only mode, so nothing a client sends can delete history. Borg run
# inside the container works on the repositories as local files, bypasses
# borg serve and its append-only flag, and is the one place deletion happens.
#
# Run as root on the server:  ./retention.sh [--env FILE] [--dry-run]
#
# A client is pruned only when RETENTION_PASSDIR holds <client>.pass with its
# repository passphrase; a repository without one is reported and left alone.
# Each setting is taken from the environment, else the settings file (--env
# FILE, else borg-docker.env beside this script), else the default.
#
set -uo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./retention.sh [--env FILE] [--dry-run]

Prunes, compacts and checks every client repository in a borgserver
container. Run it as root on the machine the container runs on.

  --env FILE   read settings from FILE instead of borg-docker.env beside
               this script
  --dry-run    list what prune would delete, and delete nothing
  -h, --help   print this help
EOF
}

SETTINGS_FILE=""
explicit_settings=0
dry_run=0
while [ $# -gt 0 ]; do
    case "$1" in
        --env) [ $# -ge 2 ] || { usage >&2; exit 2; }; SETTINGS_FILE="$2"; explicit_settings=1; shift 2 ;;
        --env=*) SETTINGS_FILE="${1#--env=}"; explicit_settings=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

if [ "$explicit_settings" -eq 1 ]; then
    [ -f "$SETTINGS_FILE" ] || die "Settings file '$SETTINGS_FILE' does not exist."
else
    SETTINGS_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/borg-docker.env"
    [ -f "$SETTINGS_FILE" ] || SETTINGS_FILE=""
fi

# Print VAR as FILE sets it, or nothing. The file is sourced in a separate
# shell, so nothing it does reaches this one, and with carriage returns
# removed, since a copy made on Windows or kept in a password manager may
# have gained them.
env_get() {
    [ -n "$1" ] && [ -f "$1" ] || return 0
    bash -c 'set -a; . <(tr -d "\r" <"$1") >/dev/null 2>&1; printf "%s" "${!2-}"' _ "$1" "$2"
}

# Set VAR from the environment, else the settings file, else the default.
setting() {
    local v="${!1-}"
    [ -n "$v" ] || v="$(env_get "$SETTINGS_FILE" "$1")"
    [ -n "$v" ] || v="$2"
    printf -v "$1" '%s' "$v"
}

setting BORGSERVER_CONTAINER borgserver
setting RETENTION_PASSDIR ""
setting RETENTION_KEEP "--keep-daily 14 --keep-weekly 8 --keep-monthly 6"
setting REPO_NAME containers

[ "$(id -u)" -eq 0 ] || die "Run as root."
[ -n "$RETENTION_PASSDIR" ] || die "Set RETENTION_PASSDIR to the directory holding <client>.pass files."
[ -d "$RETENTION_PASSDIR" ] || die "RETENTION_PASSDIR '$RETENTION_PASSDIR' does not exist."
container="$BORGSERVER_CONTAINER"

if ! docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -q true; then
    die "$container is not running."
fi

# The host directory mounted at /backup in the container holds one
# directory per client.
repo_base="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/backup"}}{{println .Source}}{{end}}{{end}}' "$container" | head -n1)"
[ -n "$repo_base" ] && [ -d "$repo_base" ] || die "Cannot find the host directory mounted at /backup in $container."

read -r -a keep_args <<<"$RETENTION_KEEP"
if [ "$dry_run" -eq 1 ]; then
    prune_args=(--dry-run --list)
    echo "Dry run: nothing will be deleted or compacted."
else
    prune_args=(--list --stats)
fi

rc=0

# Repositories nobody will prune, said once so they are not forgotten.
for dir in "$repo_base"/*/; do
    client="$(basename "$dir")"
    if [ -d "${dir}${REPO_NAME}" ] && [ ! -f "${RETENTION_PASSDIR}/${client}.pass" ]; then
        echo "== ${client}: no ${client}.pass in ${RETENTION_PASSDIR}; never pruned"
    fi
done

shopt -s nullglob
passfiles=("$RETENTION_PASSDIR"/*.pass)
shopt -u nullglob
[ "${#passfiles[@]}" -gt 0 ] || echo "No passphrase files in $RETENTION_PASSDIR"

for passfile in "${passfiles[@]}"; do
    client="$(basename "$passfile" .pass)"
    host_repo="${repo_base}/${client}/${REPO_NAME}"
    repo="/backup/${client}/${REPO_NAME}"          # as the container sees it
    if [ ! -d "$host_repo" ]; then
        echo "== ${client}: no repo yet, skipping"
        continue
    fi
    echo "== ${client}"

    # Refuse to touch a repository a client is writing to.
    if [ -e "${host_repo}/lock.exclusive" ]; then
        echo "   repo is locked (backup in progress?); skipping this run"
        continue
    fi

    # The passphrase reaches borg through docker's own environment, so it
    # never appears on a command line in the process list.
    BORG_PASSPHRASE="$(cat "$passfile")" docker exec -u borg \
        -e BORG_PASSPHRASE \
        -e BORG_RELOCATED_REPO_ACCESS_IS_OK=yes \
        "$container" borg prune "${prune_args[@]}" "${keep_args[@]}" \
        --glob-archives "${client}-*" "$repo" || rc=1

    if [ "$dry_run" -eq 0 ]; then
        docker exec -u borg "$container" borg compact "$repo" || rc=1
    fi

    docker exec -u borg "$container" borg check --repository-only "$repo" || rc=1

    echo "   on disk: $(du -sh "$host_repo" | cut -f1)"
done
exit $rc
