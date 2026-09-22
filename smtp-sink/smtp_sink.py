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
from email.parser import Parser

HOSTNAME = socket.gethostname()
MAX_MESSAGE_BYTES = 10 * 1024 * 1024
IDLE_TIMEOUT = 300  # seconds a client may sit silent before we hang up

syslog = None  # logging.Logger, set up in main() if --syslog is given


def write_entry(log_path, peer, mail_from, rcpts, body):
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    with open(log_path, "a", encoding="utf-8", errors="replace") as f:
        f.write("=" * 78 + "\n")
        f.write(f"Received: {ts}\n")
        f.write(f"Peer:     {peer[0]}:{peer[1]}\n")
        f.write(f"From:     {mail_from}\n")
        f.write(f"To:       {', '.join(rcpts)}\n")
        f.write("-" * 78 + "\n")
        f.write(body)
        if not body.endswith("\n"):
            f.write("\n")
        f.write("\n")
        f.flush()


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
    msg = Parser().parsestr(body)
    raw_subject = msg.get("Subject")
    subject = "(no subject)" if raw_subject is None else header_text(str(raw_subject))
    subject = " ".join(subject.split())
    line = (
        f'peer={peer[0]} from={mail_from} to={",".join(rcpts)} '
        f'subject="{subject}"'
    )
    if include_body:
        text = " | ".join(piece.strip() for piece in message_text(msg).splitlines() if piece.strip())
        line += f' body="{text}"'
    if len(line) > max_len:
        line = line[: max_len - 3] + "..."
    try:
        syslog.info(line)
    except Exception as exc:
        print(f"syslog forward failed: {exc}", file=sys.stderr)


async def handle_client(reader, writer, args):
    peer = writer.get_extra_info("peername") or ("?", 0)

    async def send(line):
        writer.write((line + "\r\n").encode("ascii", "replace"))
        await writer.drain()

    async def readline():
        raw = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
        if not raw:
            raise ConnectionResetError
        return raw.decode("utf-8", "replace").rstrip("\r\n")

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
                mail_from = arg.partition(":")[2].strip() or arg
                rcpts = []
                await send("250 OK")
            elif verb == "RCPT":
                rcpts.append(arg.partition(":")[2].strip() or arg)
                await send("250 OK")
            elif verb == "DATA":
                if mail_from is None or not rcpts:
                    await send("503 Need MAIL and RCPT first")
                    continue
                await send("354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                size = 0
                while True:
                    dline = await readline()
                    if dline == ".":
                        break
                    if dline.startswith(".."):
                        dline = dline[1:]  # undo dot-stuffing
                    # Octets, not characters. The limit is advertised to the
                    # client as SIZE, which SMTP defines in octets, so counting
                    # decoded characters let a message of non-ASCII text run to
                    # several times the number that was promised. The line is
                    # measured after decoding rather than before, so a byte
                    # that had to be replaced counts as its replacement, which
                    # is near enough at this threshold.
                    size += len(dline.encode("utf-8", "replace")) + 1
                    if size > MAX_MESSAGE_BYTES:
                        chunks = None
                    if chunks is not None:
                        chunks.append(dline)
                if chunks is None:
                    await send("552 Message too large")
                else:
                    body = "\n".join(chunks)
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
