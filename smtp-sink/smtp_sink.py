#!/usr/bin/env python3
"""
Minimal LAN-only SMTP sink.

Accepts any message from any sender to any recipient, appends the raw
message (plus a small envelope header) to a log file, and optionally
forwards a summary (or the body too) to a syslog server. It never relays,
authenticates, or stores mailboxes. Standard library only.

Usage:
    python3 smtp_sink.py --bind 192.168.1.50 --port 2525 --log alerts.log \
        --syslog 192.168.1.10 --syslog-proto udp --syslog-body
"""

import argparse
import asyncio
import datetime
import logging
import logging.handlers
import socket
import sys
from email.errors import HeaderParseError
from email.header import decode_header, make_header
from email.parser import BytesParser

HOSTNAME = socket.gethostname()
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
IDLE_TIMEOUT = 300  # seconds a client may sit silent before we hang up

# The longest line the stream reader will hand over. asyncio's default is 64
# KiB, and a longer line raises inside readline, which drops the connection and
# loses the message. RFC 5321 caps a line at 1000 octets, but plenty of devices
# ignore that, and a message they send within the size limit should be logged,
# so any line that could belong to an acceptable message has to fit: the
# message limit itself, and its CRLF. One line longer than the whole limit still
# ends the connection rather than drawing a 552, but that message would have
# been refused anyway.
LINE_LIMIT = MAX_MESSAGE_BYTES + 2

# How many lines can wait for syslog. Each is at most `--syslog-max`
# characters, 2000 by default, so a full queue is a few megabytes. It holds
# finished lines rather than the messages they came from, which can be 10 MB
# apiece, because a stalled syslog server is exactly when it fills, and anyone
# on the network can send mail.
FORWARD_QUEUE_SIZE = 1000

syslog = None  # logging.Logger, set up in main() if --syslog is given
forwards = None  # asyncio.Queue of syslog lines, set up by start_forwarder()
forwarder = None  # the asyncio.Task draining it


def write_entry(log_path, peer, mail_from, rcpts, body):
    """Append one message to the log file, byte for byte as it arrived.

    `body` is bytes and is written without being decoded, because a device is
    free to send 8-bit text in whatever encoding it likes and this file is the
    only permanent record of what it said. Decoding it here to write it back
    out would turn anything that is not UTF-8 into replacement characters and
    lose the original for good.

    The envelope lines the sink adds around it end in LF while the message
    keeps its own CRLF, so the file has mixed line endings by design.
    """
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    header = (
        "=" * 78 + "\n"
        f"Received: {ts}\n"
        f"Peer:     {peer[0]}:{peer[1]}\n"
        f"From:     {mail_from}\n"
        f"To:       {', '.join(rcpts)}\n"
        + "-" * 78 + "\n"
    )
    with open(log_path, "ab") as f:
        f.write(header.encode("utf-8"))
        f.write(body)
        if not body.endswith(b"\n"):
            f.write(b"\n")
        f.write(b"\n")
        f.flush()


def visible(text):
    """Text with every control character written out as an escape.

    The envelope lines in the log are line-oriented, and the file is read with
    cat and tail. A carriage return inside an address would let a client forge
    a second From line, and an escape sequence would rewrite the terminal of
    whoever reads it. Shown as escapes rather than dropped, so the record still
    says what was sent. A backslash that arrived in the address is ambiguous
    with those escapes; that is accepted, since no real address carries one.
    """
    return "".join(
        c if c.isprintable() else c.encode("unicode_escape").decode("ascii")
        for c in text
    )


def quoted(text):
    """A value for one of the quoted fields in the syslog line.

    The subject and body come from the message, which anyone on the network can
    write, and they sit between double quotes in a line a collector parses by
    key. A quote inside one would end the field early and let the rest pose as
    a field of its own, so `subject: x" body="forged` could fake a body. Quotes
    and backslashes are escaped first, so a backslash that arrived stays
    distinguishable from the escapes `visible` then adds for control
    characters.
    """
    return visible(text.replace("\\", "\\\\").replace('"', '\\"'))


def closing_bracket(text, start):
    """Where the path opened by the `<` at `start` closes, or -1.

    A quoted local part may hold a `>` of its own, as `<"a>b"@x.test>` does, so
    the first one is not necessarily the end. Quoted strings are skipped, and
    inside one a backslash takes the character after it literally.
    """
    in_quotes = False
    i = start + 1
    while i < len(text):
        c = text[i]
        if in_quotes and c == "\\":
            i += 2
            continue
        if c == '"':
            in_quotes = not in_quotes
        elif c == ">" and not in_quotes:
            return i
        i += 1
    return -1


