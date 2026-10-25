#!/usr/bin/env bash
#
# setup.sh
#
# Prepares this Docker host to push nightly Borg backups of its compose
# projects, its named volumes and whichever bind mounts you choose to a Borg
# server reached over SSH. Safe to re-run: each step detects what is already
# in place, previous choices are carried forward from /etc/borg-backup.env,
# and a hand-edited backup script is never overwritten without asking.
#
# What it does:
#   1. Installs borgbackup from apt (asks first)
#   2. Works out what to back up (projects, volumes, uncovered bind mounts)
#   3. Generates a root SSH key for borg and waits while you add it on the
#      server (skipped if the server already accepts it)
#   4. Writes an SSH config alias and verifies the connection
#   5. Generates a repo passphrase and initialises the repository (or, on a
#      rebuilt host, takes the saved passphrase so the existing repo is reused)
#   6. Installs /usr/local/sbin/borg-backup.sh (stop containers, borg create,
#      start them again)
#   7. Installs a systemd service and a daily timer
#   8. Optionally runs a backup now
#
# Run as root:  sudo ./setup.sh [--env FILE]
#
# Each setting is taken from the first of these that gives it a value: the
# environment, the settings file (--env FILE, else borg-docker.env beside
# this script), the previous run's /etc/borg-backup.env, then the default.
# The README lists every setting.
#
set -euo pipefail
# Paths are split on spaces below and never meant as patterns.
set -o noglob

KEY="/root/.ssh/borg_ed25519"
SSH_CONFIG="/root/.ssh/config"
PASSFILE="/root/.config/borg/passphrase"
BACKUP_SCRIPT="/usr/local/sbin/borg-backup.sh"
ENV_FILE="/etc/borg-backup.env"

step() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { local a; read -r -p "$1 [y/N] " a; [[ "${a,,}" == "y" ]]; }

usage() {
    cat <<'EOF'
Usage: sudo ./setup.sh [--env FILE]

Sets this Docker host up to push nightly Borg backups to a Borg server.

  --env FILE   read settings from FILE instead of borg-docker.env beside
               this script
  -h, --help   print this help
EOF
}

SETTINGS_FILE=""
explicit_settings=0
while [ $# -gt 0 ]; do
    case "$1" in
        --env) [ $# -ge 2 ] || { usage >&2; exit 2; }; SETTINGS_FILE="$2"; explicit_settings=1; shift 2 ;;
        --env=*) SETTINGS_FILE="${1#--env=}"; explicit_settings=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

# Never exit silently: report what failed and where.
trap 'printf "\033[1;31mERROR:\033[0m line %s: %s (exit %s)\n" "$LINENO" "$BASH_COMMAND" "$?" >&2' ERR

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
command -v docker >/dev/null || die "docker not found on this host."
docker info >/dev/null 2>&1 || die "docker is installed but its daemon is not answering."

if [ "$explicit_settings" -eq 1 ]; then
    [ -f "$SETTINGS_FILE" ] || die "Settings file '$SETTINGS_FILE' does not exist."
else
    SETTINGS_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/borg-docker.env"
    [ -f "$SETTINGS_FILE" ] || SETTINGS_FILE=""
fi
PREV_FILE=""
if [ -f "$ENV_FILE" ]; then PREV_FILE="$ENV_FILE"; fi

# ---------------------------------------------------------------------------
# Settings.
# ---------------------------------------------------------------------------
# Print VAR as FILE sets it, or nothing. The file is sourced in a separate
# shell, so nothing it does reaches this one, and with carriage returns
# removed, since a copy made on Windows or kept in a password manager may
# have gained them.
env_get() {
    [ -n "$1" ] && [ -f "$1" ] || return 0
    bash -c 'set -a; . <(tr -d "\r" <"$1") >/dev/null 2>&1; printf "%s" "${!2-}"' _ "$1" "$2"
}

# Load FILE once in a shell that stops at the first command that fails. A
# value that lost its quotes runs part of itself as a command and leaves its
# setting unset, which env_get alone would quietly read as "use the default".
check_settings() {
    [ -n "$1" ] || return 0
    bash -c 'set -e; set -a; . <(tr -d "\r" <"$1") >/dev/null' _ "$1" \
        || die "'$1' did not load cleanly, at the error above. Correct it and run again."
}

