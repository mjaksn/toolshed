# BasicUpsAdapter

An HTTP adapter for a CyberPower UPS on Linux. It reports what `pwrstat` knows
about the UPS, and it can shut the machine down. Both actions need a static key.

This is a reimplementation of two PHP files that did the same job behind nginx,
`ups.php` and `sdr.php`. The JSON that comes out is the same shape those served,
quirks included, so anything already pointed at the old endpoint keeps working.
What changed is listed under Differences below, and all of it is either the key
or a consequence of it.

Node with no dependencies, standard library only. Node 20 or newer, because the
tests use `node:test` and `node --test`, both stable from 20.

## Endpoints

| Method | Path | Key | What it does |
| --- | --- | --- | --- |
| `GET` | `/ups/status` | yes | Runs `pwrstat -status` and `pwrstat -config`, returns the parsed result |
| `POST` | `/ups/shutdown` | yes | Runs `systemctl poweroff` |
| `GET` | `/healthz` | no | `{"ok":true}` and nothing else |

The key goes in an `X-Api-Key` header. Not a query string: those are written to
access logs and kept in browser history, which is a poor place for a credential
that powers a machine off.

```
curl -H "X-Api-Key: $UPS_ADAPTER_KEY" http://127.0.0.1:8080/ups/status
curl -X POST -H "X-Api-Key: $UPS_ADAPTER_KEY" http://127.0.0.1:8080/ups/shutdown
```

A wrong or absent key gets `401` for every path except `/healthz`, including
paths that do not exist, so the key check cannot be used to discover what the
service serves.

## Running it

```
UPS_ADAPTER_KEY="$(openssl rand -base64 32)" node main.js
```

| Variable | Default | Notes |
| --- | --- | --- |
| `UPS_ADAPTER_KEY` | none | Required. At least 16 characters or it refuses to start |
| `PORT` | `8080` | |
| `HOST` | `127.0.0.1` | Loopback unless you say otherwise |

It binds to loopback by default, and reaching the network has to be an explicit
`HOST=0.0.0.0`. If you do that, put a reverse proxy with TLS in front: the key is
sent as a plain header and is readable in transit without it. The service says so
on startup when it is bound beyond loopback.

### Sudoers

`pwrstat` and `systemctl poweroff` both need root, so the account running this
needs three specific commands and nothing else. Three separate entries rather
than a wildcard, so the service cannot run `pwrstat` with other arguments or
`systemctl` against other units:

```
upsadapter ALL=(root) NOPASSWD: /usr/sbin/pwrstat -status
upsadapter ALL=(root) NOPASSWD: /usr/sbin/pwrstat -config
upsadapter ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
```

Check the paths against your own install with `command -v pwrstat` before
copying these in. A sudoers entry that names a command not at that path silently
fails to match.

No request data reaches any command. The three argument vectors are fixed
constants in `server.js` and run through `execFile`, which starts the binary
directly with no shell to interpret anything.

## The JSON

Two fixed top level keys, then a section, then the label and value pairs that
`pwrstat` prints as dotted lines:

```json
{
  "UPS State": {
    "Properties": { "Model Name": "CP1500PFCLCD", "Rating Power": "1000 Watt" },
    "Current UPS status": { "State": "Normal", "Battery Capacity": "100 %" }
  },
  "UPS Configuration": {
    "Daemon Configuration": { "Alarm ": "On", "Hibernate ": "Off" }
  }
}
```

That trailing space in `"Alarm "` is not a typo. The original stripped dots from
a label but not spaces, so wherever `pwrstat` writes `Alarm ......... On` with a
space before the dots, the space stays on the key. `pwrstat -config` is written
that way throughout, so most keys under `UPS Configuration` carry one, while
`UPS State` has none because its dots run straight on from the label. It is
preserved deliberately: removing it would be a silent break for anything reading
the old endpoint. There is a test pinning it.

## Differences from the PHP

- **The key.** There was none before. Anything that could reach the endpoint
  could read the UPS or shut the machine down.
- **Shutdown moved from `GET` to `POST`.** Anything that follows a link issues a
  `GET`, crawlers and browser prefetchers included, and a shutdown one stray
  fetch away is an accident waiting to happen. A `GET` now returns `405` with
  `Allow: POST`, so an old bookmark fails loudly instead of working.
- **Failures are reported.** `ups.php` returned an empty JSON array when
  `pwrstat` was missing, which reads as a UPS with nothing to say. This returns
  `500` and `{"error":"pwrstat failed"}`, with the underlying message written to
  the log and kept out of the response, since it can carry paths and sudo
  complaints.
- **`Content-Type` is set.** The PHP served JSON as `text/html`.
- **The shutdown response says something true.** `sdr.php` printed a status and
  output it never collected, so it always said `Returned with status  and output:`.
  This returns `202` and `{"status":"shutting down"}`.

## Tests

```
node --test
```

14 tests, and they pass. They cover the parser against realistic `pwrstat`
output, both inherited quirks, and the HTTP surface: a wrong key, a key that is a
prefix of the real one, that a `GET` on shutdown runs no command, that an
unauthenticated shutdown runs no command, and that a failing `pwrstat` does not
put its message in the response body.

Nothing shells out. The command runner is injected, so the suite needs neither
`pwrstat` nor a UPS and runs anywhere Node does. `check.sh` runs the same
command and is what CI runs, on `ubuntu-latest`, per `ci.json`.
