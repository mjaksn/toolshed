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
# Run as root on the server:  ./retention.sh [--env FILE] [--dry-run] [--record]
#
# A client is pruned only when RETENTION_PASSDIR holds <client>.pass with its
# repository passphrase; a repository without one is reported and left alone.
# Each run records the client's archives, by ID and name, beside it in
# <client>.archives, and a client with an archive gone since then that this
# script did not prune is reported and left alone too, since compacting would
# make the loss final.
# Each setting is taken from the environment, else the settings file (--env
# FILE, else borg-docker.env beside this script), else the default.
#
set -uo pipefail

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: ./retention.sh [--env FILE] [--dry-run] [--record]

Prunes, compacts and checks every client repository in a borgserver
container. Run it as root on the machine the container runs on.

  --env FILE   read settings from FILE instead of borg-docker.env beside
               this script
  --dry-run    list what prune would delete, and delete nothing
  --record     only check each client's archives against its record and
               update the record; prune, compact and shut out nothing
  -h, --help   print this help
EOF
}

SETTINGS_FILE=""
explicit_settings=0
dry_run=0
record_only=0
while [ $# -gt 0 ]; do
    case "$1" in
        --env) [ $# -ge 2 ] || { usage >&2; exit 2; }; SETTINGS_FILE="$2"; explicit_settings=1; shift 2 ;;
        --env=*) SETTINGS_FILE="${1#--env=}"; explicit_settings=1; shift ;;
        --dry-run) dry_run=1; shift ;;
        --record) record_only=1; shift ;;
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

# Load the settings file once in a shell that stops at the first command
# that fails. A value that lost its quotes runs part of itself as a command
# and leaves its setting unset, which env_get alone would quietly read as "use
# the default", and the default keep rules may keep less than yours.
if [ -n "$SETTINGS_FILE" ]; then
    bash -c 'set -e; set -a; . <(tr -d "\r" <"$1") >/dev/null' _ "$SETTINGS_FILE" \
        || die "'$SETTINGS_FILE' did not load cleanly, at the error above. Correct it and run again."
fi

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

# One run at a time. Two would share the saved key list and the records, and
# could each put back a list the other had changed.
command -v flock >/dev/null || die "flock is needed, from util-linux."
exec 9>"${RETENTION_PASSDIR}/.retention.lock"
flock -n 9 || die "Another retention.sh is running against ${RETENTION_PASSDIR}; try again when it has finished."
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
if [ "$record_only" -eq 1 ]; then
    echo "Record run: archives are checked against the records, and nothing is pruned or compacted."
fi

rc=0

# Borg inside the container with the current client's passphrase, which
# reaches it through docker's own environment, so it never appears on a
# command line in the process list.
borg_in() {
    BORG_PASSPHRASE="$pass" docker exec -u borg \
        -e BORG_PASSPHRASE \
        -e BORG_RELOCATED_REPO_ACCESS_IS_OK=yes \
        "$container" borg "$@"
}

# The current client's archives, one per line as its ID and then its name.
# The ID is what the record compares: a client can delete an archive and
# create another under the same name, but not with the same ID.
archives() {
    borg_in list --format '{id} {archive}{NL}' "$repo"
}

# The IDs alone, from such a list on stdin, sorted for comm.
ids() {
    cut -d' ' -f1 | sed '/^$/d' | LC_ALL=C sort
}

# Replace the current client's record with the given list. It is written
# beside the record and moved over it, so a failed write, say on a full disk,
# leaves the old record whole rather than a short one that would hide the
# next deletion.
save_record() {
    local tmp
    if tmp="$(mktemp "${record}.XXXXXX")" \
        && printf '%s\n' "$1" > "$tmp" \
        && mv -f "$tmp" "$record"; then
        return 0
    fi
    echo "   could not write ${record}; the old record is unchanged"
    rm -f "${tmp:-}"
    return 1
}

# While a client's repository is being maintained, that client is shut out,
# so it cannot delete an archive between the checks below and compact. Its
# line comes out of the authorized_keys the container builds from the client
# key files, sshd reads that file at every login, and the line goes back once
# the client is done, or on any exit. Restarting the container rebuilds the
# file from the key files too, so a client is never shut out for good.
keys=/home/borg/.ssh/authorized_keys
suspended=""

