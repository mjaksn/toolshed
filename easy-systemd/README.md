# easy-systemd

A pair of bash scripts for keeping a command running in the background on a
Linux machine with systemd. `install.sh` turns a command line into a service
that starts now, starts again at every boot, and is restarted when it stops.
`uninstall.sh` takes it away again, and will only ever take away a service
that `install.sh` made.

```
sudo ./install.sh myapp -- python3 app.py --port 8080
sudo ./uninstall.sh myapp
```

## Installing

```
sudo ./install.sh [options] NAME -- COMMAND [ARG...]
```

NAME becomes `/etc/systemd/system/NAME.service`, and is what `systemctl` and
`journalctl` know it by. Everything after the `--` is the command, passed
exactly as typed: no shell runs it, so nothing is expanded or split a second
time. For pipes, redirection or variables, make the command a shell:

```
sudo ./install.sh backup -- /bin/sh -c 'rsync -a /data /mnt/backup >>/var/log/backup.log'
```

| Option | Meaning | Default |
| --- | --- | --- |
| `-d DIR` | working directory | the current directory |
| `-u USER` | user to run as | the user who ran `sudo`, else root |
| `-r POLICY` | when to restart: `always`, `on-failure` or `no` | `always` |
| `-s SECONDS` | wait before restarting | 5 |
| `-e KEY=VALUE` | set an environment variable; repeat for more | none |

`-r` also takes the other values systemd accepts for `Restart=`. Under any
policy but `no`, systemd never gives up restarting it, however often it fails.

An `-e` value arrives exactly as typed, spaces, quotes, `%` and `$` included.
Keep secrets out of it: anyone on the machine can read a unit file, and
`systemctl show` prints its environment to anyone who asks.

A command given as a relative path, such as `./run.sh`, is found relative to
the working directory. A bare name, such as `python3`, is looked up on root's
PATH, which under `sudo` is usually the short `secure_path` list rather than
your own; if it is not found, give the full path. The path it settled on is
printed, along with the commands for checking on the service.

Running `install.sh` again with the same NAME replaces that service with the
new settings and restarts it. It refuses a NAME already taken by a unit it did
not create, including the ones the system ships, so it cannot quietly replace
`ssh` or `cron`, and it refuses to write through a symbolic link.

The chosen user needs to be able to enter the working directory. Installing
from inside your home directory as another user, or as yourself from a
directory only root can read, gets a service that fails at once; `systemctl
status NAME` says so.

## Removing

```
sudo ./uninstall.sh NAME...
sudo ./uninstall.sh --all
./uninstall.sh
```

The first stops, disables and deletes the named services. The second does
that to every service `install.sh` created. The third lists them and changes
nothing.

## How it tells its own units apart

Every unit `install.sh` writes begins with this comment:

```
# Created by easy-systemd install.sh; uninstall.sh removes only units carrying this line.
```

`uninstall.sh` refuses any unit whose first line is not exactly that, and any
that is a symbolic link, and if one name in a list is refused, none of the
others are removed either. Deleting the line by
hand is the way to keep a unit from ever being touched by `uninstall.sh`.

## What it needs

bash, systemd, and root. Nothing to install. The only files either script
writes or deletes are its own unit files in `/etc/systemd/system`, and
`systemctl` does the rest.

## What it writes

For `sudo ./install.sh -u app -d /opt/app -e PORT=8080 myapp -- python3 app.py`,
roughly:

```ini
# Created by easy-systemd install.sh; uninstall.sh removes only units carrying this line.
[Unit]
Description=myapp (easy-systemd)
Wants=network-online.target
After=network-online.target
# Never stop restarting it, however often it fails.
StartLimitIntervalSec=0

[Service]
User=app
WorkingDirectory=/opt/app
Environment="PORT=8080"
ExecStart="/usr/bin/python3" "app.py"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Each argument and each variable is quoted, with `%`, `\` and `"` escaped, and
`$` too in the command, so systemd hands the program exactly what was typed.
The output goes to the journal: `journalctl -u myapp -f` follows it.

## Tests

```
bash ./check.sh
```

This is what CI runs, on `ubuntu-latest`. It works against the real systemd,
because what matters is what systemd makes of the unit files, so it needs a
machine booted with systemd, and it runs itself again under `sudo` if started
without root.

It installs, reinstalls, restarts and removes real services, and checks what
they end up running: the user, the directory, and the arguments and
environment the program receives, compared with the ones given. It also
checks every refusal described above. Every service it creates is named
`es-check-something` and is removed when it finishes, pass or fail, and it
will not start while any unit by such a name exists already. The
`--all` test is skipped on a machine that has services of its own from
`install.sh`, since `--all` would take those too.

It passes against systemd 259 on Ubuntu under WSL.
