# borg-docker

Nightly, encrypted, append-only Borg backups of Docker hosts to a Borg server
reached over SSH. Two bash scripts:

- `setup.sh` runs once on each Docker host. It installs borg, works out what
  to back up, sets up the SSH key and the repository, and installs a systemd
  timer that each night stops the running containers, backs them up and starts
  them again. It is safe to run again, and re-running it is how a host's setup
  is changed or repaired.
- `retention.sh` runs on the server, where it prunes, compacts and checks
  every client's repository. Clients cannot delete anything, by design, so
  this is where old archives roll off.

The server it is written for is the
[borgserver](https://github.com/Nold360/docker-borgserver) container image,
which gives each client key its own repository directory and can hold every
client to append-only. Any Borg 1.x server over SSH will do for `setup.sh`;
`retention.sh` needs borgserver. [server-unraid.md](server-unraid.md) is a
worked example of running it on Unraid.

```
scp setup.sh borg-docker.env you@dockerhost:
ssh you@dockerhost
sudo ./setup.sh
```

`borg-docker.env` is your own settings file, made from
[borg-docker.env.example](borg-docker.env.example); without one, `setup.sh`
asks for the server's address. See [Settings](#settings).

## How it works

```
 Docker host (client, root)                    Borg server
 ──────────────────────────                    ─────────────────────────────
 systemd timer 03:30 (+0 to 15m jitter)
   └─ borg-backup.sh
        1. docker stop <all running>
        2. borg create ─── SSH, key auth ────▶ borgserver container
        3. docker start <same set>                └─ borg serve --append-only
                                                     └─ /backup/<client>/containers
```

Each host pushes its own backup. The server only ever receives, and nothing
on it has credentials to log in to a client.

### What gets backed up

The backup covers locations, not a hand-maintained list of volumes, so a new
project or volume is picked up without anyone remembering to add it.

| Path | What it is |
| --- | --- |
| `PROJECTS_DIR` | A directory of compose projects, when there is one. Found from the `PROJECTS_DIRECTORY` of an Arcane container, running or stopped, when that path exists on the host, else `/opt/arcane/projects` if it exists. Optional. |
| `VOLUMES_DIR` | Every named Docker volume. Found from `docker info`, normally `/var/lib/docker/volumes`. Each volume larger than `LARGE_VOLUME_MB` is asked about once, so a large one can be left out. A volume whose driver mounts something, an NFS share or a host directory bound with `o=bind`, is empty here once its containers stop, so `setup.sh` offers a bound volume's real directory as an extra one, and warns that anything else is not backed up. |
| `EXTRA_DIRS` | Anything else you said yes to. `setup.sh` looks at every container's bind mounts and every compose project's directory, and asks about each one the two above do not already cover. |

Not backed up: images (they can be pulled again), kernel and runtime paths
such as `/dev`, `/run` and the Docker socket, and the operating system
itself.

### Why containers are stopped

Databases and anything else with a write-ahead log cannot be copied
consistently while running. Stopping everything for the length of the copy,
seconds to a minute once the first archive exists, is the simplest way to
guarantee a restorable snapshot, and it also captures a multi-container
project (an app and its database) at one instant. The backup script records
which containers were running, stops exactly those, and starts exactly those
again in an `EXIT` trap, so they come back even if borg fails.

### Why Borg

- **Deduplication and compression.** Nightly archives of mostly unchanged
  data cost almost nothing after the first. Chunks are shared between
  archives.
- **Encryption at rest.** Repositories use `repokey-blake2`: the key lives in
  the repository, protected by a passphrase only the client and you hold. The
  server never sees plaintext.
- **Append-only.** The server runs `borg serve --append-only` for every
  client key. A compromised client, or ransomware on one, can add archives,
  and can remove archives from the list a repository shows, but cannot
  remove the data itself: that stays on disk until the server compacts the
  repository. `retention.sh` refuses to compact a repository that has lost
  archives it did not prune itself, so they stay recoverable until someone
  has looked; see [Retention](#retention).
- **Stock tooling.** One apt package on the client and one container on the
  server.

### Trust model

| Who | Can do |
| --- | --- |
| A client's key | Create archives in its own repository, and read it. It can also drop archives from the repository's list, but their data stays on disk, and `retention.sh` notices and stops before compacting it away. Nothing else. |
| Root on the server | Everything, through `docker exec` into the container: prune, compact, check, delete. Reading what is inside an archive also needs that client's passphrase. |
| Anyone with the backup files but no passphrase | Sees encrypted chunks only. |

### What is deliberately not done

- **No pruning from the client.** Append-only makes `borg prune` from a
  client record deletions that never free anything, which is the point.
  Retention is enforced from the server.
- **No admin key.** borgserver can exempt one named key, `BORG_ADMIN`, from
  append-only and give it every repository. That is a key on the network that
  can delete all history, and `retention.sh` does not need it, so leave
  `BORG_ADMIN` empty. The container prints a warning about that at start;
  it is expected.
- **No borgmatic.** Debian Bookworm and Trixie ship borgmatic versions with
  incompatible configuration formats; plain borg and a short wrapper avoid
  the fork.
- **No per-container stop and start.** All at once is simpler and consistent.
  A host that needs shorter downtime per service could group by the
  `com.docker.compose.project` label instead.

## What it needs

On each client:

- Debian or a derivative such as Raspberry Pi OS, since borg is installed
  with apt. Bookworm and Trixie are what it was written on.
- Docker, and systemd.
- Root, through `sudo`.
- Borg 1.x. Borg 2 has a different repository format and different commands,
  and `setup.sh` stops if it finds it.

On the server:

- The borgserver image, or any Borg 1.x server that each client can reach
  over SSH and that keeps each client to its own repository path.
- Shell access there, to add each client's public key.

## The server

The smallest borgserver that fits, as a compose file:

```yaml
services:
  borgserver:
    image: nold360/borgserver:1.4@sha256:909359e0614964bf98ffe11378b91bf040baae0105fa1afeabfce94daf25f548
    container_name: borgserver
    restart: unless-stopped
    ports:
      - "2222:22"
    environment:
      BORG_APPEND_ONLY: "yes"
    volumes:
      - ./borg/backup:/backup
      - ./borg/sshkeys:/sshkeys
```

The image is pinned by digest as well as tag. The `1.4` tag is rebuilt every
week with Debian's security updates, so the digest above is a known build,
not the latest; move it on deliberately, after checking the new one. Keep
the tag on borg 1.x: Borg 1.x clients and servers interoperate across minor
versions, and a 2.x on either side does not.

What the image expects:

- **One file per client** in `sshkeys/clients/`, named after the client,
  holding its `ssh-ed25519 ...` public key on one line, with no extension.
  The file name is the client's name, and that client can reach only
  `/backup/<name>/`.
- **At least one key before the first start.** The container refuses to
  start its SSH daemon with no client keys at all, so a fresh server gets its
  first key file before `docker compose up`.
- **A restart after any key change.** The container builds `authorized_keys`
  from that directory when it starts and not otherwise. To revoke a client,
  delete its file and restart; its repository stays on disk.
- **Ownership.** At every start it runs `chown -R` over `/backup` to its
  `borg` user, which is uid 1000 unless `PUID` and `PGID` say otherwise. Key
  files should be owned by that user too, mode `600`.

Keep the backup directory on storage with redundancy, and keep any other
backup job from stopping the container or copying its repositories while
clients are writing. [server-unraid.md](server-unraid.md) has the Unraid
version of both.

## Settings

Both scripts read a settings file, `borg-docker.env`, from beside the script,
or from wherever `--env FILE` says. It is sourced by bash, so every value is
quoted. A file that does not load cleanly, for instance a value with spaces
whose quotes went missing, stops either script before it does anything,
with bash's own message naming the line, rather than leaving that setting at
its default. `setup.sh` checks the host's `/etc/borg-backup.env` the same
way. [borg-docker.env.example](borg-docker.env.example) lists every
setting with a comment; copy it to `borg-docker.env` and fill in your own.
That file is the one place your own server address and paths live, and it is
ignored by git, so it can sit beside the scripts in a checkout and travel to
each host with them.

`setup.sh` takes each setting from the first of these that gives it a value:

1. the environment, as in `sudo BACKUP_TIME=02:30 ./setup.sh`;
2. the settings file;
3. what the previous run on this host recorded in `/etc/borg-backup.env`;
4. the default.

An empty value counts as no value. `retention.sh` takes the same order,
without the third.

| Setting | Default | Purpose |
| --- | --- | --- |
| `SERVER_HOST` | asked for | The Borg server's address |
| `SERVER_PORT` | `22` | Its SSH port |
| `SERVER_USER` | `borg` | The SSH user there |
| `SSH_ALIAS` | `borg-server` | The alias written to `/root/.ssh/config`, and the host part of the repository URL |
| `CLIENT_NAME` | the short host name | Must equal the key file's name on the server. It is also the first part of every archive name, which is what `retention.sh` prunes by. |
| `REPO_NAME` | `containers` | The repository's directory under `/backup/<client>/` |
| `REPO_PATH` | `/backup/<client>/<REPO_NAME>` | The full path on the server, for a server laid out differently. It must start with `/`. |
| `BACKUP_TIME` | `03:30` | When the timer starts, as `HH:MM`. Up to 15 minutes of jitter is added. |
| `BACKUP_TIMEOUT_MIN` | `240` | How many minutes `borg create` may run, with every container down, before it is stopped and they are started again |
| `PROJECTS_DIR` | found, see above | The compose projects directory, if any |
| `VOLUMES_DIR` | from `docker info` | Docker's volumes directory |
| `EXTRA_DIRS` | none | More paths to back up, separated by spaces |
| `SKIP_DIRS` | none | Paths you said no to, which are not asked about again |
| `BORG_EXCLUDES` | the volumes directory's `backingFsBlockDev` | Paths to leave out, separated by spaces |
| `KEPT_VOLUMES` | none | Names of large volumes you chose to keep, which are not asked about again |
| `LARGE_VOLUME_MB` | `1024` | The size in megabytes above which a volume is asked about |
| `SERVER_KEYS_DIR` | a placeholder | Where client keys go on the server; used only in the instructions it prints |
| `SERVER_KEY_OWNER` | none | Who should own a key file there, as `uid:gid`; likewise only printed |
| `BORGSERVER_CONTAINER` | `borgserver` | The container's name. `setup.sh` only prints it; `retention.sh` runs borg inside it. |
| `RETENTION_PASSDIR` | none, required | `retention.sh` only: the directory of `<client>.pass` files |
| `RETENTION_KEEP` | `--keep-daily 14 --keep-weekly 8 --keep-monthly 6` | `retention.sh` only: the prune rules |

The per host settings, `CLIENT_NAME` and everything from `PROJECTS_DIR` to
`LARGE_VOLUME_MB`, are remembered on each host after its first run, so they
rarely belong in a settings file shared between hosts. Paths are separated by
spaces, so a path containing a space cannot be listed; `setup.sh` says so and
skips any it finds.

## Setting up a client

Copy `setup.sh`, and your `borg-docker.env` if you have one, to the host and
run it as root:

```
scp setup.sh borg-docker.env you@dockerhost:
ssh you@dockerhost
sudo ./setup.sh
```

```
sudo ./setup.sh [--env FILE]
```

| Option | Meaning |
| --- | --- |
| `--env FILE` | read settings from FILE instead of `borg-docker.env` beside the script |
| `-h`, `--help` | print the usage |

Before the first step it prints the server, client and repository it is
going to use. If the host was set up before and the repository has changed,
because the server, client name or repository name changed, it shows the
old and new repository and asks before going on.

### What each step does

**1/8 borgbackup package.** Asks before running `apt-get install
borgbackup`, and installs nothing else. Stops if the installed borg is not
1.x.

**2/8 What to back up.** Settles the projects and volumes directories, then
looks at every container's bind mounts and compose project directories, and
at the host directory behind any volume bound to one, and asks, one at a
time, about each one not already covered. A volume mounted from anywhere
else, such as NFS, gets a warning instead: its directory is empty while the
containers are stopped, so it has to be backed up where it lives. Kernel and runtime
paths are left out without asking. A path you say yes to joins `EXTRA_DIRS`;
one you say no to joins `SKIP_DIRS` and is not asked about again. A
previously chosen path that no longer exists is dropped with a warning, and
one that another backed-up path now contains, such as a data directory
inside a compose project you have just said yes to, is dropped with a note,
so that nothing is given to borg twice.

Named volumes are backed up without a question, being under the volumes
directory, so it then asks about each volume larger than
`LARGE_VOLUME_MB`, showing its size and how much of it changed in the last
day. This question is the other way round from the one about directories:
it asks whether to **leave the volume out**, so yes excludes it, adding it to
`BORG_EXCLUDES`, and no keeps backing it up, adding its name to
`KEPT_VOLUMES`. Either way it is not asked about again, and neither is a
volume already excluded wholly or in part. The changed figure matters most:
borg reads every changed file in full on every run, while the containers
are down, so it is roughly what each night costs.

Rules of thumb for what to keep in the backup, whichever question is asking:

- Application state, such as databases, configuration and a Zigbee network's
  data: **keep it**. Yes to the directory question, no to the volume one.
- Large data that churns, such as video and logs: **leave it out**, and back
  it up some other way if it matters. That includes a database of many
  gigabytes written all day, such as a history or metrics store, and a cache
  that can be fetched again. No to the directory question, yes to the volume
  one.
- Sockets, cookies and small configuration files: keep them. They are tiny,
  and keeping them saves a step at restore time.

**3/8 SSH key.** Generates `/root/.ssh/borg_ed25519` if it is missing, and
tries it against the server. If the server already accepts it the step is
done. Otherwise it prints the public key and where it goes, and waits while
you add it and restart the container.

**4/8 SSH config.** Writes the alias's stanza in `/root/.ssh/config`. When a
stanza for the alias is already there and differs, it replaces it and keeps
the old file as `/root/.ssh/config.bak-borg-docker`. A stanza it writes goes
at the top of the file, since ssh takes the first value it finds for each
option and an earlier `Host *` would otherwise win; if the file began with
settings for every host, a `Host *` line after the stanza keeps them that
way. Then it connects
through the alias, and stops with ssh's own message if the key is refused.

**5/8 Passphrase and repository.** With no passphrase file, it first asks
whether this is a rebuild of a host whose repository already exists. If so,
it takes the saved passphrase. If not, it generates one into
`/root/.config/borg/passphrase`, mode 600, and shows it once: **save it in a
password manager before pressing Enter.** It then runs `borg init
--encryption=repokey-blake2` unless the repository already exists. On a host
that was set up before, a missing repository is a surprise, so it asks
before creating an empty one.

**6/8 Backup script.** Writes `/etc/borg-backup.env`, which holds every
setting, and installs `/usr/local/sbin/borg-backup.sh`. If an installed script
differs from the one it would install, it shows the diff and asks before
replacing it.

**7/8 systemd.** Installs `borg-backup.service`, a oneshot, and
`borg-backup.timer`, daily, persistent and jittered, and enables the timer.

**8/8 First backup.** Optional. Runs the service and lists the archives, or
shows the journal if it failed.

### Files it leaves behind

| Path | Purpose |
| --- | --- |
| `/root/.ssh/borg_ed25519`, and `.pub` | The client's SSH key |
| `/root/.ssh/config` | The alias |
| `/root/.config/borg/passphrase` | The repository passphrase |
| `/etc/borg-backup.env` | The repository URL, the paths, the excludes, and every other setting. **Edit this, not the script.** |
| `/usr/local/sbin/borg-backup.sh` | The backup wrapper |
| `/etc/systemd/system/borg-backup.service`, and `.timer` | The schedule |

### What a nightly run does

The backup script checks every path it is to back up. Any that no longer
exists is left out, and everything else is backed up, after which the run
exits 2 so that the timer shows a failure until the path is removed from the
env file. Borg's own exit status 1 means the archive was written with a
warning, usually a file that changed while it was being read; the script
logs that and counts it as success, so the journal carries the warning but
the run is not marked failed. Status 2 or more is an error, and is passed on.

If `/etc/borg-backup.env` does not load, say because a hand edit broke its
quoting, or the running containers cannot be listed, because Docker is not
answering, the run exits 2 before stopping anything or backing anything up.

A container started with `--rm` is left running and named in the journal,
because stopping one removes it, along with its anonymous volumes, and it
could not be started again. If any other container fails to stop, it may
still be writing, so the run starts again those that did stop and exits 2
without backing up. A container that fails to start again after the backup
also fails the run, with Docker's error in the journal, so an application
left down never hides behind a successful backup.

`borg create` has a deadline of `BACKUP_TIMEOUT_MIN` minutes, and its SSH
connection sends keepalives, so a server that stops answering ends the run
within a couple of minutes, and a backup that runs on far too long is
stopped at the deadline; either way the containers are started again and
the run fails. A first backup of a lot of data can need longer than the
default four hours; set `BACKUP_TIMEOUT_MIN` higher in
`/etc/borg-backup.env` for it, and back down afterwards.

Archives are named `<client>-<YYYY-MM-DDTHH:MM>`.

### Changing what is backed up

Edit `/etc/borg-backup.env`. The next backup reads it; nothing needs running
again.

```
sudo nano /etc/borg-backup.env
```

```
EXTRA_DIRS="/home/me/.esphome /etc/shairport-sync.conf"
BORG_EXCLUDES="/var/lib/docker/volumes/backingFsBlockDev /home/me/.esphome/.esphome/build"
```

Excludes are paths, read as Borg's `fm:` patterns, and one that matches
nothing is harmless. A path in `EXTRA_DIRS` must exist, as above. A path to
back up that is a symlink is followed, since borg would otherwise store the
link and not what it points to, and the archive holds it under the target's
path; write any exclude inside it against the target too.

To move a host to another server or repository, change the settings and run
`setup.sh` again rather than editing `BORG_REPO` by hand, since the SSH
alias has to follow.

### Removing a client

On the client:

```
sudo systemctl disable --now borg-backup.timer
sudo rm /etc/systemd/system/borg-backup.{service,timer} /usr/local/sbin/borg-backup.sh /etc/borg-backup.env
sudo systemctl daemon-reload
```

On the server, delete the client's key file and restart the container. Leave
its repository in place until you are sure nothing in it is needed.

## Retention

Clients push in append-only mode, so a prune from a client records
deletions and frees nothing. Borg run inside the borgserver container works
on the repository files directly, bypasses `borg serve`, and is the only
path that can actually delete. `retention.sh` runs it there, as the
container's `borg` user so that the files keep the ownership the clients
expect.

```
sudo ./retention.sh [--env FILE] [--dry-run] [--record]
```

| Option | Meaning |
| --- | --- |
| `--env FILE` | read settings from FILE instead of `borg-docker.env` beside the script |
| `--dry-run` | list what prune would delete, delete nothing, skip compacting, and leave the record of archives as it is |
| `--record` | only check each client's archives against its record and bring the record up to date; prune, compact and shut out nothing |
| `-h`, `--help` | print the usage |

For each client it prunes the archives named `<client>-*` by
`RETENTION_KEEP`, compacts the repository to free the space, runs `borg
check --repository-only`, and reports the size on disk. It finds the
repositories through the container's `/backup` mount, skips any that is
locked because a backup is running, does not compact after a prune that
failed, and exits non-zero if any step failed for any client. Only one run
at a time is allowed, through a lock on `.retention.lock` in
`RETENTION_PASSDIR`, which needs `flock` from util-linux on the server.

### Archives that disappear

Append-only stops a client deleting data, not deleting archives from the
repository's list of them. A client that runs `borg delete` or `borg prune`
removes the archives from that list, while the data they held stays on disk
until the repository is compacted, and compacting is what `retention.sh`
does. So each run records every client's archives, by ID as well as name,
in `<client>.archives` beside its passphrase file, and the next run
compares. An archive deleted and then made again under the same name has a
new ID, so it counts as gone. Borg allows a line break in an archive name,
which could make one archive's line pass for another's, so the IDs are also
listed on their own and must match the named listing exactly; if they do
not, the client is reported and nothing is pruned or compacted for it.
An archive that has gone since then, and that the script did not prune
itself, means a client deleted it, and that client is reported and skipped
entirely: no prune and no compact, until someone has looked. The run exits
non-zero, so a schedule shows it.

If the deletion was yours, remove `<client>.archives` and run again. If it
was not, stop that client's timer and roll the repository back to before
the deletion, following the append-only section of Borg's documentation for
`borg serve`, which explains how to use the repository's `transactions` log
to do it.

Borg holds its lock only for the length of each command, so a client could
otherwise delete an archive while `retention.sh` is running. To stop that,
each client is shut out while its repository is maintained: its line comes
out of the `authorized_keys` file the container builds from the key files,
the script checks that it has no session still open, and the line goes back
once that client is done, or when the script exits for any reason short of
being killed outright. Taking a key out stops new logins but not a
connection already made, and which client a connection belongs to cannot be
told from the server, so while any SSH session of the `borg` user is open,
usually a backup in progress, each client is skipped for that run. A backup that tries to start
while its client is shut out fails and says so in that client's journal,
which is one more reason to run retention well clear of the backup window.
If the script is ever killed outright, restarting the container rebuilds
the file from the key files, and the next run says so if it finds a line
missing.

As a second line of defence, the archives are listed again after prune, and
one gone that prune did not report removing stops the compact in the same
way; they are listed once more after compacting, and a loss found there is
reported and kept in the record, so the next run reports it again.

The first run for each client has no record to compare with, and says so.
A dry run compares and reports but never writes the record.

The record only knows about archives that existed at some run, so an
archive a client made and deleted again between two runs is never seen, and
the next compact frees its data. Run with `--record` every day, soon after
the backup window, to keep that gap short: it checks and updates the records
and changes nothing in the repositories, so it is quick and safe to
schedule. What remains is an archive made and deleted between one night's
backup and that morning's record run.

What this does not catch is a client that adds archives rather than
removing them. Archives created with a false `--timestamp` could push real
ones outside the keep rules, and prune would then remove them as it should.
Read the prune list now and then, and look twice at a run that prunes more
than it usually does.

### Passphrases

Prune reads each repository's manifest, which needs the passphrase. Keep them
in a root-only directory on the server, one file per client, named after it:

```
mkdir -p /srv/borg/.retention
chmod 700 /srv/borg/.retention
printf '%s' '<passphrase>' > /srv/borg/.retention/<client>.pass
chmod 600 /srv/borg/.retention/*.pass
```

Point `RETENTION_PASSDIR` at it. A client without a file is never pruned,
and each run names any such repository so it is not forgotten. The
passphrase reaches borg through Docker's environment, never on a command
line.

The trade-off, stated plainly: root on the server now has both the
encrypted repositories and their passphrases. That was close to true already,
since root there can take the repositories away, and the alternative, an
admin key on a client, puts a history-deleting credential on the machine
most likely to be compromised.

### Scheduling

Run it weekly, after the backup window, and with `--record` on the other
days, from root's crontab or whatever the server uses for scheduled jobs:

```
0 5 * * 0    bash /srv/borg/borg-docker/retention.sh
0 5 * * 1-6  bash /srv/borg/borg-docker/retention.sh --record
```

The weekly run checks the records as well, so it stands in for that day's
record run. Two runs at once are refused, so keep them at different times
if the schedule is changed.

Run it once by hand with `--dry-run` first, and then without, and read the
output before trusting the schedule. The default rules keep every day for two
weeks, every week for two months, and every month for six months;
`--keep-within 7d` is another rule worth knowing. While a rule has fewer
periods to fill than it asks for, as in a repository's first weeks, borg also
keeps the oldest archive, and the list marks it `daily[oldest]` or the like. Pruning only archives named
after the client keeps a stray archive made by hand under another name out of
the rules.

The next backup on each client notices the repository changed under it and
rebuilds its local chunk cache. That one run takes longer and logs a cache
message; nothing needs doing.

### A deeper check

`--repository-only` checks the segments. Once a month or so, check a
repository fully, which also checks every archive and needs the passphrase:

```
BORG_PASSPHRASE="$(cat /srv/borg/.retention/<client>.pass)" \
    docker exec -u borg -e BORG_PASSPHRASE borgserver borg check /backup/<client>/containers
```

Never schedule `--repair`. If a check fails, stop that client's timer, copy
the repository directory, and only then consider repairing it.

### Deleting a whole repository

Only for a client that is gone for good:

```
BORG_PASSPHRASE='...' docker exec -u borg -e BORG_PASSPHRASE borgserver borg delete /backup/<client>/containers
rm -rf <backup dir>/<client>
rm <sshkeys dir>/clients/<client> /srv/borg/.retention/<client>.pass
docker restart borgserver
```

## Day to day

Borg needs the repository URL, passphrase command and SSH command in its
environment, and all three are in `/etc/borg-backup.env`. Define this once,
in `~/.bashrc` for instance:

```bash
bb() { sudo bash -c 'set -a; . /etc/borg-backup.env; set +a; exec borg "$@"' bb "$@"; }
```

| Task | Command |
| --- | --- |
| List archives | `bb list` |
| Repository size and stats | `bb info` |
| Files in an archive | `bb list ::<archive>` |
| Only the directories | `bb list ::<archive> --short \| sed 's\|/[^/]*$\|\|' \| sort -u` |
| What changed between two archives | `bb diff ::<older> <newer>` |
| Run a backup now | `sudo systemctl start borg-backup.service` |
| Watch it | `sudo journalctl -u borg-backup.service -f` |
| The last run's log | `sudo journalctl -u borg-backup.service -n 40 --no-pager` |
| When the next run is | `systemctl list-timers borg-backup.timer` |
| Pause nightly backups | `sudo systemctl disable --now borg-backup.timer` |
| Resume them | `sudo systemctl enable --now borg-backup.timer` |
| What a backup would take | `bb create --dry-run --list ::test <the paths from the env file>` |

`::` on its own means the repository in `BORG_REPO`.

On the server, borg inside the container reaches every repository directly.
Repository-level commands need no passphrase; anything that reads archive
contents does:

```
R=/backup/<client>/containers
docker exec -u borg borgserver borg check --repository-only $R
docker exec -u borg borgserver borg break-lock $R      # only if a client died mid-backup
BORG_PASSPHRASE='...' docker exec -u borg -e BORG_PASSPHRASE borgserver borg list $R
docker logs borgserver --tail 50                       # logins, and the key rebuild at start
```

## Restores

Borg extracts paths relative to the current directory, without the leading
slash, and keeps ownership and permissions when run as root. Extracting from
`/` overwrites in place; extracting from `/tmp` gives a copy to look at.

A single file or directory, to look at:

```
cd /tmp
bb extract --list ::<archive> home/me/.esphome/secrets.yaml
ls -la /tmp/home/me/.esphome/
```

A named volume, in place:

```
docker stop <container>
cd /
bb extract --list ::<archive> var/lib/docker/volumes/<volume>/_data
docker start <container>
```

If the volume no longer exists, as on a fresh Docker install, `docker volume
create <volume>` first so that its `_data` directory exists, then extract
over it.

A bind-mounted directory, in place:

```
docker stop <container>
cd /
bb extract --list ::<archive> home/me/app/data
docker start <container>
```

An archive as a filesystem, to browse:

```
sudo mkdir -p /mnt/borg
sudo bash -c 'set -a; . /etc/borg-backup.env; set +a; borg mount ::<archive> /mnt/borg'
ls /mnt/borg
sudo umount /mnt/borg
```

`borg mount` needs FUSE support, from `python3-llfuse` or `python3-pyfuse3`
on Debian; if it complains, that package is what is missing.

### A host that is gone

1. Install the operating system and Docker, and whatever manages the compose
   projects, such as Arcane. Restore nothing yet.
2. Run `setup.sh` with the same settings. It generates a new SSH key: add it
   on the server under the same client name, replacing the old file. At step
   5 it asks whether this is a rebuild; say yes and paste the saved
   passphrase. It then finds the existing repository and does not create
   one. Say **no** to the backup at the end, and pause the timer with `sudo
   systemctl disable --now borg-backup.timer`, because nothing it found to
   back up exists yet.
3. Stop Docker itself, not only its containers, and extract the latest
   archive over `/`. The archive holds Docker's `volumes/metadata.db`, which
   the daemon keeps open while it runs, so it must not be running while that
   file is replaced:
   ```
   sudo systemctl stop docker.socket docker.service
   cd /
   bb extract --list ::<latest archive>
   sudo systemctl start docker.socket docker.service
   ```
4. Bring each project up, or let whatever manages them do it, and check
   them.
5. Run `setup.sh` again. The projects directory and bind mounts exist now,
   so step 2 finds them and asks about each one, and step 7 turns the timer
   back on. Check its list of what will be backed up against what the archive
   held:
   ```
   bb list ::<latest archive> --short | cut -d/ -f1-4 | sort -u
   ```
   Then run a backup, and compare its size with the archive you restored
   from, using `bb info ::<archive>`, before trusting the chain again.

## Troubleshooting

| Symptom | Cause, and fix |
| --- | --- |
| `Permission denied (publickey)` | The key file on the server is missing, misnamed, owned by the wrong user, or the container was not restarted after it was added. |
| `Connection refused` | The container is not running. Check that no other backup job stopped it. |
| `Host key verification failed` | The server's host key changed, usually because the container was recreated without its `sshkeys/host` directory. If you trust the change, remove the old entry with `sudo ssh-keygen -f /root/.ssh/known_hosts -R '[<host>]:<port>'`, or just `<host>` on port 22. |
| `Failed to create/acquire the lock` | A previous run died during a backup. Check that none is running, then `bb break-lock`, or `break-lock` from the server. |
| A container did not come back | The run fails with `not every container started again`, after Docker's own error. Check `docker ps -a`, fix the cause, then `docker start <name>`. The script restarts only what was running when it began. |
| The run failed with `no longer exist` | A path in `EXTRA_DIRS` was removed. Take it out of `/etc/borg-backup.env`. |
| `Cache, or information obtained from the security directory is newer than repository` | Usually after a prune on the server, or a repository restored from a snapshot. If you know why, `bb delete --cache-only` and run again; it rebuilds. |
| The timer shows no next run | `sudo systemctl enable --now borg-backup.timer` |
| The first backup after a prune is slow | Expected once, while the client rebuilds its chunk cache. |
| `/etc/borg-backup.env did not load cleanly` in the journal | The file was edited and a value lost its double quotes, or `BORG_REPO` went missing. Every value is quoted; the error above that line names the bad one. Nothing was stopped. |

## Things you must not lose

1. **Each client's repository passphrase.** It is in
   `/root/.config/borg/passphrase` on that client and is shown once, at
   setup. Keep a copy in a password manager. Without it the repository
   cannot be read, including after the client is reinstalled.
2. **The server's backup directory**, which holds every repository, and its
   `sshkeys` directory, which holds every client's public key and the
   server's host keys.
3. **Your `borg-docker.env`**, if you keep one, since it is not in version
   control. It holds nothing secret, but it is what makes the next host a
   one-command job.

Everything else, the client keys, the scripts and the timers, comes back by
running `setup.sh` again.
