# lid-ignore

A bash script that makes a laptop running Linux ignore its lid, so it can sit
closed on a shelf and keep working as a server. Closing the lid on a stock
install suspends the machine, and more than one thing has a say in that: the
login manager, UPower, acpid, and whichever desktop environment is installed,
each of which can override the one before. This script sets all of them, as
many as it finds, and reports what it did.

```
sudo ./lid-ignore.sh
sudo ./lid-ignore.sh --dry-run
```

## Running

```
sudo ./lid-ignore.sh [--dry-run] [--user NAME ...] [--all-users]
```

| Option | Meaning |
| --- | --- |
| `--dry-run` | report every change it would make, and make none |
| `--user NAME` | desktop settings for this user; repeat for more |
| `--all-users` | desktop settings for every local user with a home directory |
| `-h`, `--help` | print the usage |

Without `--user` or `--all-users`, the desktop settings go to the user who ran
`sudo` and to everyone with a logged-in session, root excepted.

It is safe to run again: every setting is checked before it is written, and a
setting already right is reported as such and left alone. It exits non-zero
only when a change failed to apply, in which case each failure is marked
`[FAIL]` in the output.

Each report line carries a tag: `[info]` for what it found, `[ ok ]` for a
setting already right, `[ set ]` for one it changed, `[would]` for one it would
change under `--dry-run`, `[warn]` for something to look at by hand, and
`[FAIL]` for a change that did not apply.

## What it sets

At the system level, always:

- **systemd-logind, or elogind** where systemd is absent: a drop-in at
  `/etc/systemd/logind.conf.d/99-ignore-lid.conf` (or the elogind
  equivalent) setting `HandleLidSwitch`, `HandleLidSwitchExternalPower` and
  `HandleLidSwitchDocked` to `ignore`. The drop-in wins over any setting in
  the main `logind.conf`, which is left as it is. logind is then restarted so
  the change takes effect at once, unless systemd is older than 240, where a
  restart can drop sessions; then it says to reboot instead.
- **UPower**, if installed: `IgnoreLid=true` in `/etc/UPower/UPower.conf`,
  followed by a restart of the service when it is running.
- **acpid and acpi-support**, if installed: `LID_SLEEP=false` in
  `/etc/default/acpi-support`. Any custom lid handler under `/etc/acpi` is
  reported for review rather than touched, since it may act on the lid in
  ways a setting cannot switch off.

Per user, for the desktop environments that keep their own lid setting and
override logind with it:

- **XFCE**: the three lid properties of xfce4-power-manager, through
  `xfconf-query`, and a restart of the manager when it is running.
- **GNOME, Budgie and Pantheon**: the lid keys of the power plugin through
  `gsettings`, where the installed version still has them; a modern GNOME has
  none and defers to logind, which is then reported.
- **Cinnamon** and **MATE**: their equivalent `gsettings` keys.
- **KDE Plasma**, 5 and 6: the lid action set to 0 in each power profile,
  as `LidAction` in `powerdevilrc` for Plasma 6 and as `lidAction` in
  `powermanagementprofilesrc` for Plasma 5, whichever is present, and a
  reload of powerdevil when the user has a session.
- **LXQt**: `lidWatcherEnabled=false` in `lxqt-powermanagement.conf`.
- **Anything else**: every remaining `gsettings` key with `lid` in its name is
  listed for manual review.

Commands run on a user's behalf run as that user, with the display and session
bus of a process of theirs when they are logged in, so the change lands in the
running session. A user who is not logged in has the settings written to their
files, which apply at the next login: the KDE and LXQt files directly, and the
xfconf and gsettings ones through a private bus started for each command with
`dbus-run-session`, because without a bus those tools report success and keep
nothing. Where `dbus-run-session` is not installed, those settings are skipped
and the output says so. Edits to a user's files are made as that user rather
than as root, so a link planted in their home cannot lead the script to write
anywhere else, and whatever it creates there is theirs from the start.

The last lines of the output say how to confirm it worked: close the lid, check
that SSH still answers, and check that the journal shows no new suspend. They
also say to watch the temperatures, since a closed lid leaves the fans less
air.

## What it needs

bash and root, on Linux. It expects systemd-logind or elogind, and without
either it warns and settles for what UPower and acpid can do. Nothing to
install: UPower, acpid and the desktop tools are each used when found and
skipped when not, and so is `dbus-run-session`, from the dbus package, which
the desktop settings of a user who is not logged in need. Nothing is changed
outside `/etc` and the named users'
configuration directories, and every file it writes is named in its output.

## Tests

```
bash ./check.sh
```

This is what CI runs, on `ubuntu-latest`. It runs the script for real,
because what matters is what lands in `/etc` and in a user's home, so it
needs a machine booted with systemd, and it runs itself again under `sudo`
if started without root.

It first tries the file helpers on files of their own: the drop-in writer,
the editor for `KEY=VALUE` files, and the INI editor, including the nested
groups KDE uses, the placement of a new key before the blank lines that end
its section, and a key that is a prefix of another. Then it runs the whole
script against a throwaway user named `lid-check`, whose home it seeds with
the files of a KDE, LXQt and XFCE user at once, and whose `xfconf-query` and
`gsettings` are stand-ins that record what they are told and who told them.
A dry run must change nothing, and a real run must write the drop-in where
logind reads it, restart logind, set every setting as the user and not as
root, on a private bus since the user has no session, list the settings its
filter leaves for review, and then, run again, change nothing and say so. An
edit to a user's file must run as that user, so a link in their home to a
file only root can write fails rather than writes.

On the way out, pass or fail, it puts every file in `/etc` back as it found
it, restarts logind again if the drop-in changed, and removes the user. It
will not start while a user or home directory named `lid-check` exists.

It passes against systemd 259 on Ubuntu under WSL.
