# smtp-sink

An SMTP server for a LAN that accepts mail and never delivers it. It takes any
message from any sender to any recipient, appends the raw message with a short
envelope header to a log file, and optionally forwards a summary to a syslog
server and the whole message, as JSON, to a webhook. It never relays, never
authenticates, and keeps no mailboxes.

The use it was written for is a device that can only report by sending mail: a
printer, a NAS, a UPS, a switch. Point it here and the alerts land in a file
you can read, in whatever already watches your syslog, and in anything that
takes an HTTP request.

Python 3.11 or later, standard library only, nothing to install. Earlier
versions lack the hook the sink uses to survive a syslog server being down.

```
python smtp_sink.py --bind 192.168.1.50 --port 2525 --log alerts.log
python smtp_sink.py --bind 192.168.1.50 --syslog 192.168.1.10 --syslog-body
python smtp_sink.py --bind 192.168.1.50 --webhook-url https://hooks.example.test/mail --header "X-API-Key: abc123"
```

| Option | Effect |
| --- | --- |
| `--bind ADDRESS` | Address to listen on. Required, with no default, so the sink never ends up on every interface by omission. Usually this machine's LAN address; `0.0.0.0` means every interface, for when that is really what you want. |
| `--port PORT` | Port to listen on, 2525 by default. Port 25 needs root or `CAP_NET_BIND_SERVICE`. |
| `--log PATH` | File to append messages to, `smtp_sink.log` by default. |
| `--syslog HOST[:PORT]` | Also forward each message to this syslog server. Port 514 by default. An IPv6 address goes in brackets when it has a port, `[::1]:514`, and can go bare without one, `::1`. A colon with no port after it, or a bare string of colons that is not an IPv6 address, is refused at startup rather than guessed at. A server that is down, at startup or later, costs a line on stderr per message and is tried again with the next; it never stops the sink logging mail. Over TCP, a connect or send gives up after five seconds, so a server that silently drops packets delays startup by that much at most. |
| `--syslog-proto udp\|tcp` | Transport for the above, `udp` by default. |
| `--syslog-facility NAME` | Syslog facility, `local0` by default. |
| `--syslog-body` | Put the message body in the syslog line as well as the summary. Base64 and quoted-printable are decoded, and a multipart message contributes its first text/plain part rather than its boundaries and attachments. |
| `--syslog-max N` | Truncate syslog lines to N characters, 2000 by default. A line over the limit ends in an ellipsis, unless N leaves no room for one, in which case it is simply cut to N. A cut through the subject or body puts a closing quote after the ellipsis, so the field still ends where a parser expects. |
| `--webhook-url URL` | Also send each message to this URL, as the JSON object described below. `http` and `https` both work. The only option the webhook needs; the three below all require it, and are refused without it. A URL that is not http or https, has no host, has a bad port, or carries a user name and password is refused at startup. Credentials go in a header instead. |
| `--webhook-method METHOD` | `POST` by default, or `GET`, `PUT`, `PATCH` or `DELETE`, in either case. Every one but `GET` carries the JSON body; `GET` sends the request with no body at all, as a bare notification. |
| `--webhook-disable-ssl-verify` | Accept any certificate from an `https` URL, including a self-signed one or one for another name. Without it, the certificate is checked against the system's trusted roots and the host name, and a request that fails the check is not sent. |
| `--header "NAME: VALUE"` | Add this header to every webhook request. Give it once per header; a name given more than once is sent more than once. A header given here replaces the sink's own `User-Agent` or `Content-Type`. `Content-Length` and `Transfer-Encoding` are worked out from the body and cannot be given, and a value with a line break or a character outside Latin-1 is refused at startup. |

The syslog line carries the subject as text: a device that puts a degree sign
or an accent in one sends it as an RFC 2047 encoded word, and that is decoded
on the way out. A subject that cannot be decoded is forwarded as it arrived
rather than costing the whole line.