# Set VAR from the first source that gives it a value. An empty value counts
# as no value, so it falls through to the next source.
setting() {
    local v="${!1-}"
    [ -n "$v" ] || v="$(env_get "$SETTINGS_FILE" "$1")"
    [ -n "$v" ] || v="$(env_get "$PREV_FILE" "$1")"
    [ -n "$v" ] || v="$2"
    printf -v "$1" '%s' "$v"
}

# Quote a value for the env file, which is sourced by bash.
envq() {
    local v="$1"
    v="${v//\\/\\\\}"; v="${v//\"/\\\"}"; v="${v//\$/\\\$}"; v="${v//\`/\\\`}"
    printf '"%s"' "$v"
}

check_settings "$SETTINGS_FILE"
check_settings "$PREV_FILE"
if [ -n "$SETTINGS_FILE" ]; then echo "Reading settings from ${SETTINGS_FILE}"; fi
if [ -n "$PREV_FILE" ]; then echo "Previous configuration found in ${PREV_FILE}; using it where nothing else is set."; fi

# Hosts set up before SSH_ALIAS was recorded have it only inside BORG_REPO.
prev_repo="$(env_get "$PREV_FILE" BORG_REPO)"
prev_alias=""
if [[ "$prev_repo" =~ ^ssh://([^/]+)/ ]]; then prev_alias="${BASH_REMATCH[1]}"; fi

setting SERVER_HOST ""
setting SERVER_PORT 22
setting SERVER_USER borg
setting SSH_ALIAS "${prev_alias:-borg-server}"
setting CLIENT_NAME "$(hostname -s)"
setting REPO_NAME containers
setting BACKUP_TIME 03:30
setting SERVER_KEYS_DIR "<the borgserver sshkeys directory>/clients"
setting SERVER_KEY_OWNER ""
setting BORGSERVER_CONTAINER borgserver

# REPO_PATH is derived unless it was given explicitly, so that a change of
# client or repository name is never shadowed by the previous run's path.
setting REPO_PATH ""
explicit_repo_path="$REPO_PATH"
[ -n "$REPO_PATH" ] || REPO_PATH="/backup/${CLIENT_NAME}/${REPO_NAME}"

if [ -z "$SERVER_HOST" ]; then
    read -r -p "Borg server address (host name or IP): " SERVER_HOST
fi
[ -n "$SERVER_HOST" ] || die "No server address. Set SERVER_HOST."
[[ "$SERVER_PORT" =~ ^[0-9]+$ ]] || die "SERVER_PORT '$SERVER_PORT' is not a port number."
[[ "$BACKUP_TIME" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ ]] || die "BACKUP_TIME '$BACKUP_TIME' is not HH:MM."
[[ "$CLIENT_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || die "CLIENT_NAME '$CLIENT_NAME' may hold only letters, digits, '.', '_' and '-'."
[[ "$SSH_ALIAS" =~ ^[A-Za-z0-9._-]+$ ]] || die "SSH_ALIAS '$SSH_ALIAS' may hold only letters, digits, '.', '_' and '-'."
[[ "$REPO_PATH" == /* ]] || die "REPO_PATH '$REPO_PATH' must be absolute."

REPO="ssh://${SSH_ALIAS}${REPO_PATH}"
relocated=0
if [ -n "$prev_repo" ] && [ "$prev_repo" != "$REPO" ]; then
    warn "This host backs up to ${prev_repo},"
    warn "but these settings point at ${REPO}."
    ask "Switch this host to the new repository?" || die "Nothing changed. Adjust the settings and run again."
    relocated=1
fi

echo "Server:     ${SERVER_USER}@${SERVER_HOST}:${SERVER_PORT} (alias ${SSH_ALIAS})"
echo "Client:     ${CLIENT_NAME}"
echo "Repository: ${REPO}"

# ---------------------------------------------------------------------------
step "1/8  borgbackup package"
# ---------------------------------------------------------------------------
if command -v borg >/dev/null; then
    echo "borg already installed: $(borg --version)"
else
    echo "This will run: apt-get install -y borgbackup"
    ask "Install borgbackup now?" || die "borgbackup is required. Aborting."
    apt-get update -qq
    apt-get install -y borgbackup
    echo "Installed: $(borg --version)"
fi

borg_major="$(borg --version | awk '{print $2}' | cut -d. -f1)"
[ "$borg_major" = "1" ] || die "This is borg major version ${borg_major}. This tool is written for Borg 1.x; Borg 2 has a different repository format and commands."

# ---------------------------------------------------------------------------
step "2/8  What to back up"
# ---------------------------------------------------------------------------
setting VOLUMES_DIR "$(docker info -f '{{.DockerRootDir}}')/volumes"
VOLUMES_DIR="${VOLUMES_DIR%/}"
[ -d "$VOLUMES_DIR" ] || die "Volumes directory '$VOLUMES_DIR' does not exist. Set VOLUMES_DIR."

setting PROJECTS_DIR ""
if [ -z "$PROJECTS_DIR" ]; then
    # Arcane keeps its compose projects in one directory, named by the
    # PROJECTS_DIRECTORY variable of its container. Use it when that path
    # exists on this host too.
    for cid in $(docker ps -aq); do
        img="$(docker inspect -f '{{.Config.Image}}' "$cid")"
        if [[ "$img" == *arcane* ]]; then
            found="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" \
                     | sed -n 's/^PROJECTS_DIRECTORY=//p' | head -n1)"
            if [ -n "$found" ] && [ -d "$found" ]; then PROJECTS_DIR="$found"; break; fi
        fi
    done
    if [ -z "$PROJECTS_DIR" ] && [ -d /opt/arcane/projects ]; then
        PROJECTS_DIR=/opt/arcane/projects
    fi
fi
PROJECTS_DIR="${PROJECTS_DIR%/}"
if [ -n "$PROJECTS_DIR" ] && [ ! -d "$PROJECTS_DIR" ]; then
    die "Projects directory '$PROJECTS_DIR' does not exist. Correct PROJECTS_DIR, or remove it from ${ENV_FILE}."
fi

setting EXTRA_DIRS ""
setting SKIP_DIRS ""
setting BORG_EXCLUDES "${VOLUMES_DIR}/backingFsBlockDev"
setting KEPT_VOLUMES ""
setting LARGE_VOLUME_MB 1024
[[ "$LARGE_VOLUME_MB" =~ ^[0-9]+$ ]] || die "LARGE_VOLUME_MB '$LARGE_VOLUME_MB' is not a whole number of megabytes."

# Drop any previously chosen path that no longer exists.
kept=""
for d in $EXTRA_DIRS; do
    if [ -e "$d" ]; then
        kept="${kept:+$kept }$d"
    else
        warn "Previously included path '$d' no longer exists; dropping it."
    fi
done
EXTRA_DIRS="$kept"
kept=""
for d in $SKIP_DIRS; do
    if [ -e "$d" ]; then kept="${kept:+$kept }$d"; fi
done
SKIP_DIRS="$kept"

# True when the path is already backed up, or was declined before.
covered() {
    local r
    # shellcheck disable=SC2086
    for r in $PROJECTS_DIR "$VOLUMES_DIR" $EXTRA_DIRS $SKIP_DIRS; do
        case "$1" in "$r"|"$r"/*) return 0 ;; esac
    done
    return 1
}

# A named volume whose driver mounts something, such as an NFS share, or a
# host directory through type=none,o=bind, is mounted at its _data only while
# a container uses it, so once the containers stop the backup would find an
# empty directory there. A bind-backed volume's real directory is offered
# below like any other; anything else is reported, since it has to be backed
# up where it lives.
volume_sources=()
mounted_volumes=" "
mapfile -t volume_info < <(
    docker volume ls -q 2>/dev/null | while IFS= read -r v; do
        docker volume inspect -f '{{.Name}}|{{.Driver}}|{{json .Options}}' "$v" 2>/dev/null
    done
)
for line in "${volume_info[@]}"; do
    IFS='|' read -r v driver opts <<<"$line"
    vtype="$(sed -n 's/.*"type":"\([^"]*\)".*/\1/p' <<<"$opts")"
    vopts="$(sed -n 's/.*"o":"\([^"]*\)".*/\1/p' <<<"$opts")"
    vdevice="$(sed -n 's/.*"device":"\([^"]*\)".*/\1/p' <<<"$opts")"
    if [ "$driver" = local ] && [ -z "$vtype" ] && [ -z "$vdevice" ]; then
        continue
    fi
    mounted_volumes+="$v "
    if [ "$driver" = local ] && [[ ",$vopts," == *,bind,* ]] && [[ "$vdevice" == /* ]]; then
        volume_sources+=("$vdevice")
    else
        warn "Volume ${v} is mounted from elsewhere (driver ${driver}${vtype:+, type ${vtype}}) and is not backed up: its directory is empty once its containers stop. Back it up where it lives."
    fi
done

# Every bind mount source, every compose project directory, and the directory
# behind each bind-backed volume is a candidate. Those already covered, and system paths that are never worth
# backing up, are left out; each of the rest is offered once, and the answer
# is remembered.
mapfile -t candidates < <(
    {
        # shellcheck disable=SC2046
        docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}' $(docker ps -aq) 2>/dev/null
        # shellcheck disable=SC2046
        docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' $(docker ps -aq) 2>/dev/null
    } \
    | grep -v -E '^(/var/run/docker\.sock|/etc/localtime|/etc/timezone|/dev|/sys|/proc|/run|/tmp)(/|$)' \
    | grep -v -x -e '<no value>' -e '' \
    | sort -u || true
)
candidates+=("${volume_sources[@]}")
for d in "${candidates[@]}"; do
    d="${d%/}"
    [ -n "$d" ] && [ -e "$d" ] || continue
    covered "$d" && continue
    case "$d" in
        *[[:space:]]*) warn "Skipping '$d': paths with spaces cannot be listed in EXTRA_DIRS."; continue ;;
    esac
    size="$(du -sh "$d" 2>/dev/null | cut -f1 || true)"
    if ask "Not covered: ${d}  (${size:-?})  Include in backup?"; then
        EXTRA_DIRS="${EXTRA_DIRS:+$EXTRA_DIRS }${d}"
    else
        SKIP_DIRS="${SKIP_DIRS:+$SKIP_DIRS }${d}"
    fi
done

# Named volumes are backed up as one location, so a large volume arrives
# without a question. Each one over LARGE_VOLUME_MB is offered for leaving
# out, once, with how much of it changed in the last day: a changed file is
# read in full on every run, while the containers are down, so that figure is
# roughly what each night costs. A volume already excluded, wholly or in
# part, counts as decided.
volume_decided() {
    local x
    for x in $KEPT_VOLUMES; do
        if [ "$x" = "$1" ]; then return 0; fi
    done
    for x in $BORG_EXCLUDES; do
        case "$x" in "${VOLUMES_DIR}/$1"|"${VOLUMES_DIR}/$1"/*) return 0 ;; esac
    done
    return 1
}
kept=""
for v in $KEPT_VOLUMES; do
    if [ -d "${VOLUMES_DIR}/$v" ]; then kept="${kept:+$kept }$v"; fi
done
KEPT_VOLUMES="$kept"
# Listed first, because ask reads its answers from stdin.
mapfile -t volume_dirs < <(find "$VOLUMES_DIR" -mindepth 1 -maxdepth 1 -type d | sort)
for vdir in "${volume_dirs[@]}"; do
    v="$(basename "$vdir")"
    # A mount-backed volume's size here is whatever is mounted now, and it
    # was dealt with above.
    case "$mounted_volumes" in *" $v "*) continue ;; esac
    volume_decided "$v" && continue
    mb="$(du -sm "$vdir" 2>/dev/null | cut -f1 || true)"
    [ -n "$mb" ] && [ "$mb" -ge "$LARGE_VOLUME_MB" ] || continue
    total="$(du -sh "$vdir" 2>/dev/null | cut -f1 || true)"
    changed="$(find "$vdir" -xdev -type f -mmin -1440 -printf '%s\n' 2>/dev/null \
               | awk '{ s += $1 } END { if (s >= 1073741824) printf "%.1fG", s / 1073741824; else printf "%dM", s / 1048576 }')"
    if ask "Large volume: ${v}  (${total:-?}, ${changed} changed in the last day)  Leave it out of the backup?"; then
        BORG_EXCLUDES="${BORG_EXCLUDES:+$BORG_EXCLUDES }${VOLUMES_DIR}/${v}"
    else
        KEPT_VOLUMES="${KEPT_VOLUMES:+$KEPT_VOLUMES }${v}"
    fi
done

# Saying yes to a directory can cover one chosen earlier, such as a compose
# project holding a data directory already listed. Keep only the outermost,
# so that nothing is given to borg twice.
kept=""
for d in $EXTRA_DIRS; do
    outer=""
    # shellcheck disable=SC2086
    for e in $PROJECTS_DIR "$VOLUMES_DIR" $EXTRA_DIRS; do
        case "$d" in "$e"/*) outer="$e" ;; esac
    done
    case " $kept " in *" $d "*) continue ;; esac
    if [ -n "$outer" ]; then
        echo "Dropping ${d} from the extra directories: ${outer} already covers it."
    else
        kept="${kept:+$kept }$d"
    fi
done
EXTRA_DIRS="$kept"

echo "Projects directory: ${PROJECTS_DIR:-(none)}"
echo "Volumes directory:  ${VOLUMES_DIR}"
echo "Extra directories:  ${EXTRA_DIRS:-(none)}"
echo "Not asked again:    ${SKIP_DIRS:-(none)}"
echo "Large volumes kept: ${KEPT_VOLUMES:-(none)}"
echo "Excludes:           ${BORG_EXCLUDES:-(none)}"

# ---------------------------------------------------------------------------
step "3/8  SSH key"
# ---------------------------------------------------------------------------
install -d -m 700 /root/.ssh
if [ -f "$KEY" ]; then
    echo "Key already exists at $KEY"
else
    ssh-keygen -t ed25519 -N "" -C "borg@${CLIENT_NAME}" -f "$KEY" >/dev/null
    echo "Generated $KEY"
fi

# The server lets this key run only 'borg serve', so whatever command is
# asked for, borg serve is what starts. With stdin at /dev/null it reads end
# of file and exits. ssh itself exits 255 when it cannot connect or is
# refused, and passes on the remote exit status otherwise, so anything but
# 255 means the key was accepted.
ssh_error=""
ssh_ok() {
    local out rc
    out="$(timeout 30 ssh -n -o BatchMode=yes -o ConnectTimeout=10 "$@" true 2>&1)" && rc=0 || rc=$?
    if [ "$rc" -eq 255 ] || [ "$rc" -eq 124 ]; then
        if [ "$rc" -eq 124 ]; then
            ssh_error="timed out after 30 seconds"
        else
            ssh_error="$(grep -v '^Warning: Permanently added' <<<"$out" | head -n3 || true)"
        fi
        return 1
    fi
    return 0
}
if ssh_ok -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes -i "$KEY" \
          -p "$SERVER_PORT" "${SERVER_USER}@${SERVER_HOST}"; then
    echo "Key is already accepted by ${SERVER_HOST}:${SERVER_PORT}; skipping key install."
else
    echo "The server did not accept the key yet: ${ssh_error:-no detail}"
    cat <<EOF

Add this public key to the Borg server for client '${CLIENT_NAME}'. With the
borgserver image that is a file named ${CLIENT_NAME}, holding this one line
and nothing else, in the clients directory, then a restart of the container:

    ${SERVER_KEYS_DIR%/}/${CLIENT_NAME}
EOF
    if [ -n "$SERVER_KEY_OWNER" ]; then
        echo "    chown ${SERVER_KEY_OWNER} ${SERVER_KEYS_DIR%/}/${CLIENT_NAME}"
    fi
    cat <<EOF
    chmod 600 ${SERVER_KEYS_DIR%/}/${CLIENT_NAME}
    docker restart ${BORGSERVER_CONTAINER}

----------------------------------------------------------------------
$(cat "${KEY}.pub")
----------------------------------------------------------------------
EOF
    read -r -p "Press Enter once the key is in place and the server has restarted... " _
fi

# ---------------------------------------------------------------------------
step "4/8  SSH config and connection test"
# ---------------------------------------------------------------------------
desired_block="Host ${SSH_ALIAS}
    HostName ${SERVER_HOST}
    Port ${SERVER_PORT}
    User ${SERVER_USER}
    IdentityFile ${KEY}
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new"

# The current stanza for the alias, from its Host line up to the next Host or
# Match line, with blank lines dropped and whitespace normalised.
current_block=""
if [ -f "$SSH_CONFIG" ]; then
    current_block="$(awk -v a="$SSH_ALIAS" '
        { k = tolower($1) }
        k == "host" || k == "match" { inblk = (k == "host" && NF == 2 && $2 == a) }
        inblk && NF { $1 = $1; print }
    ' "$SSH_CONFIG")"
fi
normalised_desired="$(awk 'NF { $1 = $1; print }' <<<"$desired_block")"

if [ "$current_block" = "$normalised_desired" ]; then
    echo "'${SSH_ALIAS}' in ${SSH_CONFIG} is already up to date"
else
    rest="$(mktemp)"
    new_config="$(mktemp)"
    if [ -f "$SSH_CONFIG" ]; then
        cp -p "$SSH_CONFIG" "${SSH_CONFIG}.bak-borg-docker"
        # Everything except the alias's own stanza, without trailing blank lines.
        awk -v a="$SSH_ALIAS" '
            { k = tolower($1) }
            k == "host" || k == "match" { skip = (k == "host" && NF == 2 && $2 == a) }
            skip { next }
            NF == 0 { blanks++; next }
            { while (blanks > 0) { print ""; blanks-- } print }
        ' "$SSH_CONFIG" > "$rest"
    fi
    # The stanza goes first, because ssh takes the first value it finds for
    # each option and an earlier Host * would otherwise override it. Settings
    # at the top of the old file applied to every host, and a Host * line
    # after the stanza keeps them doing so.
    {
        printf '%s\n' "$desired_block"
        if [ -s "$rest" ]; then
            echo
            first="$(awk 'NF && $1 !~ /^#/ { print tolower($1); exit }' "$rest")"
            if [ -n "$first" ] && [ "$first" != host ] && [ "$first" != match ]; then
                echo "Host *"
            fi
            cat "$rest"
        fi
    } > "$new_config"
    install -m 600 "$new_config" "$SSH_CONFIG"
    rm -f "$rest" "$new_config"
    if [ -n "$current_block" ]; then
        echo "Updated '${SSH_ALIAS}' in ${SSH_CONFIG} (previous file kept as ${SSH_CONFIG}.bak-borg-docker)"
    else
        echo "Added '${SSH_ALIAS}' to ${SSH_CONFIG}"
    fi
fi

ssh_ok "$SSH_ALIAS" || die "SSH to ${SERVER_HOST}:${SERVER_PORT} through '${SSH_ALIAS}' failed: ${ssh_error:-no detail}
Check the key file's name, contents and ownership on the server, and that the server was restarted after it was added."
echo "SSH auth OK."

# ---------------------------------------------------------------------------
step "5/8  Repository passphrase and init"
# ---------------------------------------------------------------------------
install -d -m 700 "$(dirname "$PASSFILE")"
if [ -f "$PASSFILE" ]; then
    echo "Passphrase file already exists at $PASSFILE"
elif ask "No passphrase file found. Is this a rebuild of a client whose repo already exists on the server?"; then
    read -r -s -p "Paste the saved passphrase for ${REPO}: " pp; echo
    [ -n "$pp" ] || die "Empty passphrase."
    (umask 077; printf '%s' "$pp" > "$PASSFILE")
    unset pp
    echo "Wrote $PASSFILE from your input."
else
    (umask 077; head -c 32 /dev/urandom | base64 | tr -d '\n=/+' > "$PASSFILE")
    cat <<EOF

A new repository passphrase was generated and saved to ${PASSFILE}.
Store a copy somewhere OFF this host (a password manager). Without it the
backups cannot be read, including after this host is reinstalled.

    $(cat "$PASSFILE")

EOF
    read -r -p "Press Enter once you have saved it... " _
fi

export BORG_REPO="$REPO"
export BORG_PASSCOMMAND="cat ${PASSFILE}"
export BORG_RSH="ssh -o BatchMode=yes"
# Borg asks before using a repository it remembers at another address. The
# switch was agreed to above, so the question is answered here.
if [ "$relocated" -eq 1 ]; then export BORG_RELOCATED_REPO_ACCESS_IS_OK=yes; fi

# stdin is /dev/null so that any question borg asks is answered no, rather
# than waiting unseen inside the capture.
if info_out="$(borg info </dev/null 2>&1)"; then
    echo "Repository already initialised: $REPO"
elif grep -q 'does not exist' <<<"$info_out"; then
    if [ -n "$prev_repo" ]; then
        warn "This host was set up before, but there is no repository at ${REPO}."
        ask "Initialise a new, empty repository there?" || die "Nothing initialised."
    fi
    borg init --encryption=repokey-blake2 </dev/null
    echo "Initialised repository: $REPO"
else
    die "borg could not open ${REPO}:
${info_out}"
fi

# ---------------------------------------------------------------------------
step "6/8  Backup script"
# ---------------------------------------------------------------------------
# All tunables live in the env file so the script itself never needs editing.
{
    echo "# Written by borg-docker's setup.sh. Read by ${BACKUP_SCRIPT}"
    echo "# and by the next run of setup.sh. Edit EXTRA_DIRS, SKIP_DIRS and"
    echo "# BORG_EXCLUDES here freely; to move the repository, run setup.sh again."
    echo "BORG_REPO=$(envq "$REPO")"
    echo "BORG_PASSCOMMAND=$(envq "cat ${PASSFILE}")"
    echo "BORG_RSH=$(envq "ssh -o BatchMode=yes")"
    echo "SERVER_HOST=$(envq "$SERVER_HOST")"
    echo "SERVER_PORT=$(envq "$SERVER_PORT")"
    echo "SERVER_USER=$(envq "$SERVER_USER")"
    echo "SSH_ALIAS=$(envq "$SSH_ALIAS")"
    echo "CLIENT_NAME=$(envq "$CLIENT_NAME")"
    echo "REPO_NAME=$(envq "$REPO_NAME")"
    if [ -n "$explicit_repo_path" ]; then echo "REPO_PATH=$(envq "$REPO_PATH")"; fi
    echo "BACKUP_TIME=$(envq "$BACKUP_TIME")"
    echo "PROJECTS_DIR=$(envq "$PROJECTS_DIR")"
    echo "VOLUMES_DIR=$(envq "$VOLUMES_DIR")"
    echo "EXTRA_DIRS=$(envq "$EXTRA_DIRS")"
    echo "SKIP_DIRS=$(envq "$SKIP_DIRS")"
    echo "KEPT_VOLUMES=$(envq "$KEPT_VOLUMES")"
    echo "LARGE_VOLUME_MB=$(envq "$LARGE_VOLUME_MB")"
    echo "BORG_EXCLUDES=$(envq "$BORG_EXCLUDES")"
} > "${ENV_FILE}.new"
chmod 600 "${ENV_FILE}.new"
mv "${ENV_FILE}.new" "$ENV_FILE"
echo "Wrote $ENV_FILE"

new_script="$(mktemp)"
cat > "$new_script" <<'EOF'
#!/usr/bin/env bash
#
# borg-backup.sh, installed by borg-docker's setup.sh.
#
# Stops every running container, backs up the projects directory, the named
# volumes and any EXTRA_DIRS to the Borg repository, then restarts exactly the
# containers that were running, whether or not the backup worked.
#
# All configuration lives in /etc/borg-backup.env. Edit that, not this.
#
# If the env file does not load, or the running containers cannot be listed,
# the run exits 2 before stopping anything or backing anything up. If any of them fails to stop, it
# exits 2 without backing up, after starting again those that did stop. A
container that fails to start again afterwards makes the run exit 2 as well.
#
# A path listed for backup that no longer exists is left out, the rest are
# backed up, and the run then exits 2 so the failure shows. Borg's own exit
# status 1 means the archive was written with a warning, usually a file that
# changed while it was read; that is logged and counted as success. Status 2
# or more is an error and is passed on.
#
# Pruning is deliberately not done here: the server is append-only, so
# retention is enforced on the server itself.
#
set -uo pipefail
# Paths are split on spaces below and never meant as patterns.
set -o noglob
# A settings file that will not load, or loads without a repository, would
# leave the paths to back up wrong in ways nothing else here would notice.
set -a
if ! . /etc/borg-backup.env || [ -z "${BORG_REPO:-}" ]; then
    echo "ERROR: /etc/borg-backup.env did not load cleanly; nothing was stopped or backed up." >&2
    exit 2
fi
set +a

sources=()
missing=()
# shellcheck disable=SC2086
for d in ${PROJECTS_DIR:-} "${VOLUMES_DIR:-/var/lib/docker/volumes}" ${EXTRA_DIRS:-}; do
    if [ -e "$d" ]; then sources+=("$d"); else missing+=("$d"); fi
done

exclude_args=()
for x in ${BORG_EXCLUDES:-}; do
    exclude_args+=(--exclude "$x")
done

prefix="${CLIENT_NAME:-}"
[ -n "$prefix" ] || prefix='{hostname}'

# Without the list of running containers nothing can be stopped, and a backup
# of live databases is worse than none, so a failure here ends the run.
if ! running="$(docker ps -q)"; then
    echo "ERROR: could not list the running containers; nothing was stopped or backed up." >&2
    exit 2
fi
# One line per container becomes one space-separated list, and no containers
# stays an empty string.
running="${running//$'\n'/ }"

# Runs on every exit. A container that will not start again fails the run,
# with Docker's own error in the journal, unless the run had failed already.
restart_containers() {
    local status=$?
    if [ -n "$running" ]; then
        # shellcheck disable=SC2086
        if ! docker start $running >/dev/null; then
            echo "ERROR: not every container started again; check docker ps -a." >&2
            [ "$status" -ne 0 ] || status=2
        fi
    fi
    exit "$status"
}
trap restart_containers EXIT

# A container that would not stop may still be writing, so nothing is backed
# up; the trap starts again whatever was stopped.
if [ -n "$running" ]; then
    # shellcheck disable=SC2086
    if ! docker stop --time 30 $running >/dev/null; then
        echo "ERROR: not every container stopped; nothing was backed up." >&2
        exit 2
    fi
fi

borg create \
    --stats \
    --compression zstd,6 \
    "${exclude_args[@]}" \
    "::${prefix}-{now:%Y-%m-%dT%H:%M}" \
    "${sources[@]}"
rc=$?

if [ "$rc" -eq 1 ]; then
    echo "borg finished with warnings, shown above; the archive was written." >&2
    rc=0
fi
if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: not backed up, because these no longer exist: ${missing[*]}" >&2
    echo "Remove them from /etc/borg-backup.env." >&2
    [ "$rc" -ne 0 ] || rc=2
fi
exit "$rc"
EOF

if [ -f "$BACKUP_SCRIPT" ] && ! cmp -s "$new_script" "$BACKUP_SCRIPT"; then
    echo "An existing $BACKUP_SCRIPT differs from the version this setup would install."
    echo "If you edited it by hand, those edits will be lost. Diff (existing -> new):"
    diff -u "$BACKUP_SCRIPT" "$new_script" | sed 's/^/    /' || true
    if ask "Overwrite $BACKUP_SCRIPT?"; then
        install -m 750 "$new_script" "$BACKUP_SCRIPT"
        echo "Replaced $BACKUP_SCRIPT"
    else
        echo "Kept existing $BACKUP_SCRIPT"
    fi
elif [ -f "$BACKUP_SCRIPT" ]; then
    echo "$BACKUP_SCRIPT is already up to date"
else
    install -m 750 "$new_script" "$BACKUP_SCRIPT"
    echo "Installed $BACKUP_SCRIPT"
fi
rm -f "$new_script"

# ---------------------------------------------------------------------------
step "7/8  systemd service and timer"
# ---------------------------------------------------------------------------
cat > /etc/systemd/system/borg-backup.service <<EOF
[Unit]
Description=Borg backup of Docker projects and volumes
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=${BACKUP_SCRIPT}
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
EOF

cat > /etc/systemd/system/borg-backup.timer <<EOF
[Unit]
Description=Daily Borg backup

[Timer]
OnCalendar=*-*-* ${BACKUP_TIME}:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now borg-backup.timer >/dev/null 2>&1
echo "Timer enabled. Next run:"
systemctl list-timers borg-backup.timer --no-pager | sed -n '2p'

# ---------------------------------------------------------------------------
step "8/8  Run a backup now?"
# ---------------------------------------------------------------------------
if ask "Run a backup now? Containers will be stopped briefly."; then
    if systemctl start borg-backup.service; then
        echo
        borg list
    else
        warn "Backup failed. Last 30 log lines:"
        journalctl -u borg-backup.service -n 30 --no-pager
    fi
else
    echo "Skipped. Run manually with:  sudo systemctl start borg-backup.service"
fi

cat <<EOF

Done.

  Repo:        ${REPO}
  Passphrase:  ${PASSFILE}
  Env:         ${ENV_FILE}   (edit EXTRA_DIRS / BORG_EXCLUDES here; no script edits needed)
  Script:      ${BACKUP_SCRIPT}
  Logs:        journalctl -u borg-backup.service
  Archives:    sudo bash -c 'set -a; . ${ENV_FILE}; set +a; borg list'

EOF
