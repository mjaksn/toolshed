#!/usr/bin/env python3
"""
Minimal LAN-only SMTP sink.

Accepts any message from any sender to any recipient, appends the raw
message (plus a small envelope header) to a log file, and optionally
forwards a summary (or the body too) to a syslog server and the whole
message, as JSON, to a webhook. It never relays, authenticates, or stores
mailboxes. Standard library only.

Usage:
    python3 smtp_sink.py --bind 192.168.1.50 --port 2525 --log alerts.log \
        --syslog 192.168.1.10 --syslog-proto udp --syslog-body \
        --webhook-url https://hooks.example.test/mail --header "X-Token: abc"
"""

import argparse
import asyncio
import base64
import datetime
import http.client
import ipaddress
import json
import logging
import logging.handlers
import queue
import re
import socket
import ssl
import sys
import threading
import urllib.parse
import uuid
from email.errors import CharsetError, HeaderParseError
from email.header import decode_header, make_header
from email.message import Message
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

# The envelope is held in memory until DATA, and a command line may be as long
# as LINE_LIMIT, so without these one connection could pile up RCPT after RCPT
# of 10 MB apiece. A hundred recipients is the least RFC 5321 lets a server
# refuse at (4.5.3.1.8), and further ones draw a 452 while the transaction
# carries on with those already taken. The path limit is RFC 5321's 256 octets
# (4.5.3.1.3), applied to the address as stored, escapes included, because what
# it protects is memory rather than the letter of the RFC.
MAX_RECIPIENTS = 100
MAX_PATH = 256

# The longest MAIL or RCPT argument that is parsed at all. Finding the path and
# escaping it both walk the text a character at a time on the event loop, so a
# 10 MB argument held up every client for over a second before its 501. A
# longer one draws a 500 unread. RFC 5321 allows a command line of 512 octets
# (4.5.3.1.4), and the only parameters this server invites, SIZE and BODY, add
# a few dozen, so this is roomy for any client and still cheap to walk.
MAX_ARGUMENT = 1000

# How much of the HELO or EHLO argument is kept for the webhook. It is the
# client's name for itself, and a domain name is at most 255 octets (RFC 5321,
# 4.5.3.1.2). Cut before it is escaped, for the same reason as MAX_ARGUMENT.
MAX_HELO = 255

# How many lines can wait for syslog. Each is at most `--syslog-max`
# characters, 2000 by default, so a full queue is a few megabytes. It holds
# finished lines rather than the messages they came from, which can be 10 MB
# apiece, because a stalled syslog server is exactly when it fills, and anyone
# on the network can send mail.
FORWARD_QUEUE_SIZE = 1000

# Seconds a TCP syslog connect or send may take before it counts as failed. A
# server on the LAN answers in milliseconds, so this only ever runs out on one
# that is dropping packets, and it bounds how long that can delay startup or
# hold the forwarder's thread when the sink is stopped.
SYSLOG_TIMEOUT = 5

# Seconds a webhook connect, a read, or the sending of one WEBHOOK_CHUNK of
# the body may take before the request counts as failed. It bounds each step
# rather than the whole request, so a server that trickles its reply can hold
# the sender longer than this, but only the sender: SMTP clients never wait on
# it.
WEBHOOK_TIMEOUT = 10

# How much of the body goes to the socket at a time. A socket's timeout bounds
# a whole `sendall`, so a body sent in one would have to be through in
# WEBHOOK_TIMEOUT however steadily it was going. Sent in pieces, a body only has
# to keep moving at a piece per timeout, 6.4 KB a second.
WEBHOOK_CHUNK = 64 * 1024

# How much can wait for the webhook. Unlike the syslog queue this holds whole
# messages, up to 10 MB apiece, because the JSON is built from them by the
# sender rather than by the connection that received them. So the queue is
# capped by bytes as well as by count, and a message over either cap is still
# logged, just not sent.
WEBHOOK_QUEUE_SIZE = 1000
WEBHOOK_QUEUE_BYTES = 64 * 1024 * 1024

# The longest header value that is decoded, in characters; see header_text.
# Decoding this much takes a millisecond or two, so a message that is nothing
# but headers of encoded words, each just this long, takes a few seconds in
# all rather than hours. A real header is far shorter, and what is cut off is
# still in the log file and in the webhook's `raw`.
MAX_HEADER_TEXT = 4096

# How many header fields, and how many parts, one parse keeps, across the whole
# message and all its parts; see BoundedMessage. A real message has a few
# dozen headers and a handful of parts, a big one some hundreds of each.
MAX_PARSED_HEADERS = 5000
MAX_PARSED_PARTS = 1000

# And how many parse defects, the parser's notes on lines it could not read as
# headers. A real message has none or a few; nothing here reads them at all.
MAX_PARSED_DEFECTS = 100

# The headers the email package splits into parameters; see BoundedMessage.
PARAM_HEADERS = {"content-type", "content-disposition"}

# A stretch of a header that is not ASCII, captured so that splitting on it
# keeps it; see header_value.
NON_ASCII_RUN = re.compile(r"([^\x00-\x7f]+)")

# A line break that folds a header onto the next line, which starts with a
# space or tab (RFC 5322, 2.2.3). A bare LF counts as well as CRLF, since the
# sink keeps whatever line endings a message arrived with.
FOLD = re.compile(r"\r?\n(?=[ \t])")

# RFC 9110's token, the characters a header name may be made of.
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")

# Headers the sink works out for itself on every request. One given with
# --header would contradict the body actually sent, or its framing.
COMPUTED_HEADERS = {"content-length", "transfer-encoding"}