resume_client() {
    [ -n "$suspended" ] || return 0
    # Copied beside the live file and moved over it, so the live file is
    # never half written, and the saved copy is kept until the move is done.
    if docker exec "$container" sh -c \
        'cp -p "$1.retention" "$1.restore" && mv -f "$1.restore" "$1" && rm -f "$1.retention"' _ "$keys"; then
        suspended=""
        return 0
    fi
    echo "   could not put ${suspended}'s key back; restart ${container} to rebuild it" >&2
    return 1
}

# Shut the current client out, and make sure it has no session still open.
# Fails, with the reason said, when the client should be skipped this run.
suspend_client() {
    local line n
    [ "$dry_run" -eq 0 ] && [ "$record_only" -eq 0 ] || return 0
    # The saved copy is the only way back for a client that is out, so
    # nothing may overwrite it: not the next client after a key that would
    # not go back, and not this run after one that was interrupted.
    if [ -n "$suspended" ]; then
        echo "   ${suspended}'s key could not be put back, so no other client is touched this run"
        rc=1
        return 1
    fi
    if docker exec "$container" test -e "${keys}.retention"; then
        echo "   ${keys}.retention is left from an interrupted run; skipping."
        echo "   Restart ${container}, which rebuilds ${keys}, then delete that file."
        rc=1
        return 1
    fi
    line="command=\"cd /backup/${client};"
    n="$(docker exec "$container" grep -c -F "$line" "$keys" 2>/dev/null)" || n=0
    if [ "$n" != 1 ]; then
        echo "   found ${n} key lines for ${client} in ${keys}, not 1; skipping this run."
        echo "   If an earlier run was interrupted, restart ${container} to rebuild them."
        rc=1
        return 1
    fi
    # The original is saved first, and the client counts as shut out from
    # then on, so that any failure after it, or any exit, puts that copy
    # back. The filtered list is written beside the live file and moved over
    # it, so the live file is never half written.
    if ! docker exec "$container" cp -p "$keys" "${keys}.retention"; then
        echo "   could not save ${keys}; skipping this run"
        rc=1
        return 1
    fi
    suspended="$client"
    if ! docker exec "$container" sh -c \
        'cp -p "$1" "$1.new" && { grep -v -F "$2" "$1.retention"; [ $? -le 1 ]; } > "$1.new" && mv -f "$1.new" "$1"' \
        _ "$keys" "$line"; then
        echo "   could not take ${client}'s key out; skipping this run"
        docker exec "$container" rm -f "${keys}.new" || true
        resume_client || true
        rc=1
        return 1
    fi
    # The whole listing is taken before it is searched: grep -q stopping at
    # the first match could otherwise break the pipe and read as no match.
    if ! procs="$(docker exec "$container" sh -c 'for p in /proc/[0-9]*; do tr "\0" " " < "$p/cmdline" 2>/dev/null; echo; done')"; then
        echo "   could not list the processes in ${container}; skipping this run"
        resume_client || true
        rc=1
        return 1
    fi
    # Taking the key out stops new logins, not a connection already made,
    # and a multiplexed connection can sit idle with no borg serve to show
    # for it until it opens a channel. Which client a connection belongs to
    # cannot be told from here, so any session of the borg user at all, or
    # any borg serve, means skipping this client. Retention runs when the
    # server should be quiet, so normally there is none.
    if grep -q -E -e '^sshd(-session)?: borg([@ ]|$)' -e 'borg serve' <<<"$procs"; then
        echo "   an SSH session to ${container} is open (a backup in progress?); skipping ${client} this run"
        resume_client || rc=1
        return 1
    fi
}

