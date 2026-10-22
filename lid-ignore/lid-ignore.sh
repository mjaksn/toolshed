#!/usr/bin/env bash
#
# lid-ignore.sh
#
# Configure a laptop running Linux to ignore its lid switch so it can run as a
# headless server with the lid closed.
#
# System level (always applied):
#   - systemd-logind or elogind   HandleLidSwitch*=ignore via a drop-in
#   - UPower                      IgnoreLid=true
#   - acpid / acpi-support        LID_SLEEP=false, flags custom lid handlers
#
# Per user (desktop power managers that override logind):
#   - XFCE                        xfce4-power-manager via xfconf
#   - GNOME / Budgie / Pantheon   gsettings (legacy keys if present, else logind)
#   - Cinnamon                    gsettings
#   - MATE                        gsettings
#   - KDE Plasma 5 and 6          powerdevil config files
#   - LXQt                        lxqt-powermanagement.conf
#   - anything else               any remaining gsettings keys mentioning "lid"
#                                 are listed for manual review
#
# Usage: sudo ./lid-ignore.sh [--dry-run] [--user NAME ...] [--all-users]
#
#   --dry-run     Report what would change without writing anything.
#   --user NAME   Apply desktop settings for this user (repeatable). Default is
#                 the invoking sudo user plus anyone with a logged-in session.
#   --all-users   Apply desktop settings for every local user with a home dir.
#
# Safe to re-run. Exits non-zero only if a change failed to apply.

set -uo pipefail

DRY_RUN=0
ALL_USERS=0
declare -a USERS=()
FAILURES=0
CHANGED_ACPID=0

# ---------------------------------------------------------------- output ----

if [[ -t 1 ]]; then
  C_INFO=$'\e[36m'; C_OK=$'\e[32m'; C_CHG=$'\e[33m'; C_WARN=$'\e[31m'; C_RST=$'\e[0m'
else
  C_INFO=""; C_OK=""; C_CHG=""; C_WARN=""; C_RST=""
fi

info()    { printf '%s[info]%s %s\n' "$C_INFO" "$C_RST" "$*"; }
ok()      { printf '%s[ ok ]%s %s\n' "$C_OK" "$C_RST" "$*"; }
warn()    { printf '%s[warn]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; }
fail()    { printf '%s[FAIL]%s %s\n' "$C_WARN" "$C_RST" "$*" >&2; FAILURES=$((FAILURES + 1)); }
section() { printf '\n%s== %s ==%s\n' "$C_INFO" "$*" "$C_RST"; }
change() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s[would]%s %s\n' "$C_CHG" "$C_RST" "$*"
  else
    printf '%s[ set ]%s %s\n' "$C_CHG" "$C_RST" "$*"
  fi
}

# Run a command unless in dry-run mode.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '        %s\n' "$*"
    return 0
  fi
  "$@"
}

# ----------------------------------------------------------- file helpers ----

# write_file PATH   (content on stdin). Idempotent.
write_file() {
  local path=$1 content
  content=$(cat)
  if [[ -f $path ]] && [[ "$(cat "$path")" == "$content" ]]; then
    ok "$path already correct"
    return 0
  fi
  change "write $path"
  [[ $DRY_RUN -eq 1 ]] && return 0
  mkdir -p "$(dirname "$path")" && printf '%s\n' "$content" > "$path" || fail "could not write $path"
}

# kv_set FILE KEY VALUE   for sectionless KEY=VALUE files (/etc/default/*).
# These files are sourced, so the last assignment wins: the key counts as set
# only when every assignment of it has the value, and otherwise the edit below
# rewrites them all.
kv_set() {
  local file=$1 key=$2 value=$3
  if [[ -f $file ]] && awk -v k="$key" -v v="$value" '
      $0 ~ "^[[:space:]]*" k "=" { n++; if ($0 != k "=" v) off = 1 }
      END { exit !(n && !off) }' "$file"; then
    ok "$file: $key=$value already set"
    return 0
  fi
  change "$file: $key=$value"
  [[ $DRY_RUN -eq 1 ]] && return 0
  if grep -Eq "^[[:space:]]*${key}=" "$file" 2>/dev/null; then
    sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${value}|" "$file" || fail "could not edit $file"
  else
    # On a line of its own, even when the file ends without a line break.
    [[ -s $file && -n $(tail -c1 "$file") ]] && printf '\n' >> "$file"
    printf '%s=%s\n' "$key" "$value" >> "$file" || fail "could not append to $file"
  fi
}