# The methods --webhook-method takes. Every one but GET carries the JSON body.
WEBHOOK_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")

# Held while a message is appended. The writes run in worker threads, off the
# event loop, and two messages finishing together must not interleave. It is
# also held while the message is queued for the webhook, so the queue's order
# is the file's; reentrant, because `log_message` holds it around a
# `write_entry` that takes it too.
log_lock = threading.RLock()

syslog = None  # logging.Logger, set up in main() if --syslog is given
forwards = None  # asyncio.Queue of syslog lines, set up by start_forwarder()
forwarder = None  # the asyncio.Task draining it
webhook = None  # WebhookSender, set up in main() if --webhook-url is given


def log_message(log_path, peer, mail_from, rcpts, body, received, delivery=None):
    """Append one message to the log, then queue `delivery` for the webhook.

    Both happen under `log_lock`, which is what makes the webhook's order the
    log file's. Queued anywhere later, a message logged first could still be
    queued second, by a client that took longer over its 250 or its syslog
    line. Returns whether the webhook took it, or None with no webhook.
    """
    with log_lock:
        write_entry(log_path, peer, mail_from, rcpts, body, received)
        if delivery is None or webhook is None:
            return None
        return webhook.submit(delivery)


def timestamp():
    """Now, as the log file and the webhook both write it."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_entry(log_path, peer, mail_from, rcpts, body, received=None):
    """Append one message to the log file, byte for byte as it arrived.

    `body` is bytes and is written without being decoded, because a device is
    free to send 8-bit text in whatever encoding it likes and this file is the
    only permanent record of what it said. Decoding it here to write it back
    out would turn anything that is not UTF-8 into replacement characters and
    lose the original for good.

    The envelope lines the sink adds around it end in LF while the message
    keeps its own CRLF, so the file has mixed line endings by design.

    `received` is the time to record, now if not given. The handler passes
    the one it gives the webhook, so the two records of a message agree.
    """
    header = (
        "=" * 78 + "\n"
        f"Received: {received or timestamp()}\n"
        f"Peer:     {peer[0]}:{peer[1]}\n"
        f"From:     {mail_from}\n"
        f"To:       {', '.join(rcpts)}\n"
        + "-" * 78 + "\n"
    )
    entry = header.encode("utf-8") + body
    if not body.endswith(b"\n"):
        entry += b"\n"
    entry += b"\n"
    with log_lock, open(log_path, "ab") as f:
        f.write(entry)
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


def bare(text):
    """A value for one of the unquoted envelope fields in the syslog line.

    The addresses come from the client as well, and a quoted local part can
    carry spaces and quotes, so `<x subject="forged">` would otherwise add a
    field of its own. The value has been through `visible` already, so the
    quote and the space are written out in the same style as its escapes
    rather than with a backslash of their own, which would double every
    backslash `visible` wrote.
    """
    return text.replace('"', "\\x22").replace(" ", "\\x20")


def open_quote(text):
    """Whether `text` ends inside a quoted field, and inside an escape there."""
    inside = escaped = False
    for c in text:
        if escaped:
            escaped = False
        elif inside and c == "\\":
            escaped = True
        elif c == '"':
            inside = not inside
    return inside, escaped


def truncate(line, max_len):
    """`line` cut to `max_len`, a quoted field it cuts through closed again.

    With the body included, the cut nearly always lands inside it, and a field
    left open is one a strict key="value" parser rejects outright.
    """
    # A bound under four characters leaves no room for the ellipsis, and a
    # negative one is not a length at all. Either way the line is simply cut
    # to fit, because a truncation that runs past the limit it was given is
    # worse than a line with nothing left in it.
    keep = max(max_len, 0)
    if keep <= 3:
        return line[:keep]
    cut = line[: keep - 3]
    if not open_quote(cut)[0]:
        return cut + "..."
    # One character fewer, to make room for the closing quote.
    cut = line[: keep - 4]
    inside, escaped = open_quote(cut)
    if not inside:
        # The character given up was the field's opening quote.
        return cut + "..."
    if escaped:
        # Half an escape. Left in, it would take the first dot of the
        # ellipsis as `\.`, which a strict parser refuses.
        cut = cut[:-1]
    return cut + '..."'


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
    _, colon, rest = arg.partition(":")
    rest = rest.strip() if colon else arg
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

    Only the first MAX_HEADER_TEXT characters are decoded, and a longer value
    ends in an ellipsis. Decoding takes time that grows with the square of the
    length, so one header of encoded words the size of a whole message would
    hold the thread doing it for hours.
    """
    cut = len(value) > MAX_HEADER_TEXT
    if cut:
        value = value[:MAX_HEADER_TEXT]
    try:
        text = str(make_header(decode_header(value)))
    # CharsetError is a charset name that is not ASCII, which an encoded word
    # can only carry once 8-bit bytes have been read into it.
    except (CharsetError, HeaderParseError, LookupError, UnicodeDecodeError, ValueError):
        text = value
    return text + "..." if cut else text


class BoundedDefects(list):
    """A Message's list of parse defects, keeping only MAX_PARSED_DEFECTS a parse.

    Shares BoundedMessage's budget, so the limit is across every part.
    """

    def __init__(self, budget):
        super().__init__()
        self.budget = budget

    def append(self, defect):
        if self.budget["defects"] <= 0:
            self.budget["dropped"] = True
            return
        self.budget["defects"] -= 1
        super().append(defect)