The subject and body sit between double quotes in that line, and both come
from whoever sent the message. A quote or backslash inside either is escaped
with a backslash, and a control character is written out as an escape like
`\x1b`, so neither can end its field early, fake a field of its own, or put an
escape sequence in front of whoever reads the collector's output. The sender
and recipients sit in the line unquoted, and a quoted local part can carry a
space or a quote of its own, so in those fields a quote is written as `\x22`
and a space as `\x20`, in the same style as the control characters.

The client gets its 250 once the message is in the file, before anything is
forwarded. Its connection then waits only while the message is parsed for the
syslog line, CPU time bounded by the size limit, which keeps each connection
to one message in flight. The syslog lines queue for a single sender, which
sends them in order, so a syslog server that is slow or has gone away never
holds up the mail, or leaves a client waiting long enough to send a message
twice. The queue holds 1000 lines; while it is full, a message still goes in
the file, and its forward is dropped with a line on stderr.

Without `--syslog` or `--webhook-url` nothing is forwarded and the log file is
the only record.

That file gets every message byte for byte as it arrived, whole, whatever the
syslog line was trimmed down to. Nothing in it is decoded and written back
out, so a device sending 8-bit text in some encoding of its own keeps its
bytes intact rather than having them replaced. One consequence worth knowing
before you point a tool at the file: the envelope lines the sink adds end in
LF and the messages between them keep the line endings they arrived with,
CRLF or a bare LF, so the endings are mixed by design. A message that cannot
be written to the file, because the disk is full or the file cannot be
opened, draws a 451 so the client knows to try again.

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

## The webhook

With `--webhook-url`, each message the sink logs is also sent to that URL as
one HTTP request, with a body of `Content-Type: application/json;
charset=utf-8` unless the method is `GET`. It looks like this, with `raw`
shortened:

```json
{
  "id": "6003a91b-42f4-4d3b-83ac-8df9f85a1b77",
  "received": "2026-09-26T01:09:03-05:00",
  "sink": "mailhost",
  "peer": {"address": "192.168.1.20", "port": 35140},
  "helo": "ups.lan",
  "envelope": {"from": "<ups@nas.test>", "to": ["<ops@lan.test>"]},
  "message": {
    "size": 323,
    "subject": "UPS on battery, 15°C",
    "from": "UPS <ups@nas.test>",
    "to": "ops@lan.test",
    "cc": null,
    "reply_to": null,
    "date": "Sat, 26 Sep 2026 01:09:03 -0500",
    "message_id": "<1@nas.test>",
    "content_type": "multipart/mixed",
    "headers": [
      {"name": "Subject", "value": "UPS on battery, 15°C"},
      {"name": "From", "value": "UPS <ups@nas.test>"}
    ],
    "text": "On battery.",
    "html": null,
    "attachments": [
      {"filename": "report.pdf", "content_type": "application/pdf",
       "disposition": "attachment", "size": 48213}
    ],
    "raw": "U3ViamVjdDogPT91dGYt..."
  }
}
```

| Field | What it holds |
| --- | --- |
| `id` | A random UUID, new for each message, for a receiver that wants to spot a duplicate. |
| `received` | When the message was logged, the same timestamp as its `Received:` line in the log file, so the two can be matched. |
| `sink` | The host name of the machine the sink runs on. |
| `peer` | The address and port the message came from. |
| `helo` | The name the client gave in HELO or EHLO, or null if it gave none: the first 255 characters of what it sent, control characters written out as escapes as in an address. |
| `envelope` | The sender from MAIL and the recipients from RCPT, exactly as the log file records them, ESMTP parameters dropped. A control character in an address stays written out as an escape, so a carriage return arrives as the two characters `\r`. |
| `message.size` | The message in octets, as received. |
| `message.subject`, `from`, `to`, `cc`, `reply_to`, `date`, `message_id` | Those headers as text, RFC 2047 encoded words decoded, or null for one the message does not have. |
| `message.content_type` | The message's own content type, such as `text/plain` or `multipart/mixed`. |
| `message.headers` | Every header of the message, in order, decoded the same way, a repeated one listed each time. |
| `message.text`, `message.html` | The first text/plain and first text/html part that is not an attachment, with base64 or quoted-printable undone and the part's charset decoded, or null if there is none. |
| `message.attachments` | Each part marked as an attachment, carrying a filename whatever its disposition, or holding a message of its own, such as an email forwarded as an attachment: its name, content type, disposition, and size in octets once decoded. An attached message is one entry, not walked into, and its size is that of the message written out again, which can differ a little from what arrived, for instance in line endings; a size that cannot be worked out is null. The content itself is in `raw`. |
| `message.raw` | The whole message exactly as received, in base64. It is the one field that loses nothing, since JSON cannot carry arbitrary bytes. |

