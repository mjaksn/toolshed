# smtp-sink

An SMTP server for a LAN that accepts mail and never delivers it. It takes any
message from any sender to any recipient, appends the raw message with a short
envelope header to a log file, and optionally forwards a summary to a syslog
server. It never relays, never authenticates, and keeps no mailboxes.

The use it was written for is a device that can only report by sending mail: a
printer, a NAS, a UPS, a switch. Point it here and the alerts land in a file
you can read and in whatever already watches your syslog.

Python 3, standard library only, nothing to install.

```
python smtp_sink.py --bind 192.168.1.50 --port 2525 --log alerts.log
python smtp_sink.py --bind 192.168.1.50 --syslog 192.168.1.10 --syslog-body
```

| Option | Effect |
| --- | --- |
| `--bind ADDRESS` | Address to listen on. `0.0.0.0` by default, which is every interface; naming the LAN address instead is the way to keep it off the others. |
| `--port PORT` | Port to listen on, 2525 by default. Port 25 needs root or `CAP_NET_BIND_SERVICE`. |
| `--log PATH` | File to append messages to, `smtp_sink.log` by default. |
| `--syslog HOST[:PORT]` | Also forward each message to this syslog server. Port 514 by default. |
| `--syslog-proto udp\|tcp` | Transport for the above, `udp` by default. |
| `--syslog-facility NAME` | Syslog facility, `local0` by default. |
| `--syslog-body` | Put the message body in the syslog line as well as the summary. Base64 and quoted-printable are decoded, and a multipart message contributes its first text/plain part rather than its boundaries and attachments. |
| `--syslog-max N` | Truncate syslog lines to N characters, 2000 by default. |

The syslog line carries the subject as text: a device that puts a degree sign
or an accent in one sends it as an RFC 2047 encoded word, and that is decoded
on the way out. A subject that cannot be decoded is forwarded as it arrived
rather than costing the whole line.

Without `--syslog` nothing is forwarded and the log file is the only record.

That file gets every message byte for byte as it arrived, whole, whatever the
syslog line was trimmed down to. Nothing in it is decoded and written back
out, so a device sending 8-bit text in some encoding of its own keeps its
bytes intact rather than having them replaced. One consequence worth knowing
before you point a tool at the file: the envelope lines the sink adds end in
LF and the messages between them keep the CRLF they arrived with, so the
endings are mixed by design.

## A warning about where you point it

There is no authentication and no rate limiting, and anything that connects
gets its message logged. This is fine on a network you control and is an open
relay-shaped hole on one you do not, so bind it to a LAN address and leave it
off the public internet. A message larger than 10 MB, counted in octets as the
`SIZE` the server advertises promises, is refused with a 552 and the
connection carries on; a client that goes quiet for five minutes is hung up
on.

## Tests

```
bash ./check.sh
```

That is the command CI runs, and it prints the interpreter version and then
runs the suite. `python -m unittest` from this directory does the same thing
if you would rather skip the shell.

The tests bind only the loopback address on a port the operating system picks,
so they need no network and no privileges, and they send mail nowhere. Nothing
contacts a syslog server either: the module's logger is replaced with a mock,
which is also how the case of no syslog being configured is covered.