class BoundedMessage(Message):
    """A Message whose parameter headers are read no longer than MAX_HEADER_TEXT.

    The email package reads a boundary, a charset or a filename by splitting
    Content-Type or Content-Disposition into parameters, and the way it splits
    takes time that grows with the square of the length. The parser itself
    does it for the boundary of every multipart part, so a 10 MB Content-Type
    of parameters held the thread parsing it for a quarter of an hour. Both
    headers are read through `get`, so cutting them here bounds all of that. A
    real one is a few dozen characters, and the header is whole in `raw`.

    They are also read from the raw value, as header_value reads the rest. The
    ordinary `get` hands back a header holding raw 8-bit text as an
    unknown-8bit Header with every high byte replaced, so a filename sent as
    raw UTF-8 lost every accented letter it had.

    And each parse has a budget, `budget`, set by parse_message: once it has
    kept MAX_PARSED_HEADERS header fields or MAX_PARSED_PARTS parts, the rest
    are dropped as they arrive. A 10 MB message of five-byte headers otherwise
    became two million of them held at once, a third of a gigabyte, and the
    webhook's list of them a JSON body seven times the message. Dropping any
    marks the budget `dropped`, which the webhook reports as `incomplete`.

    The parser also keeps a defect for every header line it cannot make
    sense of, a bare colon or a stray `From ` line, and appends those straight
    to `defects` rather than through anything above, so ten megabytes of them
    held over a gigabyte. `defects` is a BoundedDefects on the same budget.
    """

    budget = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.budget is not None:
            self.defects = BoundedDefects(self.budget)

    def set_raw(self, name, value):
        if self.budget is not None:
            if self.budget["headers"] <= 0:
                self.budget["dropped"] = True
                return
            self.budget["headers"] -= 1
        super().set_raw(name, value)

    def attach(self, payload):
        if self.budget is not None:
            if self.budget["parts"] <= 0:
                self.budget["dropped"] = True
                return
            self.budget["parts"] -= 1
        super().attach(payload)

    def get(self, name, failobj=None):
        if name.lower() not in PARAM_HEADERS:
            return super().get(name, failobj)
        for key, value in self.raw_items():
            if key.lower() == name.lower():
                if any("\udc80" <= c <= "\udcff" for c in value):
                    value = clean(value)
                return value[:MAX_HEADER_TEXT]
        return failobj


def parse_message(body):
    """`body` parsed into BoundedMessage parts, for syslog and the webhook.

    The message comes back with `incomplete` set when its budget ran out and
    headers or parts were left out.
    """
    budget = {
        "headers": MAX_PARSED_HEADERS, "parts": MAX_PARSED_PARTS,
        "defects": MAX_PARSED_DEFECTS, "dropped": False,
    }
    budgeted = type("BudgetedMessage", (BoundedMessage,), {"budget": budget})
    msg = BytesParser(_class=budgeted).parsebytes(body)
    msg.incomplete = budget["dropped"]
    return msg


def part_text(part):
    """One non-multipart part as text, undoing base64 or quoted-printable."""
    payload = part.get_payload(decode=True)
    if payload is None:  # nothing to decode, so take it as it came
        return part.get_payload() or ""
    try:
        charset = part.get_content_charset() or "utf-8"
    # The charset given both whole and in RFC 2231 pieces, as in
    # `charset*=utf-8''x; charset*0=a`, which the email package's own sort of
    # the pieces trips over.
    except (TypeError, ValueError):
        charset = "utf-8"
    try:
        return payload.decode(charset, "replace")
    # A charset name Python does not know, a codec such as idna that refuses
    # the "replace" error handler outright, or a name with a NUL in it, which
    # the codec lookup refuses as a ValueError.
    except (LookupError, UnicodeError, ValueError):
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
    msg = parse_message(body)
    raw_subject = msg.get("Subject")
    subject = "(no subject)" if raw_subject is None else header_text(str(raw_subject))
    # A raw 8-bit subject, one sent without encoded words, comes back out of
    # the parser as surrogates. Those cannot be encoded again, and the syslog
    # handler would raise on the way out and lose the whole line, so they are
    # flattened to replacement characters here where only the subject suffers.
    subject = subject.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    subject = " ".join(subject.split())
    line = (
        f'peer={peer[0]} from={bare(mail_from)} to={",".join(bare(r) for r in rcpts)} '
        f'subject="{quoted(subject)}"'
    )
    if include_body:
        text = " | ".join(piece.strip() for piece in message_text(msg).splitlines() if piece.strip())
        line += f' body="{quoted(text)}"'
    if len(line) > max_len:
        line = truncate(line, max_len)
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


def clean(text):
    """`text` with no lone surrogates, so it can be encoded as UTF-8.

    A header sent as raw 8-bit rather than as encoded words comes out of the
    parser holding surrogates. `json.dumps` writes those as `\\udcxx` escapes,
    which a strict receiver refuses along with the whole request, so they are
    flattened to replacement characters, as the syslog subject is.
    """
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:  # a surrogate surrogateescape did not make
        raw = text.encode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