# ini_set FILE SECTION KEY VALUE   Idempotent INI edit. SECTION is the text
# between the brackets; nested KDE groups like "AC][SuspendAndShutdown" work.
# The key counts as set only when every entry of it in the section has the
# value, since a reader may take the last; otherwise the edit below keeps the
# first entry, with the value, and drops the rest.
ini_set() {
  local file=$1 section=$2 key=$3 value=$4
  if [[ -f $file ]] && awk -v s="[$section]" -v k="$key" -v v="$value" '
      $0 == s { insec = 1; next }
      /^\[/   { insec = 0 }
      insec && index($0, k "=") == 1 { n++; if ($0 != k "=" v) off = 1 }
      END { exit !(n && !off) }' "$file"; then
    ok "$file: [$section] $key=$value already set"
    return 0
  fi
  change "$file: [$section] $key=$value"
  [[ $DRY_RUN -eq 1 ]] && return 0
  mkdir -p "$(dirname "$file")" || { fail "could not create $(dirname "$file")"; return 1; }
  [[ -f $file ]] || : > "$file"
  local tmp
  tmp=$(mktemp) || { fail "could not create a temporary file for $file"; return 1; }
  awk -v s="[$section]" -v k="$key" -v v="$value" '
    function flush() {
      if (insec && !done) { print k "=" v; done = 1 }
      while (blanks > 0) { print ""; blanks-- }
    }
    $0 == s { seen = 1; insec = 1; print; next }
    /^\[/   { flush(); insec = 0 }
    insec && /^[[:space:]]*$/ { blanks++; next }
    insec && index($0, k "=") == 1 { while (blanks > 0) { print ""; blanks-- }; if (!done) { print k "=" v; done = 1 }; next }
    insec { while (blanks > 0) { print ""; blanks-- } }
    { print }
    END { flush(); if (!seen) { if (NR > 0) print ""; print s; print k "=" v } }
  ' "$file" > "$tmp" && cat "$tmp" > "$file" || fail "could not edit $file"
  rm -f "$tmp"
}

# ---------------------------------------------------------- system level ----

systemd_version() {
  systemctl --version 2>/dev/null | awk 'NR == 1 { print $2; exit }' | tr -dc '0-9'
}

configure_logind() {
  section "systemd-logind / elogind"
  local conf dropin_dir svc
  if [[ -d /run/systemd/system ]]; then
    conf=/etc/systemd/logind.conf
    dropin_dir=/etc/systemd/logind.conf.d
    svc=systemd-logind
  elif [[ -f /etc/elogind/logind.conf ]] || command -v elogind >/dev/null 2>&1; then
    conf=/etc/elogind/logind.conf
    dropin_dir=/etc/elogind/logind.conf.d
    svc=elogind
  else
    warn "neither systemd-logind nor elogind found; relying on acpid/UPower settings only"
    return
  fi

  if grep -Eq '^[[:space:]]*HandleLidSwitch' "$conf" 2>/dev/null; then
    info "$conf sets HandleLidSwitch* directly; the drop-in below takes precedence"
  fi

  write_file "$dropin_dir/99-ignore-lid.conf" <<'EOF'
# Installed by lid-ignore.sh: this machine runs with the lid closed.
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF

  [[ $DRY_RUN -eq 1 ]] && return
  if [[ $svc == systemd-logind ]]; then
    local ver
    ver=$(systemd_version)
    if [[ -n $ver && $ver -lt 240 ]]; then
      warn "systemd $ver is old; restarting logind can drop sessions. Reboot to apply instead."
    else
      run systemctl restart systemd-logind && ok "systemd-logind restarted" \
        || fail "could not restart systemd-logind (reboot to apply)"
    fi
  else
    if command -v rc-service >/dev/null 2>&1; then
      run rc-service elogind restart || warn "could not restart elogind; reboot to apply"
    elif command -v service >/dev/null 2>&1; then
      run service elogind restart || warn "could not restart elogind; reboot to apply"
    else
      warn "restart elogind manually or reboot to apply"
    fi
  fi
}

configure_upower() {
  section "UPower"
  local conf=/etc/UPower/UPower.conf
  if [[ ! -f $conf ]]; then
    info "UPower not installed; skipping"
    return
  fi
  ini_set "$conf" UPower IgnoreLid true
  [[ $DRY_RUN -eq 1 ]] && return
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet upower 2>/dev/null; then
    run systemctl restart upower || warn "could not restart upower; takes effect on next boot"
  fi
}

configure_acpid() {
  section "acpid / acpi-support"
  local found=0 f ev
  if [[ -f /etc/default/acpi-support ]]; then
    found=1
    kv_set /etc/default/acpi-support LID_SLEEP false && CHANGED_ACPID=1
  fi
  shopt -s nullglob
  # acpid matches a rule by its event= pattern, not by its name, so a rule
  # named anything can act on the lid. One whose pattern names the lid, or
  # catches every event, is reported, and so is any action named for the lid.
  for f in /etc/acpi/events/*; do
    [[ -f $f ]] || continue
    ev=$(grep -Ei '^[[:space:]]*event=' "$f" 2>/dev/null | head -n1)
    if [[ $f == *[Ll][Ii][Dd]* ]] \
       || grep -Eiq '^[[:space:]]*event=[[:space:]]*(.*lid.*|\.\*|\*|button/?\.?\*)[[:space:]]*$' <<<"$ev"; then
      found=1
      warn "acpid rule $f (${ev:-no event line}) may act on the lid; review it and its action"
    fi
  done
  for f in /etc/acpi/actions/*[Ll][Ii][Dd]*; do
    found=1
    warn "custom acpid lid handler present: $f (review it; it may still act on lid close)"
  done
  shopt -u nullglob
  [[ $found -eq 0 ]] && info "no acpid lid handling found"
  if [[ $DRY_RUN -eq 0 && $CHANGED_ACPID -eq 1 ]] && command -v systemctl >/dev/null 2>&1 \
     && systemctl is-active --quiet acpid 2>/dev/null; then
    run systemctl reload acpid 2>/dev/null || run systemctl restart acpid || true
  fi
}

# ------------------------------------------------------------- per user ----

collect_users() {
  if [[ $ALL_USERS -eq 1 ]]; then
    while IFS=: read -r name _ uid _ _ home _; do
      [[ $uid -ge 1000 && $uid -lt 60000 && -d $home ]] && USERS+=("$name")
    done < /etc/passwd
    return
  fi
  [[ ${#USERS[@]} -gt 0 ]] && return
  [[ -n ${SUDO_USER:-} && ${SUDO_USER:-} != root ]] && USERS+=("$SUDO_USER")
  if command -v loginctl >/dev/null 2>&1; then
    while read -r _ name _; do
      [[ -n $name && $name != root ]] && USERS+=("$name")
    done < <(loginctl list-users --no-legend 2>/dev/null)
  else
    while read -r name; do
      [[ -n $name && $name != root ]] && USERS+=("$name")
    done < <(who 2>/dev/null | awk '{ print $1 }' | sort -u)
  fi
  if [[ ${#USERS[@]} -gt 0 ]]; then
    mapfile -t USERS < <(printf '%s\n' "${USERS[@]}" | awk 'NF && !seen[$0]++')
  fi
}

# Pull DISPLAY / XAUTHORITY / WAYLAND_DISPLAY / DBUS_SESSION_BUS_ADDRESS from a
# live process of the user so settings land in their running session.
user_env() {
  local uid=$1 pid env best=""
  for pid in $(pgrep -u "$uid" 2>/dev/null); do
    [[ -r /proc/$pid/environ ]] || continue
    env=$({ tr '\0' '\n' < "/proc/$pid/environ"; } 2>/dev/null \
          | grep -E '^(DISPLAY|XAUTHORITY|WAYLAND_DISPLAY|DBUS_SESSION_BUS_ADDRESS)=') || continue
    grep -q '^DBUS_SESSION_BUS_ADDRESS=' <<<"$env" || continue
    best=$env
    grep -q '^DISPLAY=' <<<"$env" && break
  done
  if [[ -n $best ]]; then
    printf '%s\n' "$best"
  elif [[ -S /run/user/$uid/bus ]]; then
    printf 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus\n' "$uid"
  fi
  return 0
}

# as_user [--no-bus] NAME CMD...   run as the user with their session
# environment, or on a private bus when they have none: U_BUS is set by
# configure_user_desktops. --no-bus is for a command that needs no bus.
as_user() {
  local -a bus=(${U_BUS[@]+"${U_BUS[@]}"})
  if [[ $1 == --no-bus ]]; then
    bus=()
    shift
  fi
  local name=$1; shift
  local -a envargs=(HOME="$U_HOME" USER="$name" LOGNAME="$name" PATH="$PATH")
  [[ -d /run/user/$U_UID ]] && envargs+=(XDG_RUNTIME_DIR="/run/user/$U_UID")
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$name" -- env -i "${envargs[@]}" ${U_ENV[@]+"${U_ENV[@]}"} ${bus[@]+"${bus[@]}"} "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$name" env -i "${envargs[@]}" ${U_ENV[@]+"${U_ENV[@]}"} ${bus[@]+"${bus[@]}"} "$@"
  else
    fail "need runuser or sudo to run commands as $name"
    return 1
  fi
}

user_has() { printf '%s\n' ${U_ENV[@]+"${U_ENV[@]}"} | grep -q "^$1="; }

# user_ini_set NAME FILE SECTION KEY VALUE   ini_set, run as the user. This
# script runs as root and the user owns every part of the path, so a root
# write there could be led through a link to any file on the machine, and no
# check beforehand closes that, since the link can appear after the check.
# Run as the user, the edit reaches only what the user can, and whatever it
# creates is theirs from the start. The child counts its own failures and
# hands them back as its exit status.
user_ini_set() {
  local name=$1
  shift
  as_user --no-bus "$name" bash -c "$(declare -f ok change fail ini_set)
DRY_RUN=$DRY_RUN FAILURES=0
C_OK=${C_OK@Q} C_CHG=${C_CHG@Q} C_WARN=${C_WARN@Q} C_RST=${C_RST@Q}
ini_set \"\$@\"
exit \$(( FAILURES > 0 ))" ini_set "$@" || FAILURES=$((FAILURES + 1))
}

# xfce_pm_running UID   whether xfce4-power-manager is running for the user.
# The name is longer than the 15 characters a process name holds, so a plain
# pgrep never matches it; the full command line is matched instead.
xfce_pm_running() { pgrep -u "$1" -f '(^|/)xfce4-power-manager( |$)' >/dev/null 2>&1; }

xfconf_set() {
  local name=$1 prop=$2 type=$3 value=$4 cur
  cur=$(as_user "$name" xfconf-query -c xfce4-power-manager -p "$prop" 2>/dev/null)
  if [[ $cur == "$value" ]]; then
    ok "$name: xfconf $prop already $value"
    return 0
  fi
  change "$name: xfconf $prop -> $value"
  [[ $DRY_RUN -eq 1 ]] && return 0
  as_user "$name" xfconf-query -c xfce4-power-manager -p "$prop" -n -t "$type" -s "$value" 2>/dev/null \
    || fail "$name: could not set xfconf $prop"
}

# The greps here read to the end rather than stopping at the match with -q:
# under pipefail, a grep that stops early can hand gsettings a broken pipe and
# fail the pipeline on a schema that is there.
gsettings_schema_exists() {
  as_user "$1" gsettings list-schemas 2>/dev/null | grep -x "$2" >/dev/null
}

# gsettings_set NAME SCHEMA KEY VALUE   Returns 1 if the key does not exist.
gsettings_set() {
  local name=$1 schema=$2 key=$3 value=$4 cur
  as_user "$name" gsettings list-keys "$schema" 2>/dev/null | grep -x "$key" >/dev/null || return 1
  cur=$(as_user "$name" gsettings get "$schema" "$key" 2>/dev/null)
  if [[ $cur == "$value" ]]; then
    ok "$name: $schema $key already $value"
    return 0
  fi
  change "$name: $schema $key -> $value"
  [[ $DRY_RUN -eq 1 ]] && return 0
  as_user "$name" gsettings set "$schema" "$key" "$value" 2>/dev/null \
    || fail "$name: could not set $schema $key"
}

configure_user_desktops() {
  local name=$1
  U_UID=$(id -u "$name" 2>/dev/null) || { warn "user $name not found; skipping"; return; }
  U_HOME=$(getent passwd "$name" | cut -d: -f6)
  mapfile -t U_ENV < <(user_env "$U_UID")
  U_BUS=()
  U_NO_BUS=0
  local cfg="$U_HOME/.config"
  local handled=0

  section "desktop settings for $name (uid $U_UID)"
  if user_has DBUS_SESSION_BUS_ADDRESS; then
    info "using live session bus for $name"
  elif command -v dbus-run-session >/dev/null 2>&1; then
    # xfconf and gsettings need a bus to keep anything, and without one they
    # still report success. A private bus for each command makes the change
    # land in the user's files, for their next login.
    U_BUS=(dbus-run-session --)
    info "no session bus for $name; running each desktop tool on a private bus, so settings apply on next login"
  else
    U_NO_BUS=1
    warn "no session bus for $name and no dbus-run-session; xfconf and gsettings settings are skipped, file-based ones are written"
  fi

  # --- XFCE ---
  if [[ $U_NO_BUS -eq 0 ]] && command -v xfconf-query >/dev/null 2>&1 && { [[ -d $cfg/xfce4 ]] || xfce_pm_running "$U_UID"; }; then
    handled=1
    xfconf_set "$name" /xfce4-power-manager/lid-action-on-ac int 0
    xfconf_set "$name" /xfce4-power-manager/lid-action-on-battery int 0
    xfconf_set "$name" /xfce4-power-manager/logind-handle-lid-switch bool false
    if [[ $DRY_RUN -eq 0 ]] && xfce_pm_running "$U_UID" && user_has DISPLAY; then
      as_user "$name" xfce4-power-manager --restart >/dev/null 2>&1 && ok "$name: xfce4-power-manager restarted" \
        || info "$name: xfce4-power-manager picks the change up on next login"
    fi
  fi

  # --- GNOME family (GNOME, Budgie, Pantheon) ---
  if [[ $U_NO_BUS -eq 0 ]] && command -v gsettings >/dev/null 2>&1 && gsettings_schema_exists "$name" org.gnome.settings-daemon.plugins.power; then
    handled=1
    if ! gsettings_set "$name" org.gnome.settings-daemon.plugins.power lid-close-ac-action "'nothing'"; then
      info "$name: modern GNOME has no lid keys and defers to logind (configured above)"
    else
      gsettings_set "$name" org.gnome.settings-daemon.plugins.power lid-close-battery-action "'nothing'"
    fi
  fi

  # --- Cinnamon ---
  if [[ $U_NO_BUS -eq 0 ]] && command -v gsettings >/dev/null 2>&1 && gsettings_schema_exists "$name" org.cinnamon.settings-daemon.plugins.power; then
    handled=1
    gsettings_set "$name" org.cinnamon.settings-daemon.plugins.power lid-close-ac-action "'nothing'" || true
    gsettings_set "$name" org.cinnamon.settings-daemon.plugins.power lid-close-battery-action "'nothing'" || true
  fi

  # --- MATE ---
  if [[ $U_NO_BUS -eq 0 ]] && command -v gsettings >/dev/null 2>&1 && gsettings_schema_exists "$name" org.mate.power-manager; then
    handled=1
    gsettings_set "$name" org.mate.power-manager button-lid-ac "'nothing'" || true
    gsettings_set "$name" org.mate.power-manager button-lid-battery "'nothing'" || true
  fi

  # --- KDE Plasma (powerdevil) ---
  if command -v plasmashell >/dev/null 2>&1 || [[ -f $cfg/powerdevilrc || -f $cfg/powermanagementprofilesrc ]]; then
    handled=1
    local p do6=0 do5=0
    [[ -f $cfg/powerdevilrc ]] && do6=1
    [[ -f $cfg/powermanagementprofilesrc ]] && do5=1
    if [[ $do6 -eq 0 && $do5 -eq 0 ]]; then
      if command -v kwriteconfig6 >/dev/null 2>&1; then do6=1; elif command -v kwriteconfig5 >/dev/null 2>&1; then do5=1; else do6=1; do5=1; fi
    fi
    for p in AC Battery LowBattery; do
      [[ $do6 -eq 1 ]] && user_ini_set "$name" "$cfg/powerdevilrc" "$p][SuspendAndShutdown" LidAction 0
      [[ $do5 -eq 1 ]] && user_ini_set "$name" "$cfg/powermanagementprofilesrc" "$p][HandleButtonEvents" lidAction 0
    done
    if [[ $DRY_RUN -eq 0 ]] && user_has DBUS_SESSION_BUS_ADDRESS && command -v dbus-send >/dev/null 2>&1; then
      # A method call, and one that waits for the reply: dbus-send sends a
      # signal by default, which powerdevil ignores and which cannot fail.
      as_user "$name" dbus-send --session --type=method_call --print-reply \
        --dest=org.kde.Solid.PowerManagement \
        /org/kde/Solid/PowerManagement org.kde.Solid.PowerManagement.refreshStatus >/dev/null 2>&1 \
        && ok "$name: powerdevil reloaded" || info "$name: powerdevil picks the change up on next login"
    fi
  fi

  # --- LXQt ---
  if command -v lxqt-powermanagement >/dev/null 2>&1 || [[ -d $cfg/lxqt ]]; then
    handled=1
    user_ini_set "$name" "$cfg/lxqt/lxqt-powermanagement.conf" General lidWatcherEnabled false
  fi

  # --- catch-all: any other gsettings keys mentioning "lid" ---
  if [[ $U_NO_BUS -eq 0 ]] && command -v gsettings >/dev/null 2>&1; then
    local extra
    extra=$(as_user "$name" gsettings list-recursively 2>/dev/null \
      | grep -i 'lid' \
      | grep -Ev '^(org\.gnome\.settings-daemon\.plugins\.power|org\.cinnamon\.settings-daemon\.plugins\.power|org\.mate\.power-manager) ' \
      | grep -Eiv 'valid|solid|slide|holiday' || true)
    if [[ -n $extra ]]; then
      warn "$name: other lid-related settings found; review manually:"
      printf '        %s\n' "$extra"
    fi
  fi

  [[ $handled -eq 0 && $U_NO_BUS -eq 0 ]] && info "$name: no supported desktop environment detected; logind handles the lid"
}

# ------------------------------------------------------------------ main ----

usage() { sed -n '2,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

main() {
  while [[ $# -gt 0 ]]; do
    case $1 in
      --dry-run)   DRY_RUN=1 ;;
      --all-users) ALL_USERS=1 ;;
      --user)      [[ $# -ge 2 && -n $2 && $2 != -* ]] || { echo "--user needs a name" >&2; usage 1; }
                   USERS+=("$2"); shift ;;
      -h|--help)   usage 0 ;;
      *)           echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
  done

  if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root (use sudo)." >&2
    exit 1
  fi

  [[ $DRY_RUN -eq 1 ]] && info "dry run: nothing will be written"

  if [[ -d /proc/acpi/button/lid ]]; then
    info "lid state: $(cat /proc/acpi/button/lid/*/state 2>/dev/null | awk '{ print $2 }' | paste -sd, -)"
  else
    info "no ACPI lid device found under /proc/acpi/button/lid (fine for non-laptops)"
  fi

  configure_logind
  configure_upower
  configure_acpid

  collect_users
  if [[ ${#USERS[@]} -eq 0 ]]; then
    section "desktop settings"
    info "no users detected; pass --user NAME or --all-users to configure desktop power managers"
  else
    for u in "${USERS[@]}"; do
      configure_user_desktops "$u"
    done
  fi

  section "done"
  if [[ $FAILURES -gt 0 ]]; then
    warn "$FAILURES change(s) failed; see above"
  else
    ok "all changes applied"
  fi
  info "verify: close the lid, then confirm SSH still answers and 'journalctl -b | grep -i suspend' shows nothing new"
  info "with the lid closed the fans have less airflow; check temperatures under sustained load"

  exit $(( FAILURES > 0 ? 1 : 0 ))
}

# Sourced, as check.sh does to test the helpers, this defines everything and
# runs nothing.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
