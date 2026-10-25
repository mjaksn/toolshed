# borgserver on Unraid

A worked example of the server side of [borg-docker](README.md) on Unraid:
the share the repositories live on, the container, client keys, and
`retention.sh` on a schedule. The README carries the general rules; this is
what they come to on Unraid.

Use it for a first setup, or to rebuild the server after an Unraid
reinstall. If the `borg` share survived with its contents, only the
container needs recreating: the repositories and client keys are all on the
share.

It needs Unraid with Community Applications, and root on its web UI and
shell.

## 1. Create the share, on the array only

Shares, then Add Share:

| Setting | Value |
| --- | --- |
| Name | `borg` |
| Primary storage | **Array** |
| Secondary storage | None |
| Export (SMB/NFS) | No |

Array only is not optional. A Borg repository is a live set of segment files
that a client expects to stay put while it holds the lock, so the mover
copying segments between cache and array during a backup is a corruption
risk, and a backup sitting on a single cache drive has no parity until it is
moved. Caching Borg writes brings no speed worth having.

If the share was created with a cache tier by mistake, drain it before
switching:

1. Stop the borgserver container.
2. Set Primary to Cache, Secondary to Array, and the mover action to move
   from cache to array.
3. Main, then Move Now. Wait for it to finish.
4. Check that `ls /mnt/cache/borg` is empty.
5. Set Primary to Array and remove Secondary.
6. Start the container.

## 2. Add the first client key

The container will not start its SSH daemon with no client keys, so on a
fresh share put one in place first. Run `setup.sh` on the first client; at
step 3 it prints the key and waits. Then, on Unraid:

```
mkdir -p /mnt/user/borg/sshkeys/clients
nano /mnt/user/borg/sshkeys/clients/<client>     # paste the key, one line
chown -R 99:100 /mnt/user/borg/sshkeys
chmod 600 /mnt/user/borg/sshkeys/clients/<client>
```

Every later client is one more file the same way, followed by a restart of
the container, since it builds `authorized_keys` only when it starts:

```
docker restart borgserver
```

Setting `SERVER_KEYS_DIR="/mnt/user/borg/sshkeys/clients"` and
`SERVER_KEY_OWNER="99:100"` in `borg-docker.env` makes `setup.sh` print
these exact paths.

## 3. Install the container

Apps, then search for **borgserver**, image `nold360/borgserver`.

| Field | Value |
| --- | --- |
| Repository | `nold360/borgserver:1.4@sha256:909359e0614964bf98ffe11378b91bf040baae0105fa1afeabfce94daf25f548` |
| Network | bridge |
| Host port | `2222` to container port `22` |
| `/backup` | `/mnt/user/borg/backup` |
| `/sshkeys` | `/mnt/user/borg/sshkeys` |
| `BORG_APPEND_ONLY` | `yes` |
| `BORG_ADMIN` | leave empty |
| `PUID` / `PGID` | `99` / `100` |

The repository is pinned by digest, for the reasons in the README; the
template's default of `latest` follows Debian's stable release and would
move to a new borg without asking. `PUID` and `PGID` of 99 and 100 are
Unraid's `nobody` and `users`, which is what everything else on the share is
owned by.

Leave `BORG_ADMIN` empty. `retention.sh` runs borg inside the container,
which needs no key at all, so with it empty there is no key anywhere on the
network that can delete history. The container logs a warning about that at
start; it is expected.

## 4. Keep the Appdata Backup plugin away from it

The Appdata Backup plugin stops containers and archives their mapped
volumes by default. For borgserver that means stopping the SSH endpoint
while clients are trying to push, and tarring every repository into a
second copy on the same disks each night.

In the plugin's settings for each container, **exclude borgserver
entirely**. If you prefer to keep it listed, set it not to be stopped and
exclude the `/mnt/user/borg` mappings. Either way, schedule the plugin well
clear of the clients' backup window; with the default 03:30 start and its
15 minutes of jitter, 02:00 works.

## 5. Check it

On Unraid:

```
docker exec borgserver borg --version
docker logs borgserver --tail 20          # shows each client key being added
ls -la /mnt/user/borg/sshkeys/clients/
```

On a client that has been set up:

```
sudo ssh -n -o BatchMode=yes borg-server true; echo $?
```

using the client's `SSH_ALIAS` in place of `borg-server`. Any status but 255
means the key was accepted. 255 with `Permission denied` means the key file
is wrong, misnamed, or the container was not restarted.

## 6. Retention on a schedule

Put the script and your settings on the share, where they survive a
reinstall of Unraid. It is run with `bash`, so the share need not allow
executing files:

```
mkdir -p /mnt/user/borg/borg-docker
cp retention.sh borg-docker.env /mnt/user/borg/borg-docker/
```

Then the passphrases, one file per client, in a root-only directory on the
share:

```
mkdir -p /mnt/user/borg/.retention
chmod 700 /mnt/user/borg/.retention
printf '%s' '<passphrase>' > /mnt/user/borg/.retention/<client>.pass
chmod 600 /mnt/user/borg/.retention/*.pass
```

with `RETENTION_PASSDIR="/mnt/user/borg/.retention"` in `borg-docker.env`.
`retention.sh` keeps its record of each client's archives there too, as
`<client>.archives`, which is how it notices archives a client has deleted.

Schedule it with the User Scripts plugin, from Community Applications: Add
New Script, name it `borg-retention`, and give it this body:

```bash
#!/bin/bash
bash /mnt/user/borg/borg-docker/retention.sh
```

Set the schedule to Custom, `0 5 * * 0`, which is Sundays at 05:00, after
the clients' backup window. Run it once by hand first with `--dry-run` added
to that line, then without, and read the output before trusting the
schedule.

## Rolling back

The container holds no state of its own. Remove it and recreate it from the
template; as long as `/mnt/user/borg` is intact, nothing is lost. Never
delete the share.