A header sent as raw 8-bit text rather than as encoded words cannot be
decoded without knowing its charset, so bytes that are not UTF-8 arrive as
U+FFFD replacement characters there, and are intact in `raw`. The JSON is
ASCII throughout, anything else written as a `\u` escape.

Sending never holds up the mail. A message is queued for the webhook in the
same step that writes it to the file, and the request is made later from a
thread of its own, so the client's 250 never waits on the webhook, and a
webhook that is slow, down, or accepts the connection and never answers holds
up no SMTP client, whether the one that sent that message or any other.
Requests go one at a time, in the order messages were logged, even when two
clients finish at once, since the order of the queue is the order of the file.
Each connect, send and read gives up after 10 seconds without progress.
Looking up the host's name is not covered by that, so a URL naming a host
whose DNS server does not answer holds the sender for as long as the system
resolver takes. Messages wait for the sender in a queue of at most 1000
messages and 64 MB; while it is full, a message still goes in the file, and is
not sent, with a line on stderr.

A request that fails, whether it cannot connect, times out, or is answered
with anything but a 2xx status, costs one line on stderr, and the message is
not sent again. A redirect is not followed and counts as a failure, which
says what the URL should have been. Proxy settings in the environment are
ignored, so the request goes where `--webhook-url` says. The URL the sink
prints, at startup and on stderr, is cut to its scheme, host and port, since
plenty of webhook URLs carry their secret in the path, and a URL refused at
startup is not printed at all. Nor is a header value, even in the error for
one that is refused.

## A warning about where you point it

There is no authentication and no rate limiting, and anything that connects
gets its message logged. This is fine on a network you control and is an open
relay-shaped hole on one you do not, so bind it to a LAN address and leave it
off the public internet. That is why `--bind` has to be given. A transaction
takes at most 100 recipients, and the next draws a 452 while those already
taken still get the message. An address longer than 256 characters as
recorded, escapes included, draws a 501. Both limits keep one connection from
holding an envelope of unbounded size in memory until DATA. A MAIL or RCPT
argument over 1000 characters draws a 500 without being read for an address,
since picking one out of a line of megabytes would hold up every other client
while it happened. A message larger than 10 MB, counted in octets as the
`SIZE` the server advertises promises, is refused with a 552 and the
connection carries on. A single line of any length up to that limit is taken,
whatever RFC 5321 says about a thousand octets, because plenty of devices
ignore it; one line longer than the whole limit ends the connection instead of
drawing the 552. A client that goes quiet for five minutes is hung up on.

A webhook at a plain `http` URL gets every message, attachments and all, in
the clear, and so does anything between the sink and it. So does one at an
`https` URL with `--webhook-disable-ssl-verify`, to anyone able to stand in
for its server. Both are allowed, because a receiver on the same LAN is the
common case, but reaching anything further off is what `https` with checked
certificates is for.

## Tests

```
bash ./check.sh
```

That is the command CI runs, and it prints the interpreter version and then
runs the suite. `python -m unittest` from this directory does the same thing
if you would rather skip the shell.

The tests bind only the loopback address on a port the operating system picks,
so they need no network and no privileges, and they send mail nowhere. Nothing
contacts a real syslog server or a real webhook either. Most syslog tests
replace the module's logger with a mock, which is also how the case of no
syslog being configured is covered, and the tests for a server that is down
open their own listener on the loopback address, except the one for a server
that never answers, which fakes the connect timing out. The webhook tests run
a small HTTP server of their own on the loopback address, which records each
request and can be told to answer with an error, a redirect, or not at all.
They take a couple of seconds, or about six on Windows, where a refused
loopback connection takes two seconds and the syslog tests make two.