def header_value(value):
    """One header's value, as `raw_items` gives it, as readable text.

    From `raw_items` rather than `get`, because for a header holding raw 8-bit
    text `get` hands back an unknown-8bit Header whose text has every high
    byte replaced already, valid UTF-8 included. The raw value still holds the
    bytes, as surrogates, and `clean` reads them as UTF-8, which is what a
    device sending raw 8-bit nearly always means, replacing only what is not.

    Such a value can still hold encoded words beside the raw text, and those
    are ASCII by definition, but `decode_header` gives up on a value holding
    anything else and leaves all of it encoded. So once it is text, each run
    of ASCII in it is decoded on its own and the rest is left as it is, after
    the same cut to MAX_HEADER_TEXT `header_text` makes.

    A value folded across lines is unfolded first, as RFC 5322 has it: each
    line break before a space or tab goes, the space stays. Decoding encoded
    words unfolds as a side effect, so without this the same subject arrived in
    two shapes depending on how it was encoded.
    """
    value = FOLD.sub("", value)
    if not any("\udc80" <= c <= "\udcff" for c in value):
        return clean(header_text(value))
    # Read as text first and cut after, so the limit counts characters, as it
    # does everywhere else, rather than the bytes they arrived as, and never
    # falls in the middle of one. Reading it is linear in the length; only the
    # decoding below needs the limit.
    text = clean(value)
    cut = len(text) > MAX_HEADER_TEXT
    text = text[:MAX_HEADER_TEXT]
    # Cleaned again once decoded: an encoded word can name a codec such as
    # unicode-escape, which turns `\ud800` into the lone surrogate itself.
    decoded = clean("".join(
        ascii_run_text(run) if run.isascii() else run
        for run in NON_ASCII_RUN.split(text)
    ))
    return decoded + "..." if cut else decoded


def ascii_run_text(run):
    """A run of ASCII from a header, its encoded words decoded, its edges kept.

    `decode_header` drops the space at either end of what it is given, and
    here that space is what separates the run from the raw text beside it.
    """
    core = run.strip()
    if not core:
        return run
    start = run.index(core)
    return run[:start] + header_text(core) + run[start + len(core):]


def decoded_header(msg, name):
    """One header as readable text, or None if the message has none."""
    for key, value in msg.raw_items():
        if key.lower() == name.lower():
            return header_value(value)
    return None


def content_parts(msg):
    """Every part that holds content, in order, walking only into multipart.

    `walk` goes into an attached message as well, since a message/rfc822 part
    holds a message of its own, and then that message's parts pass for the
    outer one's: its text becomes the text, and the attachment itself is lost.
    Here an attached message is one part, like any other attachment, and so
    is a multipart part that is itself attached, marked so or given a name:
    what is inside it belongs to the attachment, not to the message. The
    message itself is walked into whatever its headers say.
    """
    found = []
    pending = [msg]
    while pending:
        part = pending.pop()
        container = part.get_content_maintype() == "multipart" and part.is_multipart()
        if container and (part is msg or not is_attachment(part)):
            pending.extend(reversed(part.get_payload()))
        else:
            found.append(part)
    return found


def attachment_name(part):
    """A part's filename, or None. A malformed RFC 2231 name is not fatal.

    `get_filename` decodes RFC 2231, the standard way to put a non-ASCII name
    in a parameter, but Gmail and Outlook use RFC 2047 encoded words there
    instead, which it leaves alone, so those are decoded here.
    """
    try:
        name = part.get_filename()
    except (HeaderParseError, LookupError, UnicodeError, ValueError, TypeError):
        return None
    return None if name is None else clean(header_text(str(name)))


def is_attachment(part):
    """Whether a part is attached rather than part of the message's text.

    Marked as an attachment, carrying a filename whatever its disposition, or
    a message in its own right. A text part with a filename is a file that
    happens to be text, not what the sender wrote.
    """
    return (
        part.get_content_disposition() == "attachment"
        or attachment_name(part) is not None
        or part.get_content_maintype() == "message"
    )


def first_text(msg, content_type):
    """The first part of `content_type` that is not an attachment, or None."""
    for part in content_parts(msg):
        if part.get_content_type() == content_type and not is_attachment(part):
            return clean(part_text(part))
    return None


def part_size(part):
    """A part's size in octets once decoded, or None if it cannot be had.

    An attached message, or an attached multipart part, holds parts rather
    than a payload to decode, so its size is that of those parts written out
    again, which is close to, though not always exactly, what arrived.
    """
    if part.is_multipart():
        try:
            return sum(len(inner.as_bytes()) for inner in part.get_payload())
        # RecursionError: the generator writes nested messages out recursively,
        # and one attached inside another a few hundred deep runs out of stack.
        except (LookupError, RecursionError, UnicodeError, ValueError, TypeError):
            return None
    payload = part.get_payload(decode=True)
    return len(payload) if isinstance(payload, bytes) else None


def attachments(msg):
    """What the message carries besides its text, described, not included.

    Each part `is_attachment` accepts. Its content is in `raw` with the rest
    of the message, so only its size is given here.
    """
    return [
        {
            "filename": attachment_name(part),
            "content_type": part.get_content_type(),
            "disposition": part.get_content_disposition(),
            "size": part_size(part),
        }
        for part in content_parts(msg)
        if is_attachment(part)
    ]


class Delivery:
    """One message waiting for the webhook, as the handler had it."""

    def __init__(self, received, peer, helo, mail_from, rcpts, body):
        self.received = received
        self.peer = peer
        self.helo = helo
        self.mail_from = mail_from
        self.rcpts = tuple(rcpts)
        self.body = body


def webhook_payload(delivery):
    """The JSON object sent for one message.

    The envelope values are the ones the log file records, control characters
    already written out as escapes, so a carriage return in an address arrives
    as the two characters `\\r` rather than as itself. Header values are decoded
    from RFC 2047. `raw` is the whole message exactly as received, in base64,
    because nothing else can carry arbitrary bytes through JSON intact, and it
    is the only field here that loses nothing.

    A message that cannot be parsed, or read once parsed, still goes out: the
    fields that come from reading it are null or empty, `incomplete` is set,
    and `parse_error` says what went wrong. The parser itself runs out of
    stack on multiparts nested a thousand deep, and whatever else it or a part
    can raise, the envelope and `raw` do not depend on it.
    """
    body = delivery.body
    try:
        fields = message_fields(parse_message(body))
    except Exception as exc:
        fields = {**UNREAD_FIELDS, "parse_error": visible(f"{type(exc).__name__}: {exc}")}
    return {
        "id": str(uuid.uuid4()),
        "received": delivery.received,
        "sink": HOSTNAME,
        "peer": {"address": str(delivery.peer[0]), "port": delivery.peer[1]},
        "helo": delivery.helo,
        "envelope": {"from": delivery.mail_from, "to": list(delivery.rcpts)},
        "message": {
            "size": len(body),
            **fields,
            "raw": base64.b64encode(body).decode("ascii"),
        },
    }