def envelope_path(arg):
    """The address out of a MAIL or RCPT argument, ESMTP parameters dropped.

    `MAIL FROM:<a@b.test> SIZE=1234 BODY=8BITMIME` is what a client sends the
    moment the server advertises SIZE, which this one does, so the parameters
    have to come off or they end up recorded as part of the address. Python's
    own smtplib appends `size=` to every message. The null sender, `<>`, is
    kept as it is, because it says something.

    The result goes to the log file, the syslog line and stdout, so control
    characters are escaped here, once, rather than at each of those.
    """
    rest = arg.partition(":")[2].strip() or arg
    start = rest.find("<")
    if start != -1:
        end = closing_bracket(rest, start)
        if end == -1:
            # An unbalanced quote leaves no bracket outside one. The first `>`
            # is the best guess at where a malformed path meant to stop.
            end = rest.find(">", start + 1)
        if end != -1:
            return visible(rest[start : end + 1])
    # No angle brackets. Anything after the first space is a parameter.
    return visible(rest.split(" ")[0])


def header_text(value):
    """A header as text, with any RFC 2047 encoded words decoded.

    A subject with a non-ASCII character in it arrives on the wire as
    something like `=?utf-8?B?VVBTIG9uIGJhdHRlcnk=?=`, which is unreadable in
    a syslog line and is what most devices send the moment a degree sign or an
    accent turns up. Anything that cannot be decoded is passed through as it
    came, because an unreadable subject beats a forward that did not happen.
    """
    try:
        return str(make_header(decode_header(value)))
    except (HeaderParseError, LookupError, UnicodeDecodeError, ValueError):
        return value


def part_text(part):
    """One non-multipart part as text, undoing base64 or quoted-printable."""
    payload = part.get_payload(decode=True)
    if payload is None:  # nothing to decode, so take it as it came
        return part.get_payload() or ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:  # a charset name Python does not know
        return payload.decode("utf-8", "replace")


def message_text(msg):
    """The readable text of a message, for the syslog line.

    A multipart message is mostly scaffolding: boundary markers, a header
    block per part, and the base64 of whatever was attached. None of that
    tells anyone what the device was reporting, and at a couple of thousand
    characters the line is truncated long before the text is reached, so the
    first text/plain part is what gets used and the rest is left in the log
    file, which keeps the message whole.
    """
    if msg.is_multipart():
        for part in msg.walk():
            if not part.is_multipart() and part.get_content_type() == "text/plain":
                return part_text(part)
        return ""
    return part_text(msg)


def syslog_line(peer, mail_from, rcpts, body, include_body, max_len):
    """The one line a message becomes in syslog, no longer than `max_len`."""
    msg = BytesParser().parsebytes(body)
    raw_subject = msg.get("Subject")
    subject = "(no subject)" if raw_subject is None else header_text(str(raw_subject))
    # A raw 8-bit subject, one sent without encoded words, comes back out of
    # the parser as surrogates. Those cannot be encoded again, and the syslog
    # handler would raise on the way out and lose the whole line, so they are
    # flattened to replacement characters here where only the subject suffers.
    subject = subject.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    subject = " ".join(subject.split())
    line = (
        f'peer={peer[0]} from={mail_from} to={",".join(rcpts)} '
        f'subject="{quoted(subject)}"'
    )
    if include_body:
        text = " | ".join(piece.strip() for piece in message_text(msg).splitlines() if piece.strip())
        line += f' body="{quoted(text)}"'
    if len(line) > max_len:
        # A bound under four characters leaves no room for the ellipsis, and a
        # negative one is not a length at all. Either way the line is simply
        # cut to fit, because a truncation that runs past the limit it was
        # given is worse than a line with nothing left in it.
        keep = max(max_len, 0)
        line = line[: keep - 3] + "..." if keep > 3 else line[:keep]
    return line


def send_syslog(line):
    """Hand one finished line to the syslog logger, if there is one."""
    if syslog is None:
        return
    try:
        syslog.info(line)
    except Exception as exc:
        print(f"syslog forward failed: {exc}", file=sys.stderr)


def start_forwarder():
    """Make the queue of lines waiting for syslog, and the task that drains it.

    The task is kept in a module global as well as returned, because the event
    loop holds tasks only weakly and one nobody holds can be collected while it
    is still running. There is no draining at shutdown: the sink stops on
    Ctrl-C, and whatever is still queued is in the log file already.
    """
    global forwards, forwarder
    forwards = asyncio.Queue(maxsize=FORWARD_QUEUE_SIZE)
    forwarder = asyncio.create_task(run_forwarder())
    return forwarder


