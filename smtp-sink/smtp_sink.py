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

syslog = None  # logging.Logger, set up in main() if --syslog is given


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


def envelope_path(arg):
    """The address out of a MAIL or RCPT argument, ESMTP parameters dropped.

    `MAIL FROM:<a@b.test> SIZE=1234 BODY=8BITMIME` is what a client sends the
    moment the server advertises SIZE, which this one does, so the parameters
    have to come off or they end up recorded as part of the address. Python's
    own smtplib appends `size=` to every message. The null sender, `<>`, is
    kept as it is, because it says something.
    """
    rest = arg.partition(":")[2].strip() or arg
    start = rest.find("<")
    end = rest.find(">", start + 1)
    if start != -1 and end != -1:
        return rest[start : end + 1]
    # No angle brackets. Anything after the first space is a parameter.
    return rest.split(" ")[0]


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


def forward_syslog(peer, mail_from, rcpts, body, include_body, max_len):
    if syslog is None:
        return
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
        f'subject="{subject}"'
    )
    if include_body:
        text = " | ".join(piece.strip() for piece in message_text(msg).splitlines() if piece.strip())
        line += f' body="{text}"'
    if len(line) > max_len:
        # A bound under four characters leaves no room for the ellipsis, and a
        # negative one is not a length at all. Either way the line is simply
        # cut to fit, because a truncation that runs past the limit it was
        # given is worse than a line with nothing left in it.
        keep = max(max_len, 0)
        line = line[: keep - 3] + "..." if keep > 3 else line[:keep]
    try:
        syslog.info(line)
    except Exception as exc:
        print(f"syslog forward failed: {exc}", file=sys.stderr)


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

            if verb == "HELO":
                await send(f"250 {HOSTNAME}")
            elif verb == "EHLO":
                await send(f"250-{HOSTNAME}")
                await send(f"250-SIZE {MAX_MESSAGE_BYTES}")
                await send("250 8BITMIME")
            elif verb == "MAIL":
                mail_from = envelope_path(arg)
                rcpts = []
                await send("250 OK")
            elif verb == "RCPT":
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
                    forward_syslog(peer, mail_from, rcpts, body, args.syslog_body, args.syslog_max)
                    print(f"[{peer[0]}] logged message from {mail_from} to {rcpts}")
                    await send("250 OK: queued")
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


def setup_syslog(args):
    """Build a logger that ships each record to the remote syslog server."""
    host, _, port = args.syslog.partition(":")
    port = int(port) if port else 514
    socktype = socket.SOCK_STREAM if args.syslog_proto == "tcp" else socket.SOCK_DGRAM
    handler = logging.handlers.SysLogHandler(
        address=(host, port),
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
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (use your LAN IP to limit exposure)")
    ap.add_argument("--port", type=int, default=2525, help="port to listen on (25 needs root or CAP_NET_BIND_SERVICE)")
    ap.add_argument("--log", default="smtp_sink.log", help="file to append messages to")
    ap.add_argument("--syslog", metavar="HOST[:PORT]", help="forward each message to this syslog server (default port 514)")
    ap.add_argument("--syslog-proto", choices=("udp", "tcp"), default="udp")
    ap.add_argument("--syslog-facility", default="local0", help="syslog facility name, e.g. local0, mail, user")
    ap.add_argument("--syslog-body", action="store_true", help="include the message body in the syslog line, not just the summary")
    ap.add_argument("--syslog-max", type=int, default=2000, help="truncate syslog lines to this many characters")
    args = ap.parse_args()

    if args.syslog:
        syslog = setup_syslog(args)

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, args), args.bind, args.port
    )
    print(f"SMTP sink listening on {args.bind}:{args.port}, logging to {args.log}"
          + (f", forwarding to syslog {args.syslog} ({args.syslog_proto})" if args.syslog else ""))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