def message_fields(msg):
    """What `message` in the webhook's JSON says of a parsed message."""
    return {
        "subject": decoded_header(msg, "Subject"),
        "from": decoded_header(msg, "From"),
        "to": decoded_header(msg, "To"),
        "cc": decoded_header(msg, "Cc"),
        "reply_to": decoded_header(msg, "Reply-To"),
        "date": decoded_header(msg, "Date"),
        "message_id": decoded_header(msg, "Message-ID"),
        "content_type": msg.get_content_type(),
        "headers": [
            {"name": clean(name), "value": header_value(value)}
            for name, value in msg.raw_items()
        ],
        "text": first_text(msg, "text/plain"),
        "html": first_text(msg, "text/html"),
        "attachments": attachments(msg),
        "incomplete": msg.incomplete,
        "parse_error": None,
    }


# message_fields for a message that could not be read at all.
UNREAD_FIELDS = {
    "subject": None, "from": None, "to": None, "cc": None, "reply_to": None,
    "date": None, "message_id": None, "content_type": None, "headers": [],
    "text": None, "html": None, "attachments": [], "incomplete": True, "parse_error": None,
}


class WebhookError(Exception):
    """A request the webhook answered with something other than 2xx."""


class WebhookSender:
    """Sends each message to the webhook from a thread of its own.

    The syslog forwarder is an asyncio task that hands each line to a worker
    thread; this is a plain thread instead, and on purpose. A webhook can be
    slow for as long as its server likes, and a request stuck in the asyncio
    default executor holds a worker the log file writes need too, and holds up
    the interpreter's exit while it waits. A daemon thread of its own blocks
    nothing but later webhook deliveries, and is abandoned at exit.

    Deliveries go one at a time, in the order the messages were logged. There
    is no retry: a failed delivery costs one line on stderr, and the message
    is in the log file whatever happens here.
    """

    def __init__(self, url, method="POST", headers=(), verify=True):
        parts = urllib.parse.urlsplit(url)
        self.https = parts.scheme.lower() == "https"
        self.host = parts.hostname
        # Always a number. Given no port, http.client looks for one after the
        # last colon in the host, and an IPv6 address such as ::1 has one, so
        # http://[::1]/ would go to a host of ":" on port 1.
        self.port = parts.port or (443 if self.https else 80)
        self.target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        self.method = method
        self.headers = list(headers)
        # Shown at startup and in every error line. Scheme, host and port
        # only, because plenty of webhook URLs carry their secret in the path.
        host = f"[{self.host}]" if ":" in self.host else self.host
        self.shown = f"{parts.scheme.lower()}://{host}" + (f":{parts.port}" if parts.port else "")
        self.context = None
        if self.https:
            self.context = ssl.create_default_context()
            if not verify:
                self.context.check_hostname = False
                self.context.verify_mode = ssl.CERT_NONE
        self.queue = queue.Queue()
        self.lock = threading.Lock()
        self.idle = threading.Condition(self.lock)
        self.waiting = 0  # deliveries queued or being sent
        self.waiting_bytes = 0
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="webhook", daemon=True)
        self.thread.start()

    def stop(self, timeout=None):
        """Finish what is queued and end the thread. For tests; the sink never
        stops it, because the process exiting does."""
        self.queue.put(None)
        if self.thread is not None:
            self.thread.join(timeout)

    def submit(self, delivery):
        """Queue one message, or return False if it would overfill the queue.

        Never blocks. It is called from log_message, in a worker thread, with
        log_lock held, so while it runs every other message's log write waits.
        """
        size = len(delivery.body)
        with self.lock:
            if (self.waiting >= WEBHOOK_QUEUE_SIZE
                    or self.waiting_bytes + size > WEBHOOK_QUEUE_BYTES):
                return False
            self.waiting += 1
            self.waiting_bytes += size
        self.queue.put(delivery)
        return True

    def wait_idle(self, timeout=None):
        """Whether everything queued was dealt with within `timeout`."""
        with self.idle:
            return self.idle.wait_for(lambda: self.waiting == 0, timeout)

    def run(self):
        while True:
            delivery = self.queue.get()
            if delivery is None:
                return
            try:
                self.deliver(delivery)
            except Exception as exc:
                # Escaped, because the text can come from the webhook: a
                # status line or reason with a carriage return or a terminal
                # escape in it would otherwise reach stderr as it came.
                print(f"[{delivery.peer[0]}] webhook {self.shown} failed, not sent;"
                      f" the message is in the log file: {visible(str(exc))}", file=sys.stderr)
            finally:
                with self.idle:
                    self.waiting -= 1
                    self.waiting_bytes -= len(delivery.body)
                    self.idle.notify_all()

    def request_headers(self, body):
        """The headers for one request, the ones given with --header last.

        A header given with --header replaces the sink's own of the same name,
        so a receiver that wants some other Content-Type or User-Agent can have
        it. A name given more than once is sent more than once, as given.
        """
        own = [("User-Agent", "smtp-sink")]
        if body is not None:
            own.append(("Content-Type", "application/json; charset=utf-8"))
            own.append(("Content-Length", str(len(body))))
        given = {name.lower() for name, _ in self.headers}
        return [h for h in own if h[0].lower() not in given] + self.headers

    def deliver(self, delivery):
        """Send one request, raising if it fails or is answered with non-2xx.

        http.client rather than urllib: it sends header names exactly as given,
        where urllib rewrites `X-API-Key` as `X-api-key`, it follows no
        redirects, and it ignores proxy settings in the environment, so the
        request goes where --webhook-url says and nowhere else. A redirect is
        reported as a failure with its status, which says what to fix.
        """
        body = None
        if self.method != "GET":
            body = json.dumps(webhook_payload(delivery)).encode("utf-8")
        if self.https:
            conn = http.client.HTTPSConnection(
                self.host, self.port, timeout=WEBHOOK_TIMEOUT, context=self.context
            )
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=WEBHOOK_TIMEOUT)
        try:
            headers = self.request_headers(body)
            given_host = any(name.lower() == "host" for name, _ in headers)
            given_encoding = any(name.lower() == "accept-encoding" for name, _ in headers)
            conn.putrequest(
                self.method, self.target,
                skip_host=given_host, skip_accept_encoding=given_encoding,
            )
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders()
            response = None
            if body is not None:
                try:
                    for start in range(0, len(body), WEBHOOK_CHUNK):
                        conn.send(body[start : start + WEBHOOK_CHUNK])
                except OSError as exc:
                    # A server may answer before it has read the whole body,
                    # a 413 or a redirect, and hang up, and then it is the
                    # send that fails. What it answered says more than the
                    # reset does, so it is read if it is there to be read.
                    try:
                        response = conn.getresponse()
                    except (OSError, http.client.HTTPException):
                        raise exc from None
            if response is None:
                response = conn.getresponse()
            # Read, so the server is not cut off mid-reply, but not without
            # limit: nothing in it is used. So nothing that goes wrong reading
            # it counts either: the status has already said how it went, and a
            # 2xx cut off mid-body was still taken.
            try:
                response.read(64 * 1024)
            except (OSError, http.client.HTTPException):
                pass
        finally:
            conn.close()
        if not 200 <= response.status < 300:
            raise WebhookError(f"HTTP {response.status} {response.reason}")