on_exit() {
    local status=$?
    resume_client || status=1
    exit "$status"
}
trap on_exit EXIT
# Turned into ordinary exits, so that the EXIT trap runs for them too.
trap 'exit 143' TERM
trap 'exit 130' INT

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
    suspend_client || continue

    pass="$(cat "$passfile")"
    record="${RETENTION_PASSDIR}/${client}.archives"
    if ! now="$(archives)"; then
        echo "   could not list the archives; skipping this run"
        rc=1
        resume_client || rc=1
        continue
    fi

    # Append-only lets a client remove archives from the manifest but never
    # from the disk, and compacting is what would make that permanent. So an
    # archive that has gone since the last run, without this script pruning
    # it, stops everything for this client until someone has looked.
    if [ -f "$record" ]; then
        gone="$(comm -23 <(LC_ALL=C sort "$record") <(LC_ALL=C sort <<<"$now"))"
        if [ -n "$gone" ]; then
            echo "   archives gone since the last run, which this script did not prune:"
            sed 's/^/     /' <<<"$gone"
            echo "   A client may have deleted them. Nothing is pruned or compacted, so"
            echo "   they can still be recovered; see Retention in the README. If you"
            echo "   deleted them yourself, remove ${record} and run again."
            rc=1
            resume_client || rc=1
            continue
        fi
    else
        echo "   no record of its archives yet; this run starts one"
    fi

    # A record run stops here, having found nothing gone: it brings the
    # record up to date, so that an archive made since the last run is
    # watched from now on, and changes nothing in the repository.
    if [ "$record_only" -eq 1 ]; then
        if [ "$dry_run" -eq 0 ]; then
            save_record "$now" || rc=1
            echo "   recorded $(grep -c . <<<"$now") archives"
        fi
        continue
    fi

    prune_ok=1
    prune_out="$(borg_in prune "${prune_args[@]}" "${keep_args[@]}" \
        --glob-archives "${client}-*" "$repo" 2>&1)" || { prune_ok=0; rc=1; }
    printf '%s\n' "$prune_out"

    # The client is shut out, so nothing should change between the check
    # above and compact below. The archives are still listed again before
    # compacting, and anything gone that prune did not report pruning stops
    # it, and again after, in case shutting the client out ever fails to
    # hold.
    if [ "$dry_run" -eq 0 ]; then
        pruned="$(grep '^Pruning archive' <<<"$prune_out" | grep -o '\[[0-9a-f]\{64\}\]' | tr -d '[]' | LC_ALL=C sort || true)"
        if ! after="$(archives)"; then
            echo "   could not list the archives after pruning; nothing is compacted and the record is unchanged"
            rc=1
        else
            unexpected="$(comm -23 <(ids <<<"$now") <(ids <<<"$after") | comm -23 - <(printf '%s\n' "$pruned"))"
            if [ -n "$unexpected" ]; then
                echo "   archives gone during this run that prune did not remove:"
                grep -F -f <(printf '%s\n' "$unexpected") <<<"$now" | sed 's/^/     /'
                echo "   A client may have deleted them. Nothing is compacted, and the record"
                echo "   still lists them, so the next run reports them too."
                # Recorded as before, less what prune did report, so that the
                # next run names only these.
                keep="$now"
                if [ -n "$pruned" ]; then
                    keep="$(grep -v -F -f <(printf '%s\n' "$pruned") <<<"$now" || true)"
                fi
                save_record "$keep" || rc=1
                rc=1
            elif [ "$prune_ok" -eq 0 ]; then
                echo "   prune failed, so nothing is compacted"
                save_record "$after" || rc=1
            else
                docker exec -u borg "$container" borg compact "$repo" || rc=1
                if ! final="$(archives)"; then
                    echo "   could not list the archives after compacting; recording the list from before"
                    save_record "$after" || rc=1
                    rc=1
                elif lost="$(comm -23 <(ids <<<"$after") <(ids <<<"$final"))"; [ -n "$lost" ]; then
                    echo "   archives gone just before compacting, whose data may now be lost:"
                    grep -F -f <(printf '%s\n' "$lost") <<<"$after" | sed 's/^/     /'
                    echo "   The record still lists them, so the next run reports them too."
                    save_record "$after" || rc=1
                    rc=1
                else
                    save_record "$final" || rc=1
                fi
            fi
        fi
    fi

    resume_client || rc=1

    docker exec -u borg "$container" borg check --repository-only "$repo" || rc=1

    echo "   on disk: $(du -sh "$host_repo" | cut -f1)"
done
exit $rc