async def run_forwarder():
    """Send queued lines to syslog one at a time, in the order they came."""
    while True:
        line = await forwards.get()
        try:
            # Off the loop: `syslog.info` is synchronous, and a TCP handler
            # whose server has gone away connects again inside `emit`, which
            # blocks. Only this task waits on that, never an SMTP client.
            await asyncio.to_thread(send_syslog, line)
        finally:
            forwards.task_done()


async def handle_client(reader, writer, args):
    peer = writer.get_extra_info("peername") or ("?", 0)

    async def send(line):
        writer.write((line + "\r\n").encode("ascii", "replace"))
        await writer.drain()

    async def readline_raw():
        raw = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
        if not raw:
            raise ConnectionResetError
        # One terminator, not every trailing CR and LF. `rstrip` would eat a
        # carriage return that belongs to the message.
        if raw.endswith(b"\r\n"):
            return raw[:-2]
        if raw.endswith(b"\n"):
            return raw[:-1]
        return raw

    async def readline():
        """A command line, as text. DATA reads bytes instead."""
        return (await readline_raw()).decode("utf-8", "replace")

    mail_from = None
    rcpts = []

    try:
        await send(f"220 {HOSTNAME} SMTP sink ready")
        while True:
            line = await readline()
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            arg = arg.strip()

            # A greeting starts the session over, envelope included, exactly
            # as RSET would (RFC 5321, 4.1.4). Otherwise MAIL and RCPT, a
            # second EHLO, then DATA would log the message against the old
            # envelope instead of refusing it.
            if verb == "HELO":
                mail_from, rcpts = None, []
                await send(f"250 {HOSTNAME}")
            elif verb == "EHLO":
                mail_from, rcpts = None, []
                await send(f"250-{HOSTNAME}")
                await send(f"250-SIZE {MAX_MESSAGE_BYTES}")
                await send("250 8BITMIME")
            elif verb == "MAIL":
                mail_from = envelope_path(arg)
                rcpts = []
                await send("250 OK")
            elif verb == "RCPT":
                # A recipient belongs to a transaction, and there is none until
                # MAIL (RFC 5321, 3.3). A 250 here would tell the client the
                # recipient was taken when DATA is going to be refused anyway.
                if mail_from is None:
                    await send("503 Need MAIL first")
                    continue
                rcpts.append(envelope_path(arg))
                await send("250 OK")
            elif verb == "DATA":
                if mail_from is None or not rcpts:
                    await send("503 Need MAIL and RCPT first")
                    continue
                await send("354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                size = 0
                while True:
                    dline = await readline_raw()
                    if dline == b".":
                        break
                    if dline.startswith(b".."):
                        dline = dline[1:]  # undo dot-stuffing
                    # The message in octets, its CRLF included, which is what
                    # SIZE promises the client and what the client's own
                    # `size=` parameter counts. Measuring decoded characters
                    # let a message of 8-bit text run well past the limit.
                    size += len(dline) + 2
                    if size > MAX_MESSAGE_BYTES:
                        chunks = None
                    if chunks is not None:
                        chunks.append(dline)
                if chunks is None:
                    await send("552 Message too large")
                else:
                    # Rebuilt exactly as it came off the wire, dot-stuffing
                    # undone and nothing else touched.
                    body = b"".join(chunk + b"\r\n" for chunk in chunks)
                    write_entry(args.log, peer, mail_from, rcpts, body)
                    print(f"[{peer[0]}] logged message from {mail_from} to {rcpts}")
                    # The message is safe in the file, so the client hears so
                    # now, before the forward. Kept waiting on a syslog server
                    # that has gone away, a client can time out and send the
                    # same message again, and the file would hold it twice.
                    await send("250 OK: queued")
                    if syslog is not None:
                        # Building the line parses the message, which is CPU
                        # work up to the size limit, so it runs in a thread;
                        # sending it is left to the forwarder. Either way this
                        # connection goes straight back to reading commands,
                        # whatever state the syslog server is in.
                        line = await asyncio.to_thread(
                            syslog_line, peer, mail_from, rcpts, body,
                            args.syslog_body, args.syslog_max,
                        )
                        try:
                            forwards.put_nowait(line)
                        except asyncio.QueueFull:
                            print(f"[{peer[0]}] syslog queue full, not forwarded;"
                                  " the message is in the log file", file=sys.stderr)
                mail_from, rcpts = None, []
            elif verb == "RSET":
                mail_from, rcpts = None, []
                await send("250 OK")
            elif verb == "NOOP":
                await send("250 OK")
            elif verb == "QUIT":
                await send("221 Bye")
                break
            elif verb in ("AUTH", "STARTTLS"):
                await send("502 Not supported")
            else:
                await send("500 Unrecognized command")
    except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
        pass
    except Exception as exc:  # keep one bad client from killing the server
        print(f"[{peer[0]}] error: {exc}", file=sys.stderr)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def syslog_address(text):
    """`--syslog` as a (host, port) pair, for argparse to call.

    An IPv6 address is full of colons, so one with a port goes in brackets, as
    in a URL: `[::1]:514`, or `[::1]` for the default port. A bare address with
    more than one colon, such as `::1`, can only be IPv6 without a port, and is
    taken that way. Anything else is `HOST` or `HOST:PORT`.
    """
    if text.startswith("["):
        host, bracket, rest = text[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise argparse.ArgumentTypeError(f"expected [HOST] or [HOST]:PORT, got {text!r}")
        port = rest[1:]
    elif text.count(":") > 1:
        host, port = text, ""
    else:
        host, _, port = text.partition(":")
    if not host:
        raise argparse.ArgumentTypeError(f"no host in {text!r}")
    if not port:
        return host, 514
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"not a port number: {port!r}")
    return host, int(port)


def shown_address(address):
    """A (host, port) pair written back out the way `--syslog` takes it."""
    host, port = address
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


class PatientSysLogHandler(logging.handlers.SysLogHandler):
    """A SysLogHandler that outlasts its syslog server being down.

    The standard one connects a TCP socket while it is being built and raises
    if nothing is listening, which stopped the sink from starting at all, so no
    mail was logged either. Once a TCP send has failed it also keeps the dead
    socket, so every later message fails on it and it never connects again.
    Here a failed connect or send costs one line on stderr, the socket is
    dropped, and the next message tries afresh.
    """

    def createSocket(self):
        try:
            super().createSocket()
        except OSError as exc:
            self.socket = None
            print(f"syslog {shown_address(self.address)}: {exc}", file=sys.stderr)

    def emit(self, record):
        if not self.socket:
            self.createSocket()
        if self.socket:
            super().emit(record)

    def handleError(self, record):
        # One line rather than the logging module's traceback per message.
        exc = sys.exc_info()[1]
        print(f"syslog {shown_address(self.address)}: {exc}", file=sys.stderr)
        if isinstance(exc, OSError) and self.socktype == socket.SOCK_STREAM and self.socket:
            self.socket.close()
            self.socket = None


def setup_syslog(args):
    """Build a logger that ships each record to the remote syslog server."""
    socktype = socket.SOCK_STREAM if args.syslog_proto == "tcp" else socket.SOCK_DGRAM
    handler = PatientSysLogHandler(
        address=args.syslog,
        facility=args.syslog_facility,
        socktype=socktype,
    )
    # RFC 3164 style line: "<pri>Mon DD HH:MM:SS host tag: message"
    handler.setFormatter(logging.Formatter(
        f"%(asctime)s {HOSTNAME} smtp_sink: %(message)s", datefmt="%b %d %H:%M:%S"
    ))
    logger = logging.getLogger("smtp_sink")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


async def main():
    global syslog
    ap = argparse.ArgumentParser(description="LAN-only SMTP sink that logs mail to a file and syslog")
    # No default. Anything that connects gets its message appended to a file,
    # with no authentication and no rate limit, so an address that quietly
    # meant every interface would put that on a public one too. A LAN address
    # is the usual answer, and 0.0.0.0 is still there for anyone who says so.
    ap.add_argument("--bind", required=True, metavar="ADDRESS",
                    help="address to listen on, usually this machine's LAN address; 0.0.0.0 means every interface")
    ap.add_argument("--port", type=int, default=2525, help="port to listen on (25 needs root or CAP_NET_BIND_SERVICE)")
    ap.add_argument("--log", default="smtp_sink.log", help="file to append messages to")
    ap.add_argument("--syslog", type=syslog_address, metavar="HOST[:PORT]",
                    help="forward each message to this syslog server (default port 514); IPv6 as [::1]:514")
    ap.add_argument("--syslog-proto", choices=("udp", "tcp"), default="udp")
    ap.add_argument("--syslog-facility", default="local0", help="syslog facility name, e.g. local0, mail, user")
    ap.add_argument("--syslog-body", action="store_true", help="include the message body in the syslog line, not just the summary")
    ap.add_argument("--syslog-max", type=int, default=2000, help="truncate syslog lines to this many characters")
    args = ap.parse_args()

    if args.syslog:
        syslog = setup_syslog(args)
        start_forwarder()

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, args), args.bind, args.port,
        limit=LINE_LIMIT,
    )
    print(f"SMTP sink listening on {args.bind}:{args.port}, logging to {args.log}"
          + (f", forwarding to syslog {shown_address(args.syslog)} ({args.syslog_proto})" if args.syslog else ""))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