async def handle_client(reader, writer, args):
    peer = writer.get_extra_info("peername") or ("?", 0)

    async def send(line):
        writer.write((line + "\r\n").encode("ascii", "replace"))
        await writer.drain()

    async def readline_split():
        """A line as (content, terminator), the terminator as it arrived."""
        raw = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT)
        if not raw:
            raise ConnectionResetError
        # One terminator, not every trailing CR and LF. `rstrip` would eat a
        # carriage return that belongs to the message.
        if raw.endswith(b"\r\n"):
            return raw[:-2], b"\r\n"
        if raw.endswith(b"\n"):
            return raw[:-1], b"\n"
        return raw, b""

    async def readline_raw():
        return (await readline_split())[0]

    async def readline():
        """A command line, as text. DATA reads bytes instead."""
        return (await readline_raw()).decode("utf-8", "replace")

    async def take_path(arg):
        """The address in a MAIL or RCPT argument, or None once refused."""
        if len(arg) > MAX_ARGUMENT:
            await send("500 Line too long")
            return None
        path = envelope_path(arg)
        if len(path) > MAX_PATH:
            await send("501 Path too long")
            return None
        return path

    mail_from = None
    rcpts = []
    # The name the client gave in HELO or EHLO, for the webhook, or None for
    # a greeting with no name. It belongs to the session rather than a
    # transaction, so RSET leaves it alone.
    helo = None

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
                helo = visible(arg[:MAX_HELO]) or None
                await send(f"250 {HOSTNAME}")
            elif verb == "EHLO":
                mail_from, rcpts = None, []
                helo = visible(arg[:MAX_HELO]) or None
                await send(f"250-{HOSTNAME}")
                await send(f"250-SIZE {MAX_MESSAGE_BYTES}")
                await send("250 8BITMIME")
            elif verb == "MAIL":
                path = await take_path(arg)
                if path is None:
                    # The client meant to start a transaction, so an older one
                    # is not left standing for its DATA to be logged under.
                    mail_from, rcpts = None, []
                    continue
                mail_from = path
                rcpts = []
                await send("250 OK")
            elif verb == "RCPT":
                # A recipient belongs to a transaction, and there is none until
                # MAIL (RFC 5321, 3.3). A 250 here would tell the client the
                # recipient was taken when DATA is going to be refused anyway.
                if mail_from is None:
                    await send("503 Need MAIL first")
                    continue
                path = await take_path(arg)
                if path is None:
                    continue
                if len(rcpts) >= MAX_RECIPIENTS:
                    await send("452 Too many recipients")
                    continue
                rcpts.append(path)
                await send("250 OK")
            elif verb == "DATA":
                if mail_from is None or not rcpts:
                    await send("503 Need MAIL and RCPT first")
                    continue
                await send("354 End data with <CR><LF>.<CR><LF>")
                chunks = []
                size = 0
                while True:
                    dline, eol = await readline_split()
                    if dline == b".":
                        break
                    if dline.startswith(b".."):
                        dline = dline[1:]  # undo dot-stuffing
                    # The message in octets, its line ending included, which
                    # is what SIZE promises the client and what the client's
                    # own `size=` parameter counts. Measuring decoded
                    # characters let a message of 8-bit text run well past
                    # the limit.
                    size += len(dline) + len(eol)
                    if size > MAX_MESSAGE_BYTES:
                        chunks = None
                    if chunks is not None:
                        chunks.append(dline + eol)
                if chunks is None:
                    await send("552 Message too large")
                else:
                    # Rebuilt exactly as it came off the wire, dot-stuffing
                    # undone and nothing else touched, a bare LF included.
                    body = b"".join(chunks)
                    received = timestamp()
                    # Queued for the webhook as it is logged, never waited on:
                    # the sender thread builds the JSON and makes the request,
                    # so a webhook that is slow or down never holds up this
                    # connection or any other.
                    delivery = None
                    if webhook is not None:
                        delivery = Delivery(received, peer, helo, mail_from, rcpts, body)
                    try:
                        # In a thread: up to the size limit in one write would
                        # otherwise stall every other client on the loop.
                        queued = await asyncio.to_thread(
                            log_message, args.log, peer, mail_from, rcpts, body, received, delivery
                        )
                    except OSError as exc:
                        # A full disk or a file that cannot be opened is a
                        # transient failure the client should hear about and
                        # retry, not a dropped connection.
                        print(f"[{peer[0]}] could not log message: {exc}", file=sys.stderr)
                        await send("451 Local error, message not logged")
                        mail_from, rcpts = None, []
                        continue
                    print(f"[{peer[0]}] logged message from {mail_from} to {rcpts}")
                    # Said now, before anything that talks to the client: if
                    # the 250 below finds the connection gone, the handler
                    # ends there, and a message the webhook never got would
                    # have left no trace.
                    if queued is False:
                        print(f"[{peer[0]}] webhook queue full, not sent;"
                              " the message is in the log file", file=sys.stderr)
                    # The message is safe in the file, so the client hears so
                    # now, before the forward. Kept waiting on a syslog server
                    # that has gone away, a client can time out and send the
                    # same message again, and the file would hold it twice.
                    await send("250 OK: queued")
                    if syslog is not None:
                        # Building the line parses the message, which is CPU
                        # work up to the size limit, so it runs in a thread,
                        # and sending it is left to the forwarder. This
                        # connection waits for its own message to be parsed
                        # before reading its next command, and that is on
                        # purpose: it keeps each connection to one message in
                        # flight, where a parse handed off to run on its own
                        # would let a fast client queue 10 MB bodies without
                        # limit. The wait is CPU time bounded by the size
                        # limit and comes after the 250, so it never depends
                        # on the syslog server and never lasts long enough for
                        # the client to send the message again. The webhook
                        # bounds the same thing differently: its queue is
                        # capped in bytes, so its parse need not be waited for
                        # here, and it was queued as the message was logged.
                        # A message that cannot be summarised costs its forward,
                        # never the connection the client already heard 250 on.
                        try:
                            line = await asyncio.to_thread(
                                syslog_line, peer, mail_from, rcpts, body,
                                args.syslog_body, args.syslog_max,
                            )
                        except Exception as exc:
                            print(f"[{peer[0]}] syslog line failed, not forwarded;"
                                  f" the message is in the log file: {exc}", file=sys.stderr)
                        else:
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
    refused unless it parses as one, so a typo such as `host:514:` is caught
    here. Anything else is `HOST` or `HOST:PORT`.

    A colon with nothing after it is refused rather than read as the default
    port. A mistyped address would otherwise start the sink with every forward
    failing, and those failures are retried quietly rather than stopping it.
    """
    if text.startswith("["):
        host, bracket, rest = text[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise argparse.ArgumentTypeError(f"expected [HOST] or [HOST]:PORT, got {text!r}")
        colon, port = rest[:1], rest[1:]
    elif text.count(":") > 1:
        try:
            ipaddress.IPv6Address(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"not an IPv6 address, and a port needs brackets: {text!r}"
            ) from None
        host, colon, port = text, "", ""
    else:
        host, colon, port = text.partition(":")
    if not host:
        raise argparse.ArgumentTypeError(f"no host in {text!r}")
    if colon and not port:
        raise argparse.ArgumentTypeError(f"no port after the colon in {text!r}")
    if not port:
        return host, 514
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"not a port number: {port!r}")
    return host, int(port)


def shown_address(address):
    """A (host, port) pair written back out the way `--syslog` takes it."""
    host, port = address
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def webhook_url(text):
    """`--webhook-url`, checked, for argparse to call.

    Everything wrong with it is caught here rather than on the first message,
    because by then it is a line on stderr per message and a sink that looks
    as if it is working.

    No refusal repeats the URL or any part of it, because plenty of webhook
    URLs carry their secret in the path, and stderr at startup goes to the same
    journal as everything else. A ValueError from the parser is turned into a
    refusal here for the same reason: argparse would print the argument with it.
    """
    # http.client refuses a request line holding a space, a control character
    # or anything outside ASCII, but only when the request is made, once per
    # message, and its refusal quotes the whole path. urlsplit would quietly
    # drop a tab or line break, so the text is checked before it is split.
    if any(not "\x21" <= c <= "\x7e" for c in text):
        raise argparse.ArgumentTypeError(
            "the webhook URL can hold only printable ASCII, with no spaces;"
            " percent-encode anything else"
        )
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:  # an unbalanced bracket around an IPv6 host, say
        raise argparse.ArgumentTypeError("the webhook URL could not be read as a URL") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise argparse.ArgumentTypeError("the webhook URL has to start with http:// or https://")
    if not parts.hostname:
        raise argparse.ArgumentTypeError("the webhook URL has no host")
    if parts.username is not None or parts.password is not None:
        # http.client would drop them without a word. Said this way, the
        # credentials go where they can be sent.
        raise argparse.ArgumentTypeError(
            "credentials in the URL are not sent; use --header \"Authorization: ...\" instead"
        )
    try:
        port = parts.port
    except ValueError:  # out of range, or not a number
        port = 0
    if port == 0:
        raise argparse.ArgumentTypeError("the port in the webhook URL is not a number from 1 to 65535")
    return text


def header_pair(text):
    """`--header "Name: Value"` as a (name, value) pair, for argparse to call.

    A refusal never repeats the value, which is as likely as not to be a
    token, and names the header only once the name is known to be one. Given
    `Authorization Bearer abc:def`, what comes before the first colon is half
    the secret, not a name.
    """
    name, colon, value = text.partition(":")
    if not colon:
        raise argparse.ArgumentTypeError('expected "Name: Value", with a colon after the name')
    # Spaces and tabs only, HTTP's optional whitespace. A plain strip would
    # take a trailing line break off before the check below could refuse it.
    value = value.strip(" \t")
    if not HEADER_NAME.fullmatch(name):
        raise argparse.ArgumentTypeError(
            'what comes before the first colon is not a header name; expected "Name: Value"'
        )
    if name.lower() in COMPUTED_HEADERS:
        raise argparse.ArgumentTypeError(f"{name} is worked out from the body and cannot be given")
    # A line break would start a header of its own, and http.client refuses
    # one only when the first request is made, by which time the sink is
    # running and the refusal is a line on stderr per message.
    if any(c in value for c in "\r\n\0"):
        raise argparse.ArgumentTypeError(f"the value of {name} cannot hold a line break")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise argparse.ArgumentTypeError(f"the value of {name} has to be Latin-1") from None
    return name, value


class PatientSysLogHandler(logging.handlers.SysLogHandler):
    """A SysLogHandler that outlasts its syslog server being down.

    The standard one connects a TCP socket while it is being built and raises
    if nothing is listening, which stopped the sink from starting at all, so no
    mail was logged either. Once a TCP send has failed it also keeps the dead
    socket, so every later message fails on it and it never connects again.
    Here a failed connect or send costs one line on stderr, the socket is
    dropped, and the next message tries afresh.

    A TCP socket is also given SYSLOG_TIMEOUT, which the standard one only
    learned in Python 3.14. Without it, a server that silently drops packets
    holds the connect for as long as the system allows, which is minutes on
    some, and the first connect happens before the sink starts listening.
    """

    def createSocket(self):
        try:
            if self.socktype == socket.SOCK_STREAM:
                # `create_connection` tries each address the name resolves
                # to, IPv6 included, as the standard handler does, and leaves
                # the timeout on the socket, so it bounds each send too.
                self.socket = socket.create_connection(self.address, SYSLOG_TIMEOUT)
                self.unixsocket = False
            else:
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
    global syslog, webhook
    # What the sink prints holds addresses from the network, and an address
    # with a character the console cannot encode, such as an umlaut on a
    # Windows console redirected to a file, raised from print. That print comes
    # after the message is in the file but before the client hears 250, so the
    # client sent it again and the file held it twice. Escaped instead.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    ap = argparse.ArgumentParser(
        description="LAN-only SMTP sink that logs mail to a file, and optionally to syslog and a webhook"
    )
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
    ap.add_argument("--syslog-facility", default="local0",
                    choices=sorted(logging.handlers.SysLogHandler.facility_names),
                    help="syslog facility name, e.g. local0, mail, user")
    ap.add_argument("--syslog-body", action="store_true", help="include the message body in the syslog line, not just the summary")
    ap.add_argument("--syslog-max", type=int, default=2000, help="truncate syslog lines to this many characters")
    ap.add_argument("--webhook-url", type=webhook_url, metavar="URL",
                    help="send each message, as JSON, to this http or https URL")
    ap.add_argument("--webhook-method", type=str.upper, choices=WEBHOOK_METHODS,
                    help="request method for the webhook, POST by default; GET sends no body")
    ap.add_argument("--webhook-disable-ssl-verify", action="store_true",
                    help="accept any certificate from an https webhook, self-signed or not")
    ap.add_argument("--header", type=header_pair, action="append", default=[], metavar='"NAME: VALUE"',
                    help="add this header to every webhook request; give it once per header")
    args = ap.parse_args()

    # Webhook options without a webhook are refused rather than ignored: a
    # header nobody sends is a receiver that never authenticates the sink.
    if not args.webhook_url:
        given = [flag for flag, value in (
            ("--webhook-method", args.webhook_method),
            ("--webhook-disable-ssl-verify", args.webhook_disable_ssl_verify),
            ("--header", args.header),
        ) if value]
        if given:
            ap.error(f"{', '.join(given)} needs --webhook-url")

    if args.syslog:
        syslog = setup_syslog(args)
        start_forwarder()
    if args.webhook_url:
        webhook = WebhookSender(
            args.webhook_url, args.webhook_method or "POST", args.header,
            verify=not args.webhook_disable_ssl_verify,
        )
        webhook.start()

    server = await asyncio.start_server(
        lambda r, w: handle_client(r, w, args), args.bind, args.port,
        limit=LINE_LIMIT,
    )
    print(f"SMTP sink listening on {args.bind}:{args.port}, logging to {args.log}"
          + (f", forwarding to syslog {shown_address(args.syslog)} ({args.syslog_proto})" if args.syslog else "")
          + (f", sending to webhook {webhook.shown} ({webhook.method})" if args.webhook_url else ""))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
