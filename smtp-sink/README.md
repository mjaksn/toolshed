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
| `--bind ADDRESS` | Address to listen on. Required, with no default, so the sink never ends up on every interface by omission. Usually this machine's LAN address; `0.0.0.0` means every interface, for when that is really what you want. |
| `--port PORT` | Port to listen on, 2525 by default. Port 25 needs root or `CAP_NET_BIND_SERVICE`. |
| `--log PATH` | File to append messages to, `smtp_sink.log` by default. |
| `--syslog HOST[:PORT]` | Also forward each message to this syslog server. Port 514 by default. |
| `--syslog-proto udp\|tcp` | Transport for the above, `udp` by default. |
| `--syslog-facility NAME` | Syslog facility, `local0` by default. |
| `--syslog-body` | Put the message body in the syslog line as well as the summary. Base64 and quoted-printable are decoded, and a multipart message contributes its first text/plain part rather than its boundaries and attachments. |
| `--syslog-max N` | Truncate syslog lines to N characters, 2000 by default. A line over the limit ends in an ellipsis, unless N leaves no room for one, in which case it is simply cut to N. |

The syslog line carries the subject as text: a device that puts a degree sign
or an accent in one sends it as an RFC 2047 encoded word, and that is decoded
on the way out. A subject that cannot be decoded is forwarded as it arrived
rather than costing the whole line.

The subject and body sit between double quotes in that line, and both come
from whoever sent the message. A quote or backslash inside either is escaped
with a backslash, and a control character is written out as an escape like
`\x1b`, so neither can end its field early, fake a field of its own, or put an
escape sequence in front of whoever reads the collector's output.

The client gets its 250 once the message is in the file, before anything is
forwarded, so a syslog server that is slow or has gone away never holds up the
mail or leaves a client waiting long enough to send it twice.

Without `--syslog` nothing is forwarded and the log file is the only record.

That file gets every message byte for byte as it arrived, whole, whatever the
syslog line was trimmed down to. Nothing in it is decoded and written back
out, so a device sending 8-bit text in some encoding of its own keeps its
bytes intact rather than having them replaced. One consequence worth knowing
before you point a tool at the file: the envelope lines the sink adds end in
LF and the messages between them keep the CRLF they arrived with, so the
endings are mixed by design.

The envelope records the address on its own. A client sends `MAIL
FROM:<a@b.test> SIZE=1234` once the server advertises `SIZE`, which it does,
and the parameters after the address are not part of it. An address whose
quoted local part holds a `>` of its own, such as `<"a>b"@b.test>`, is kept
whole. A control character in an address, such as a carriage return that
would forge a second `From:` line or an escape sequence that would rewrite the
terminal of whoever reads the file, is written out as an escape like `\r`
rather than passed through. A greeting, HELO or EHLO, starts the session over
and drops any envelope in progress, as RSET does, and a RCPT before any MAIL
is refused with a 503.

## A warning about where you point it

There is no authentication and no rate limiting, and anything that connects
gets its message logged. This is fine on a network you control and is an open
relay-shaped hole on one you do not, so bind it to a LAN address and leave it
off the public internet. That is why `--bind` has to be given. A message larger
than 10 MB, counted in octets as the `SIZE` the server advertises promises, is
refused with a 552 and the connection carries on. A single line of any length
up to that limit is taken, whatever RFC 5321 says about a thousand octets,
because plenty of devices ignore it; one line longer than the whole limit ends
the connection instead of drawing the 552. A client that goes quiet for five
minutes is hung up on.

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
