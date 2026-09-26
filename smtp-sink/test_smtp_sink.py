"""Tests for smtp_sink.py. Standard library only, so nothing to install.

    python -m unittest

Most of it calls the module's functions directly, because what they produce
is the point of the tool and none of them needs a socket. `main` is run with
`start_server` replaced, so the command line is checked without anything
listening. The protocol tests start the real server on an ephemeral port on
the loopback address and speak SMTP to it, so the replies and the log file are
checked against the same code path a mail client drives.

Nothing here contacts a real syslog server. Most tests replace the module's
`syslog` logger with a mock, which is also how the "no syslog configured" case
is tested, since that is the module's own default. The tests for a server that
is down open their own listener on the loopback address instead, except the
one for a server that never answers, which fakes the connect timing out.

Nothing contacts a real webhook either. The webhook tests run RecordingServer,
an HTTP server on the loopback address that keeps every request it is sent and
can answer with an error, a redirect, or nothing at all.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import email.message
import http.client
import http.server
import io
import json
import logging
import re
import shutil
import smtplib
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import smtp_sink

# Every network read in these tests is bounded by this rather than by the
# module's IDLE_TIMEOUT, which is five minutes: a server that never answers
# should fail the test in a moment, not stall the suite.
REPLY_TIMEOUT = 5

# The shape a device with anything to attach actually sends: a text part saying
# what happened and a second part nobody wants in a syslog line.
ATTACHMENT_BASE64 = base64.b64encode(b"\x00\x01binary junk").decode("ascii")
MULTIPART = (
    "Subject: an alert\n"
    'Content-Type: multipart/mixed; boundary="B"\n'
    "\n"
    "--B\n"
    "Content-Type: text/plain; charset=utf-8\n"
    "\n"
    "the readable part\n"
    "--B\n"
    "Content-Type: application/octet-stream\n"
    "Content-Transfer-Encoding: base64\n"
    "\n"
    + ATTACHMENT_BASE64 + "\n"
    "--B--\n"
)


async def read_reply(reader):
    """Read one SMTP reply, which may be several lines.

    A continuation line carries a hyphen in the fourth column and the last
    line of the reply carries a space, which is how EHLO's multi-line answer
    is told from the end of it.
    """
    lines = []
    while True:
        raw = await asyncio.wait_for(reader.readline(), REPLY_TIMEOUT)
        if not raw:
            break
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(line)
        if len(line) < 4 or line[3] != "-":
            break
    return lines


class WriteEntryTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.log = self.dir / "sink.log"

    def write_bytes(self, body, mail_from="<a@example.test>", rcpts=("<b@example.test>",)):
        """Log one message and hand back the file as bytes, byte for byte."""
        smtp_sink.write_entry(str(self.log), ("192.0.2.7", 41234), mail_from, list(rcpts), body)
        return self.log.read_bytes()

    def write(self, body, mail_from="<a@example.test>", rcpts=("<b@example.test>",)):
        """The same, taking and returning text, which most cases read better in."""
        raw = self.write_bytes(body.encode("utf-8"), mail_from, rcpts)
        return raw.decode("utf-8", "replace")

    def test_records_the_envelope_and_the_body(self):
        text = self.write("Subject: hello\n\nthe body\n")
        self.assertIn("Peer:     192.0.2.7:41234", text)
        self.assertIn("From:     <a@example.test>", text)
        self.assertIn("To:       <b@example.test>", text)
        self.assertIn("Subject: hello", text)
        self.assertIn("the body", text)

    def test_joins_several_recipients_with_commas(self):
        text = self.write("body\n", rcpts=("<b@example.test>", "<c@example.test>"))
        self.assertIn("To:       <b@example.test>, <c@example.test>", text)

    def test_adds_the_newline_a_body_ends_without(self):
        text = self.write("no trailing newline")
        self.assertIn("no trailing newline\n\n", text)

    def test_keeps_a_multipart_message_whole(self):
        """The counterpart to the syslog line, which keeps only the text part."""
        text = self.write(MULTIPART)
        self.assertIn("the readable part", text)
        self.assertIn(ATTACHMENT_BASE64, text)
        self.assertIn("--B--", text)

    def test_keeps_bytes_that_are_not_utf_8(self):
        """The log is the only record, so nothing in it is decoded and rewritten."""
        body = (
            b"Subject: s\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n"
            b"\r\n"
            b"caf\xe9 too warm\r\n"
        )
        raw = self.write_bytes(body)
        self.assertIn(b"caf\xe9 too warm", raw)
        # U+FFFD is what a decode and re-encode of that byte would
        # have left behind, so its absence is the whole assertion.
        self.assertNotIn(chr(0xFFFD).encode(), raw)

    def test_keeps_the_line_endings_the_message_arrived_with(self):
        raw = self.write_bytes(b"Subject: s\r\n\r\ntwo\r\nlines\r\n")
        self.assertIn(b"two\r\nlines\r\n", raw)

    def test_appends_rather_than_replacing(self):
        self.write("first\n")
        text = self.write("second\n")
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertEqual(text.count("Received:"), 2)


class EnvelopePathTests(unittest.TestCase):
    def test_takes_the_address_and_drops_the_parameters(self):
        cases = {
            "FROM:<a@b.test> SIZE=99 BODY=8BITMIME": "<a@b.test>",
            "TO:<b@b.test> NOTIFY=FAILURE": "<b@b.test>",
            "FROM:<>": "<>",
            "FROM:a@b.test SIZE=99": "a@b.test",
        }
        for arg, expected in cases.items():
            with self.subTest(arg=arg):
                self.assertEqual(smtp_sink.envelope_path(arg), expected)

    def test_a_quoted_local_part_can_hold_a_bracket(self):
        cases = {
            'FROM:<"a>b"@example.test> SIZE=1': '<"a>b"@example.test>',
            # A backslash inside the quotes takes the next character literally,
            # so the quote after it does not close the string.
            'FROM:<"a\\">b"@x.test> SIZE=1': '<"a\\">b"@x.test>',
            # An unbalanced quote is malformed. The first `>` is the fallback,
            # which is where the path was always cut before.
            'FROM:<"a>b@x.test> SIZE=1': '<"a>',
        }
        for arg, expected in cases.items():
            with self.subTest(arg=arg):
                self.assertEqual(smtp_sink.envelope_path(arg), expected)

    def test_escapes_control_characters(self):
        # A carriage return would forge a second envelope line in the log, and
        # an escape sequence would rewrite the terminal of whoever reads it.
        cases = {
            "FROM:<a\rb@x.test> SIZE=1": "<a\\rb@x.test>",
            "TO:<a\x1b[2Jb@x.test>": "<a\\x1b[2Jb@x.test>",
            "FROM:<a\x00b@x.test>": "<a\\x00b@x.test>",
            "FROM:a\rb@x.test SIZE=1": "a\\rb@x.test",
        }
        for arg, expected in cases.items():
            with self.subTest(arg=arg):
                self.assertEqual(smtp_sink.envelope_path(arg), expected)

    def test_leaves_printable_non_ascii_alone(self):
        self.assertEqual(smtp_sink.envelope_path("TO:<caf\xe9@b.test>"), "<caf\xe9@b.test>")


class ForwardSyslogTests(unittest.TestCase):
    def setUp(self):
        self.logger = mock.Mock()
        patcher = mock.patch.object(smtp_sink, "syslog", self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)

    def forward(self, body, include_body=False, max_len=2000):
        if isinstance(body, str):
            body = body.encode("utf-8")
        smtp_sink.send_syslog(smtp_sink.syslog_line(
            ("192.0.2.7", 41234), "<a@example.test>", ["<b@example.test>"],
            body, include_body, max_len,
        ))
        self.assertEqual(self.logger.info.call_count, 1)
        return self.logger.info.call_args[0][0]

    def test_summarises_the_envelope_and_the_subject(self):
        line = self.forward("Subject: a test\n\nbody line\n")
        self.assertIn("peer=192.0.2.7", line)
        self.assertIn("from=<a@example.test>", line)
        self.assertIn("to=<b@example.test>", line)
        self.assertIn('subject="a test"', line)

    def test_collapses_whitespace_in_a_folded_subject(self):
        line = self.forward("Subject: one\n  two   three\n\nbody\n")
        self.assertIn('subject="one two three"', line)

    def test_decodes_a_base64_encoded_subject(self):
        line = self.forward("Subject: =?utf-8?B?VVBTIG9uIGJhdHRlcnk=?=\n\nbody\n")
        self.assertIn('subject="UPS on battery"', line)

    def test_decodes_a_quoted_printable_encoded_subject(self):
        line = self.forward("Subject: =?utf-8?Q?caf=C3=A9_too_warm?=\n\nbody\n")
        self.assertIn('subject="café too warm"', line)

    def test_decodes_a_subject_of_several_encoded_words(self):
        line = self.forward(
            "Subject: =?utf-8?B?VVBT?= is =?utf-8?B?b24gYmF0dGVyeQ==?=\n\nbody\n"
        )
        self.assertIn('subject="UPS is on battery"', line)

    def test_an_undecodable_subject_is_passed_through_rather_than_dropped(self):
        # An unknown charset and base64 that is not base64. Neither should cost
        # the forward, which carries the envelope whatever the subject says.
        for raw in ("=?x-nonsense?B?YWJj?=", "=?utf-8?B?!!!not base64!!!?="):
            with self.subTest(raw=raw):
                self.logger.reset_mock()
                line = self.forward(f"Subject: {raw}\n\nbody\n")
                self.assertIn("peer=192.0.2.7", line)
                self.assertIn(raw, line)

    @staticmethod
    def keys(line):
        """The keys of the line, split the way a collector would split it.

        A quoted value runs to the next quote that is not escaped, so a field
        forged by a stray quote shows up here as a key that should not exist.
        """
        return [key for key, _ in re.findall(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)', line)]

    def test_a_quote_in_the_subject_cannot_forge_a_field(self):
        line = self.forward('Subject: x" body="forged\n\nbody\n')
        self.assertEqual(self.keys(line), ["peer", "from", "to", "subject"])
        self.assertIn('subject="x\\" body=\\"forged"', line)

    def test_an_address_cannot_forge_a_field(self):
        # A quoted local part may carry spaces and quotes, and the envelope
        # fields sit unquoted in the line, so both are escaped there.
        smtp_sink.send_syslog(smtp_sink.syslog_line(
            ("192.0.2.7", 41234), '<x subject="forged">', ['<"a b"@example.test>'],
            b"Subject: real\n\nbody\n", False, 2000,
        ))
        line = self.logger.info.call_args[0][0]
        self.assertEqual(self.keys(line), ["peer", "from", "to", "subject"])
        self.assertIn("from=<x\\x20subject=\\x22forged\\x22>", line)
        self.assertIn("to=<\\x22a\\x20b\\x22@example.test>", line)
        self.assertIn('subject="real"', line)

    def test_an_escaped_control_character_in_an_address_is_not_doubled(self):
        # The address arrives with its control characters escaped already,
        # and the syslog line shows them as the log file does.
        line = smtp_sink.syslog_line(
            ("192.0.2.7", 41234), smtp_sink.envelope_path("FROM:<a\x1b@b.test>"),
            ["<c@d.test>"], b"Subject: s\n\nbody\n", False, 2000,
        )
        self.assertIn("from=<a\\x1b@b.test>", line)

    def test_a_backslash_ending_the_subject_does_not_escape_the_quote(self):
        # Unescaped, `C:\` would make the closing quote `\"` and the field
        # would run on into whatever came next.
        line = self.forward("Subject: C:\\\n\nbody\n")
        self.assertTrue(line.endswith('subject="C:\\\\"'), line)
        self.assertEqual(self.keys(line), ["peer", "from", "to", "subject"])

    def test_control_characters_in_the_subject_are_escaped(self):
        line = self.forward("Subject: alert\x1b[2J\n\nbody\n")
        self.assertNotIn("\x1b", line)
        self.assertIn('subject="alert\\x1b[2J"', line)

    def test_the_body_is_escaped_the_same_way(self):
        line = self.forward('Subject: s\n\nhi\x1b[31m "there" \\ done\n', include_body=True)
        self.assertNotIn("\x1b", line)
        self.assertIn('body="hi\\x1b[31m \\"there\\" \\\\ done"', line)
        self.assertEqual(self.keys(line), ["peer", "from", "to", "subject", "body"])

    def test_says_so_when_there_is_no_subject(self):
        self.assertIn('subject="(no subject)"', self.forward("\nbody\n"))

    def test_leaves_the_body_out_by_default(self):
        self.assertNotIn("body=", self.forward("Subject: s\n\nsecret\n"))

    def test_includes_the_body_on_one_line_when_asked(self):
        line = self.forward("Subject: s\n\nfirst\n\nsecond\n", include_body=True)
        self.assertIn('body="first | second"', line)

    def test_takes_the_text_part_out_of_a_multipart_message(self):
        line = self.forward(MULTIPART, include_body=True)
        self.assertIn('body="the readable part"', line)
        # None of the scaffolding, which is what the line used to be made of.
        self.assertNotIn("--B", line)
        self.assertNotIn("Content-Type", line)
        self.assertNotIn(ATTACHMENT_BASE64, line)

    def test_decodes_a_base64_body(self):
        body = (
            "Subject: s\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "Content-Transfer-Encoding: base64\n"
            "\n"
            + base64.b64encode(b"decoded at last\n").decode("ascii")
            + "\n"
        )
        self.assertIn('body="decoded at last"', self.forward(body, include_body=True))

    def test_an_unreadable_charset_does_not_stop_the_forward(self):
        body = (
            "Subject: s\n"
            "Content-Type: text/plain; charset=x-nonsense\n"
            "\n"
            "still readable\n"
        )
        self.assertIn("still readable", self.forward(body, include_body=True))

    def test_a_charset_that_refuses_replacement_does_not_stop_the_forward(self):
        # The idna codec raises UnicodeError for any error handler but strict.
        body = (
            "Subject: s\n"
            "Content-Type: text/plain; charset=idna\n"
            "\n"
            "still readable\n"
        )
        self.assertIn("still readable", self.forward(body, include_body=True))

    def test_a_multipart_message_with_no_text_part_forwards_an_empty_body(self):
        body = MULTIPART.replace("text/plain", "text/html")
        self.assertIn('body=""', self.forward(body, include_body=True))

    def test_the_subject_of_a_multipart_message_is_still_summarised(self):
        self.assertIn('subject="an alert"', self.forward(MULTIPART))

    def test_truncates_a_long_line(self):
        line = self.forward("Subject: " + "x" * 500 + "\n\nbody\n", max_len=80)
        self.assertEqual(len(line), 80)
        # The cut falls inside the subject, so its quote is closed again.
        self.assertTrue(line.endswith('xxx..."'), line)

    def test_a_cut_through_the_body_closes_its_quote(self):
        line = self.forward("Subject: s\n\n" + "y" * 500 + "\n", include_body=True, max_len=120)
        self.assertEqual(len(line), 120)
        self.assertEqual(self.keys(line), ["peer", "from", "to", "subject", "body"])
        self.assertTrue(line.endswith('yyy..."'), line)

    def test_a_cut_through_an_escape_leaves_no_half_of_it(self):
        # Every length from just inside the subject to past it, so the cut
        # lands on each half of the escaped backslashes and on the quote.
        body = "Subject: " + "\\" * 40 + "\n\nbody\n"
        full = self.forward(body)
        for max_len in range(full.index('subject="') + 4, len(full)):
            with self.subTest(max_len=max_len):
                self.logger.reset_mock()
                line = self.forward(body, max_len=max_len)
                self.assertLessEqual(len(line), max_len)
                self.assertEqual(smtp_sink.open_quote(line), (False, False))
                if line.endswith('..."'):
                    self.assertFalse(smtp_sink.open_quote(line[:-4])[1], line)

    def test_a_limit_with_no_room_for_the_ellipsis_still_holds(self):
        for max_len in (0, 1, 2, 3, 4, -5):
            with self.subTest(max_len=max_len):
                self.logger.reset_mock()
                line = self.forward("Subject: " + "x" * 500 + "\n\nbody\n", max_len=max_len)
                self.assertLessEqual(len(line), max(max_len, 0))

    def test_a_body_of_eight_bit_text_is_not_mangled(self):
        # No transfer encoding, so the payload is the wire bytes themselves.
        # Round-tripping those through a str payload turned the accent into a
        # replacement character.
        body = (
            "Subject: s\n"
            "Content-Type: text/plain; charset=utf-8\n"
            "\n"
            "café too warm\n"
        ).encode()
        self.assertIn('body="café too warm"', self.forward(body, include_body=True))

    def test_a_latin_1_body_is_decoded_with_its_own_charset(self):
        body = (
            b"Subject: s\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n"
            b"\r\n"
            b"caf\xe9 too warm\r\n"
        )
        self.assertIn('body="café too warm"', self.forward(body, include_body=True))

    def test_a_raw_eight_bit_subject_does_not_lose_the_line(self):
        """The forward has to survive a subject that cannot be encoded again."""
        # A syslog handler encodes the line on its way out. This mock does the
        # same, so a subject carrying surrogates fails here exactly as it would
        # there, rather than passing a test and failing in the field.
        self.logger.info.side_effect = lambda line: line.encode("utf-8")
        body = b"Subject: caf\xe9 alert\r\n\r\nbody\r\n"
        line = self.forward(body)
        self.assertIn("peer=192.0.2.7", line)
        line.encode("utf-8")  # would raise on a surrogate

    def test_does_nothing_when_no_syslog_is_configured(self):
        with mock.patch.object(smtp_sink, "syslog", None):
            smtp_sink.send_syslog("peer=192.0.2.7 subject=\"s\"")
        self.logger.info.assert_not_called()


class SyslogAddressTests(unittest.TestCase):
    def test_reads_every_way_of_writing_one(self):
        cases = {
            "192.0.2.1": ("192.0.2.1", 514),
            "192.0.2.1:1514": ("192.0.2.1", 1514),
            "syslog.local": ("syslog.local", 514),
            "syslog.local:1514": ("syslog.local", 1514),
            # IPv6: bare means the default port, brackets allow one.
            "::1": ("::1", 514),
            "fe80::5": ("fe80::5", 514),
            "fe80::5%eth0": ("fe80::5%eth0", 514),
            "[::1]": ("::1", 514),
            "[::1]:1514": ("::1", 1514),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(smtp_sink.syslog_address(text), expected)

    def test_refuses_what_is_not_an_address(self):
        refused = (
            "host:abc", "host:0", "host:70000", "[::1", "[::1]x", ":514", "[]:514",
            # A colon with no port after it, and a stray one that would
            # otherwise pass for an IPv6 address.
            "host:", "[::1]:", "host:514:", "a:b:c",
        )
        for text in refused:
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                smtp_sink.syslog_address(text)

    def test_writes_one_back_out_the_way_it_is_given(self):
        self.assertEqual(smtp_sink.shown_address(("192.0.2.1", 514)), "192.0.2.1:514")
        self.assertEqual(smtp_sink.shown_address(("::1", 514)), "[::1]:514")


class SyslogHandlerTests(unittest.TestCase):
    """A TCP syslog server that is down, over a real loopback socket."""

    def setUp(self):
        # `setup_syslog` adds a handler to one process-wide logger; take every
        # one back off so no test sees another's.
        logger = logging.getLogger("smtp_sink")
        self.addCleanup(self._remove_handlers, logger)
        err = io.StringIO()
        self.err = err
        quiet = contextlib.redirect_stderr(err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    @staticmethod
    def _remove_handlers(logger):
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()

    @staticmethod
    def closed_port():
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_a_server_down_at_startup_is_waited_for(self):
        port = self.closed_port()
        args = argparse.Namespace(
            syslog=("127.0.0.1", port), syslog_proto="tcp", syslog_facility="local0"
        )
        logger = smtp_sink.setup_syslog(args)  # used to raise ConnectionRefusedError
        logger.info("lost")  # still down: a line on stderr, nothing raised
        self.assertNotIn("Traceback", self.err.getvalue())
        self.assertEqual(self.err.getvalue().count(f"syslog 127.0.0.1:{port}:"), 2)

        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        listener.settimeout(REPLY_TIMEOUT)
        logger.info("hello")
        conn, _ = listener.accept()
        self.addCleanup(conn.close)
        conn.settimeout(REPLY_TIMEOUT)
        self.assertIn(b"smtp_sink: hello", conn.recv(4096))

    def test_a_tcp_socket_carries_the_timeout(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        handler = smtp_sink.PatientSysLogHandler(
            address=listener.getsockname(), socktype=socket.SOCK_STREAM
        )
        self.addCleanup(handler.close)
        self.assertEqual(handler.socket.gettimeout(), smtp_sink.SYSLOG_TIMEOUT)

    def test_a_connect_that_times_out_is_waited_for(self):
        # A server dropping packets, which loopback cannot stand in for: there
        # the kernel accepts or refuses a connect at once, never ignores it.
        with mock.patch.object(
            smtp_sink.socket, "create_connection", side_effect=TimeoutError("timed out")
        ) as connect:
            handler = smtp_sink.PatientSysLogHandler(
                address=("192.0.2.1", 514), socktype=socket.SOCK_STREAM
            )
        self.addCleanup(handler.close)
        connect.assert_called_once_with(("192.0.2.1", 514), smtp_sink.SYSLOG_TIMEOUT)
        self.assertIsNone(handler.socket)
        self.assertIn("syslog 192.0.2.1:514: timed out", self.err.getvalue())

    def test_a_failed_send_drops_the_socket_so_the_next_one_reconnects(self):
        # Connected for real, then swapped for a socket that has failed. A
        # refused connect would do as a start, but costs two seconds on Windows.
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        handler = smtp_sink.PatientSysLogHandler(
            address=listener.getsockname(), socktype=socket.SOCK_STREAM
        )
        self.addCleanup(handler.close)
        handler.socket.close()
        dead = mock.Mock()
        handler.socket = dead
        try:
            raise ConnectionResetError("the server went away")
        except OSError:
            handler.handleError(logging.makeLogRecord({"msg": "x"}))
        dead.close.assert_called_once_with()
        self.assertIsNone(handler.socket)
        self.assertIn("the server went away", self.err.getvalue())
        self.assertNotIn("Traceback", self.err.getvalue())


class CommandLineTests(unittest.TestCase):
    """The options `main` reads, without a server ever listening."""

    def test_bind_has_to_be_given(self):
        err = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["smtp_sink.py"]),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            asyncio.run(smtp_sink.main())
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--bind", err.getvalue())

    def test_a_syslog_address_that_is_not_one_is_refused_cleanly(self):
        err = io.StringIO()
        argv = ["smtp_sink.py", "--bind", "127.0.0.1", "--syslog", "host:abc"]
        with (
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            asyncio.run(smtp_sink.main())
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--syslog", err.getvalue())

    def test_an_unknown_syslog_facility_is_refused_cleanly(self):
        err = io.StringIO()
        argv = ["smtp_sink.py", "--bind", "127.0.0.1", "--syslog-facility", "bogus"]
        with (
            mock.patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            asyncio.run(smtp_sink.main())
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--syslog-facility", err.getvalue())

    def test_the_server_starts_on_the_address_given_with_the_line_limit(self):
        class Stop(Exception):
            pass

        seen = {}

        async def start_server(handler, host, port, **kwargs):
            seen.update(host=host, port=port, **kwargs)
            raise Stop

        argv = ["smtp_sink.py", "--bind", "192.0.2.10", "--port", "2600"]
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(smtp_sink.asyncio, "start_server", start_server),
            self.assertRaises(Stop),
        ):
            asyncio.run(smtp_sink.main())
        # The limit matters as much as the address. ServerTests start their
        # server with the same one, and this is what keeps that honest.
        self.assertEqual(
            seen, {"host": "192.0.2.10", "port": 2600, "limit": smtp_sink.LINE_LIMIT}
        )


class ServerTests(unittest.IsolatedAsyncioTestCase):
    """The protocol, driven over a real socket against the real handler."""

    async def asyncSetUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.log = self.dir / "sink.log"
        self.args = argparse.Namespace(
            log=str(self.log), syslog_body=False, syslog_max=2000
        )
        self.server = await asyncio.start_server(
            lambda r, w: smtp_sink.handle_client(r, w, self.args), "127.0.0.1", 0,
            limit=smtp_sink.LINE_LIMIT,
        )
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []
        # The forwarder main starts when --syslog is given. It idles unless a
        # test sets a syslog logger, since the handler queues nothing without.
        self.forwarder = smtp_sink.start_forwarder()
        # The handler prints a line per accepted message, which is noise here.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    async def asyncTearDown(self):
        self.forwarder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.forwarder
        # The clients go next, and this is not tidiness. `wait_closed` waits
        # for the handlers still running, and a test that ends without QUIT
        # leaves one sitting in a read that does not give up for IDLE_TIMEOUT,
        # which is five minutes. Hanging up on it first ends that read at once.
        for writer in self.clients:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
        self.server.close()
        await self.server.wait_closed()

    async def connect(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.clients.append(writer)
        greeting = await read_reply(reader)
        self.assertTrue(greeting[0].startswith("220 "), greeting)
        return reader, writer

    async def command(self, reader, writer, text):
        writer.write((text + "\r\n").encode("utf-8"))
        await writer.drain()
        return await read_reply(reader)

    async def test_accepts_a_message_and_logs_it(self):
        reader, writer = await self.connect()
        ehlo = await self.command(reader, writer, "EHLO client.example.test")
        self.assertTrue(ehlo[0].startswith("250-"), ehlo)
        self.assertTrue(ehlo[-1].startswith("250 "), ehlo)
        self.assertTrue(any("SIZE" in line for line in ehlo), ehlo)

        self.assertTrue((await self.command(reader, writer, "MAIL FROM:<a@example.test>"))[0].startswith("250"))
        self.assertTrue((await self.command(reader, writer, "RCPT TO:<b@example.test>"))[0].startswith("250"))
        self.assertTrue((await self.command(reader, writer, "DATA"))[0].startswith("354"))
        accepted = await self.command(reader, writer, "Subject: hello\r\n\r\nthe body\r\n.")
        self.assertTrue(accepted[0].startswith("250"), accepted)
        self.assertTrue((await self.command(reader, writer, "QUIT"))[0].startswith("221"))

        text = self.log.read_bytes().decode("utf-8")
        self.assertIn("From:     <a@example.test>", text)
        self.assertIn("To:       <b@example.test>", text)
        self.assertIn("Subject: hello", text)
        self.assertIn("the body", text)

    async def send_message(self, reader, writer, body):
        """A whole transaction, returning the reply to the message itself."""
        await self.command(reader, writer, "EHLO client.example.test")
        await self.command(reader, writer, "MAIL FROM:<a@example.test>")
        await self.command(reader, writer, "RCPT TO:<b@example.test>")
        await self.command(reader, writer, "DATA")
        writer.write(body.encode("utf-8") + b"\r\n.\r\n")
        await writer.drain()
        return await read_reply(reader)

    async def test_esmtp_parameters_stay_out_of_the_envelope(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        await self.command(reader, writer, "MAIL FROM:<a@example.test> SIZE=99 BODY=8BITMIME")
        await self.command(reader, writer, "RCPT TO:<b@example.test> NOTIFY=FAILURE")
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        await read_reply(reader)

        text = self.log.read_bytes().decode("utf-8")
        self.assertIn("From:     <a@example.test>\n", text)
        self.assertIn("To:       <b@example.test>\n", text)
        self.assertNotIn("SIZE=99", text)
        self.assertNotIn("NOTIFY", text)

    async def test_a_forged_address_cannot_add_a_line_to_the_log(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        # The CR survives the line reader, which splits on LF, so it reaches
        # the handler inside the address exactly as an attacker would send it.
        writer.write(b"MAIL FROM:<evil\rFrom:     <ceo@example.test>>\r\n")
        await writer.drain()
        await read_reply(reader)
        writer.write(b"RCPT TO:<b@example.test\x1b[2J>\r\n")
        await writer.drain()
        await read_reply(reader)
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        reply = await read_reply(reader)
        self.assertTrue(reply[0].startswith("250"), reply)

        envelope = self.log.read_bytes().split(b"-" * 78)[0]
        controls = [b for b in envelope if b < 0x20 and b != 0x0A]
        self.assertEqual(controls, [], envelope)
        # One From line, carrying the forgery as visible text.
        self.assertEqual(
            [line for line in envelope.split(b"\n") if line.startswith(b"From:")],
            [b"From:     <evil\\rFrom:     <ceo@example.test>"],
        )

    async def test_the_null_sender_is_kept(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        await self.command(reader, writer, "MAIL FROM:<>")
        await self.command(reader, writer, "RCPT TO:<b@example.test>")
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        await read_reply(reader)
        self.assertIn("From:     <>\n", self.log.read_bytes().decode("utf-8"))

    async def test_the_message_reaches_the_log_exactly_as_it_was_sent(self):
        """CRLF kept, dot-stuffing undone, and a non-UTF-8 byte left alone."""
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        await self.command(reader, writer, "MAIL FROM:<a@example.test>")
        await self.command(reader, writer, "RCPT TO:<b@example.test>")
        await self.command(reader, writer, "DATA")
        writer.write(
            b"Subject: s\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n"
            b"\r\n"
            b"caf\xe9 too warm\r\n"
            b"..stuffed\r\n"
            b".\r\n"
        )
        await writer.drain()
        reply = await read_reply(reader)
        self.assertTrue(reply[0].startswith("250"), reply)

        raw = self.log.read_bytes()
        self.assertIn(
            b"Subject: s\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n"
            b"\r\n"
            b"caf\xe9 too warm\r\n"
            b".stuffed\r\n",
            raw,
        )

    async def test_a_bare_line_feed_reaches_the_log_as_it_came(self):
        reader, writer = await self.connect()
        reply = await self.send_message(reader, writer, "Subject: s\n\nlf only\n")
        self.assertTrue(reply[0].startswith("250"), reply)
        self.assertIn(b"Subject: s\n\nlf only\n", self.log.read_bytes())
        self.assertNotIn(b"lf only\r\n", self.log.read_bytes())

    async def test_a_log_file_that_cannot_be_written_draws_a_451(self):
        self.args.log = str(self.dir / "missing" / "sink.log")
        reader, writer = await self.connect()
        with contextlib.redirect_stderr(io.StringIO()):
            reply = await self.send_message(reader, writer, "Subject: s\r\n\r\nbody")
        self.assertTrue(reply[0].startswith("451"), reply)
        # The connection carries on, and the envelope went with the failure.
        self.assertTrue((await self.command(reader, writer, "NOOP"))[0].startswith("250"))
        self.assertTrue((await self.command(reader, writer, "DATA"))[0].startswith("503"))

    async def test_the_advertised_size_is_the_limit_that_is_enforced(self):
        reader, writer = await self.connect()
        ehlo = await self.command(reader, writer, "EHLO client.example.test")
        self.assertIn(f"250-SIZE {smtp_sink.MAX_MESSAGE_BYTES}", ehlo)

    async def test_refuses_a_message_over_the_size_limit(self):
        # The limit is patched rather than fed 10 MB. It is the same comparison
        # on the same code path, and a test that moves ten megabytes to watch a
        # three digit reply come back is one nobody will wait for.
        reader, writer = await self.connect()
        with mock.patch.object(smtp_sink, "MAX_MESSAGE_BYTES", 200):
            reply = await self.send_message(reader, writer, "x" * 500)
        self.assertTrue(reply[0].startswith("552"), reply)
        self.assertFalse(self.log.exists())

    async def test_the_size_limit_counts_octets_not_characters(self):
        # Sixty characters and a hundred and twenty octets. A limit between the
        # two is refused only when octets are what is being counted, which is
        # what SIZE promised the client.
        reader, writer = await self.connect()
        with mock.patch.object(smtp_sink, "MAX_MESSAGE_BYTES", 100):
            reply = await self.send_message(reader, writer, "é" * 60)
        self.assertTrue(reply[0].startswith("552"), reply)
        self.assertFalse(self.log.exists())

    async def test_a_stalled_syslog_server_holds_up_nothing(self):
        # A forward stuck on a syslog server that has gone away. The client
        # gets its 250, and the same connection answers its next command and
        # takes its next message, all while that forward is still stuck.
        started = threading.Event()
        released = threading.Event()
        self.addCleanup(released.set)
        sent = []

        def stalled(line):
            started.set()
            released.wait(REPLY_TIMEOUT * 2)
            sent.append(line)

        with mock.patch.object(smtp_sink, "syslog", mock.Mock(info=stalled)):
            reader, writer = await self.connect()
            reply = await self.send_message(reader, writer, "Subject: first\r\n\r\nbody")
            self.assertTrue(reply and reply[0].startswith("250"), reply)
            self.assertTrue(await asyncio.to_thread(started.wait, REPLY_TIMEOUT))
            self.assertTrue((await self.command(reader, writer, "NOOP"))[0].startswith("250"))
            reply = await self.send_message(reader, writer, "Subject: second\r\n\r\nbody")
            self.assertTrue(reply and reply[0].startswith("250"), reply)
            self.assertFalse(released.is_set())

            # Let the server come back. Both lines go out, in the order sent.
            released.set()
            await asyncio.wait_for(smtp_sink.forwards.join(), REPLY_TIMEOUT)
        self.assertEqual(len(sent), 2)
        self.assertIn('subject="first"', sent[0])
        self.assertIn('subject="second"', sent[1])
        self.assertIn(b"Subject: second\r\n", self.log.read_bytes())

    async def test_a_full_syslog_queue_costs_the_forward_not_the_message(self):
        full = asyncio.Queue(maxsize=1)
        full.put_nowait("a line nobody is sending")
        err = io.StringIO()
        with (
            mock.patch.object(smtp_sink, "syslog", mock.Mock()),
            mock.patch.object(smtp_sink, "forwards", full),
            contextlib.redirect_stderr(err),
        ):
            reader, writer = await self.connect()
            reply = await self.send_message(reader, writer, "Subject: s\r\n\r\nbody")
            # The 250 comes before the line is built, so the complaint about
            # the queue can still be on its way. Waited for here, with stderr
            # still captured, rather than looked for once.
            deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT
            while "syslog queue full" not in err.getvalue():
                if asyncio.get_running_loop().time() > deadline:
                    break
                await asyncio.sleep(0.01)
        self.assertTrue(reply and reply[0].startswith("250"), reply)
        self.assertIn("syslog queue full", err.getvalue())
        self.assertIn(b"Subject: s\r\n", self.log.read_bytes())

    async def test_a_line_longer_than_64_kib_is_logged(self):
        # asyncio's default line limit. RFC 5321 says 1000 octets, devices do
        # not always listen, and the message is well within the size limit.
        reader, writer = await self.connect()
        reply = await self.send_message(reader, writer, "Subject: s\r\n\r\n" + "x" * 100_000)
        self.assertTrue(reply and reply[0].startswith("250"), reply)
        self.assertIn(b"x" * 100_000 + b"\r\n", self.log.read_bytes())

    async def test_one_long_line_over_the_size_limit_gets_552(self):
        reader, writer = await self.connect()
        with mock.patch.object(smtp_sink, "MAX_MESSAGE_BYTES", 100_000):
            reply = await self.send_message(reader, writer, "x" * 150_000)
        self.assertTrue(reply and reply[0].startswith("552"), reply)
        self.assertFalse(self.log.exists())
        self.assertTrue((await self.command(reader, writer, "NOOP"))[0].startswith("250"))

    async def test_the_connection_survives_a_refused_message(self):
        reader, writer = await self.connect()
        with mock.patch.object(smtp_sink, "MAX_MESSAGE_BYTES", 200):
            await self.send_message(reader, writer, "x" * 500)
        # The same connection, with a message that fits. This also shows the
        # refusal cleared the envelope rather than leaving it behind.
        reply = await self.send_message(reader, writer, "Subject: small\r\n\r\nfits")
        self.assertTrue(reply[0].startswith("250"), reply)
        self.assertIn("fits", self.log.read_bytes().decode("utf-8"))

    async def test_helo_is_answered_as_well_as_ehlo(self):
        reader, writer = await self.connect()
        reply = await self.command(reader, writer, "HELO client.example.test")
        self.assertEqual(len(reply), 1)
        self.assertTrue(reply[0].startswith("250 "), reply)

    async def test_data_before_an_envelope_is_refused(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        reply = await self.command(reader, writer, "DATA")
        self.assertTrue(reply[0].startswith("503"), reply)
        self.assertFalse(self.log.exists())

    async def test_a_greeting_clears_the_envelope(self):
        for greeting in ("EHLO again.example.test", "HELO again.example.test"):
            with self.subTest(greeting=greeting):
                reader, writer = await self.connect()
                await self.command(reader, writer, "EHLO client.example.test")
                await self.command(reader, writer, "MAIL FROM:<a@example.test>")
                await self.command(reader, writer, "RCPT TO:<b@example.test>")
                await self.command(reader, writer, greeting)
                reply = await self.command(reader, writer, "DATA")
                self.assertTrue(reply[0].startswith("503"), reply)
        self.assertFalse(self.log.exists())

    async def test_rcpt_before_mail_is_refused(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        reply = await self.command(reader, writer, "RCPT TO:<b@example.test>")
        self.assertTrue(reply[0].startswith("503"), reply)
        # Refused, not remembered: a transaction started afterwards carries
        # only the recipients given inside it.
        await self.command(reader, writer, "MAIL FROM:<a@example.test>")
        await self.command(reader, writer, "RCPT TO:<c@example.test>")
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        self.assertTrue((await read_reply(reader))[0].startswith("250"))
        self.assertIn("To:       <c@example.test>\n", self.log.read_bytes().decode("utf-8"))

    async def test_recipients_past_the_limit_draw_452_and_the_rest_are_kept(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        await self.command(reader, writer, "MAIL FROM:<a@example.test>")
        with mock.patch.object(smtp_sink, "MAX_RECIPIENTS", 2):
            for name in ("b", "c"):
                reply = await self.command(reader, writer, f"RCPT TO:<{name}@example.test>")
                self.assertTrue(reply[0].startswith("250"), reply)
            reply = await self.command(reader, writer, "RCPT TO:<d@example.test>")
            self.assertTrue(reply[0].startswith("452"), reply)
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        self.assertTrue((await read_reply(reader))[0].startswith("250"))
        text = self.log.read_bytes().decode("utf-8")
        self.assertIn("To:       <b@example.test>, <c@example.test>\n", text)

    async def test_a_path_past_the_limit_draws_501_and_is_not_kept(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        long_path = "<" + "x" * smtp_sink.MAX_PATH + "@example.test>"
        reply = await self.command(reader, writer, f"MAIL FROM:{long_path}")
        self.assertTrue(reply[0].startswith("501"), reply)
        # Refused, so there is no transaction for a recipient to join.
        reply = await self.command(reader, writer, "RCPT TO:<b@example.test>")
        self.assertTrue(reply[0].startswith("503"), reply)

        await self.command(reader, writer, "MAIL FROM:<a@example.test>")
        reply = await self.command(reader, writer, f"RCPT TO:{long_path}")
        self.assertTrue(reply[0].startswith("501"), reply)
        # The transaction survives the refused recipient and takes the next.
        await self.command(reader, writer, "RCPT TO:<c@example.test>")
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        self.assertTrue((await read_reply(reader))[0].startswith("250"))
        text = self.log.read_bytes().decode("utf-8")
        self.assertIn("To:       <c@example.test>\n", text)
        self.assertNotIn("x" * smtp_sink.MAX_PATH, text)

    async def test_an_argument_past_the_limit_draws_500_unparsed(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        padding = " X-PAD=" + "x" * smtp_sink.MAX_ARGUMENT
        with mock.patch.object(smtp_sink, "envelope_path", wraps=smtp_sink.envelope_path) as parse:
            reply = await self.command(reader, writer, "MAIL FROM:<a@example.test>" + padding)
            self.assertTrue(reply[0].startswith("500"), reply)
            reply = await self.command(reader, writer, "RCPT TO:<b@example.test>")
            self.assertTrue(reply[0].startswith("503"), reply)

            await self.command(reader, writer, "MAIL FROM:<a@example.test>")
            reply = await self.command(reader, writer, "RCPT TO:<b@example.test>" + padding)
            self.assertTrue(reply[0].startswith("500"), reply)
            # Neither long argument reached the parser; the short MAIL did.
            self.assertEqual(parse.call_count, 1)
        # The transaction survives the refused recipient and takes the next.
        await self.command(reader, writer, "RCPT TO:<c@example.test>")
        await self.command(reader, writer, "DATA")
        writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
        await writer.drain()
        self.assertTrue((await read_reply(reader))[0].startswith("250"))
        self.assertIn("To:       <c@example.test>\n", self.log.read_bytes().decode("utf-8"))

    async def test_rset_clears_the_envelope(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        await self.command(reader, writer, "MAIL FROM:<a@example.test>")
        await self.command(reader, writer, "RCPT TO:<b@example.test>")
        self.assertTrue((await self.command(reader, writer, "RSET"))[0].startswith("250"))
        reply = await self.command(reader, writer, "DATA")
        self.assertTrue(reply[0].startswith("503"), reply)

    async def test_auth_and_starttls_are_declined(self):
        reader, writer = await self.connect()
        await self.command(reader, writer, "EHLO client.example.test")
        for verb in ("AUTH LOGIN", "STARTTLS"):
            reply = await self.command(reader, writer, verb)
            self.assertTrue(reply[0].startswith("502"), (verb, reply))

    async def test_noop_is_accepted_and_nonsense_is_not(self):
        reader, writer = await self.connect()
        self.assertTrue((await self.command(reader, writer, "NOOP"))[0].startswith("250"))
        reply = await self.command(reader, writer, "WIDGET please")
        self.assertTrue(reply[0].startswith("500"), reply)

    async def test_a_real_client_round_trip_undoes_dot_stuffing(self):
        """smtplib is blocking, so it runs in a thread and leaves the loop free.

        It is here for the one thing the hand-driven tests cannot show: a real
        client stuffs a leading dot on the way out, and the body in the log is
        the body that was sent only if the server unstuffs it again.
        """
        body = "Subject: stuffed\n\n.hidden line\nplain line\n"

        def send():
            with smtplib.SMTP("127.0.0.1", self.port, timeout=REPLY_TIMEOUT) as client:
                client.sendmail("a@example.test", ["b@example.test"], body)

        await asyncio.to_thread(send)

        text = self.log.read_bytes().decode("utf-8")
        self.assertIn(".hidden line", text)
        self.assertIn("plain line", text)
        # The exact line, not a substring. smtplib appends `size=` to MAIL
        # FROM the moment the server advertises SIZE, and a loose assertion
        # here let that parameter into the envelope unnoticed.
        self.assertIn("From:     <a@example.test>\n", text)
        self.assertIn("To:       <b@example.test>\n", text)

    async def test_a_message_reaches_the_webhook_with_its_session(self):
        hook = RecordingServer(self)
        sender = hook.sender()
        with mock.patch.object(smtp_sink, "webhook", sender):
            reader, writer = await self.connect()
            reply = await self.send_message(reader, writer, "Subject: to the hook\r\n\r\nbody")
            self.assertTrue(reply[0].startswith("250"), reply)
            self.assertTrue(await asyncio.to_thread(sender.wait_idle, REPLY_TIMEOUT))
        payload = json.loads(hook.requests[0].body)
        self.assertEqual(payload["helo"], "client.example.test")
        self.assertEqual(payload["envelope"], {"from": "<a@example.test>", "to": ["<b@example.test>"]})
        self.assertEqual(payload["message"]["subject"], "to the hook")
        # The same moment as the log file records, so the two can be matched.
        self.assertIn(f"Received: {payload['received']}\n", self.log.read_bytes().decode("utf-8"))

    async def test_a_greeting_with_no_name_leaves_helo_null(self):
        queued = []
        webhook = mock.Mock(submit=lambda delivery: queued.append(delivery) or True)
        with mock.patch.object(smtp_sink, "webhook", webhook):
            reader, writer = await self.connect()
            await self.command(reader, writer, "EHLO")
            await self.command(reader, writer, "MAIL FROM:<a@example.test>")
            await self.command(reader, writer, "RCPT TO:<b@example.test>")
            await self.command(reader, writer, "DATA")
            writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
            await writer.drain()
            self.assertTrue((await read_reply(reader))[0].startswith("250"))
        self.assertIsNone(queued[0].helo)
        self.assertIsNone(smtp_sink.webhook_payload(queued[0])["helo"])

    async def test_the_helo_name_is_escaped_cut_and_kept_through_rset(self):
        queued = []
        webhook = mock.Mock(submit=lambda delivery: queued.append(delivery) or True)
        name = "ups\x1b[2J" + "x" * 300
        with mock.patch.object(smtp_sink, "webhook", webhook):
            reader, writer = await self.connect()
            await self.command(reader, writer, f"EHLO {name}")
            await self.command(reader, writer, "RSET")
            await self.command(reader, writer, "MAIL FROM:<a@example.test>")
            await self.command(reader, writer, "RCPT TO:<b@example.test>")
            await self.command(reader, writer, "DATA")
            writer.write(b"Subject: s\r\n\r\nbody\r\n.\r\n")
            await writer.drain()
            self.assertTrue((await read_reply(reader))[0].startswith("250"))
        helo = queued[0].helo
        # Cut to MAX_HELO before escaping, so the escape makes it longer.
        self.assertEqual(helo, "ups\\x1b[2J" + "x" * (smtp_sink.MAX_HELO - len("ups\x1b[2J")))
        self.assertNotIn("\x1b", helo)

    async def test_a_stalled_webhook_holds_up_nothing(self):
        # A webhook that takes the request and never answers. The client gets
        # its 250, and the same connection and a new one both carry on, all
        # while the first delivery is still waiting.
        hook = RecordingServer(self, stall=True)
        sender = hook.sender()
        with mock.patch.object(smtp_sink, "webhook", sender):
            reader, writer = await self.connect()
            reply = await self.send_message(reader, writer, "Subject: first\r\n\r\nbody")
            self.assertTrue(reply[0].startswith("250"), reply)
            self.assertTrue(await asyncio.to_thread(hook.arrived.wait, REPLY_TIMEOUT))
            self.assertTrue((await self.command(reader, writer, "NOOP"))[0].startswith("250"))
            reply = await self.send_message(reader, writer, "Subject: second\r\n\r\nbody")
            self.assertTrue(reply[0].startswith("250"), reply)
            other_reader, other_writer = await self.connect()
            reply = await self.send_message(other_reader, other_writer, "Subject: third\r\n\r\nbody")
            self.assertTrue(reply[0].startswith("250"), reply)
            self.assertIn(b"Subject: third\r\n", self.log.read_bytes())
            # One at a time: the first is with the server, the other two wait
            # their turn. Each was queued before its 250, so this is settled
            # by now, and a sender that sent them all at once would show the
            # server three requests and an empty queue.
            self.assertEqual(len(hook.requests), 1)
            self.assertEqual(sender.queue.qsize(), 2)

            # Let it answer. All three go out, in the order they were logged.
            hook.released.set()
            self.assertTrue(await asyncio.to_thread(sender.wait_idle, REPLY_TIMEOUT))
        subjects = [json.loads(r.body)["message"]["subject"] for r in hook.requests]
        self.assertEqual(subjects, ["first", "second", "third"])

    async def test_a_message_is_queued_for_the_webhook_while_the_log_is_locked(self):
        # What keeps the webhook's order the file's: queued any later, a
        # message logged first could be queued second by a slower client.
        seen = {}

        def submit(delivery):
            def probe():
                seen["free"] = smtp_sink.log_lock.acquire(blocking=False)
                if seen["free"]:
                    smtp_sink.log_lock.release()

            other = threading.Thread(target=probe)
            other.start()
            other.join(REPLY_TIMEOUT)
            seen["logged"] = b"Subject: s\r\n" in self.log.read_bytes()
            return True

        with mock.patch.object(smtp_sink, "webhook", mock.Mock(submit=submit)):
            reader, writer = await self.connect()
            reply = await self.send_message(reader, writer, "Subject: s\r\n\r\nbody")
        self.assertTrue(reply[0].startswith("250"), reply)
        self.assertEqual(seen, {"free": False, "logged": True})

    async def test_a_full_webhook_queue_is_reported_even_if_the_250_fails(self):
        # A client gone by the time its 250 is written ends the handler right
        # there. The message was logged and refused by the webhook's queue,
        # and that still has to be said.
        class GoneAt250:
            def __init__(self, writer):
                self._writer = writer

            def __getattr__(self, name):
                return getattr(self._writer, name)

            def write(self, data):
                if data.startswith(b"250 OK: queued"):
                    raise ConnectionResetError("the client has gone")
                self._writer.write(data)

        server = await asyncio.start_server(
            lambda r, w: smtp_sink.handle_client(r, GoneAt250(w), self.args), "127.0.0.1", 0,
            limit=smtp_sink.LINE_LIMIT,
        )
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)
        err = io.StringIO()
        with (
            mock.patch.object(smtp_sink, "webhook", mock.Mock(submit=lambda delivery: False)),
            contextlib.redirect_stderr(err),
        ):
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            self.clients.append(writer)
            await read_reply(reader)
            reply = await self.send_message(reader, writer, "Subject: s\r\n\r\nbody")
        self.assertEqual(reply, [])  # hung up on, not answered
        self.assertIn(b"Subject: s\r\n", self.log.read_bytes())
        self.assertIn("webhook queue full", err.getvalue())

    async def test_a_full_webhook_queue_costs_the_send_not_the_message(self):
        sender = smtp_sink.WebhookSender("http://192.0.2.1/")  # never started
        err = io.StringIO()
        with (
            mock.patch.object(smtp_sink, "webhook", sender),
            mock.patch.object(smtp_sink, "WEBHOOK_QUEUE_SIZE", 0),
            contextlib.redirect_stderr(err),
        ):
            reader, writer = await self.connect()
            reply = await self.send_message(reader, writer, "Subject: s\r\n\r\nbody")
        self.assertTrue(reply[0].startswith("250"), reply)
        self.assertIn("webhook queue full", err.getvalue())
        self.assertIn(b"Subject: s\r\n", self.log.read_bytes())


class Request:
    """One request as the recording server saw it."""

    def __init__(self, method, path, headers, body):
        self.method = method
        self.path = path
        self.headers = headers  # (name, value) pairs, names as sent
        self.body = body

    def header(self, name):
        return [v for n, v in self.headers if n.lower() == name.lower()]


class RecordingServer:
    """A webhook on the loopback address that keeps what it is sent.

    `status` is what it answers with. With `stall`, each request waits on
    `released` before it is answered, which is a webhook server that has
    taken the request and gone quiet.
    """

    def __init__(self, case, status=204, stall=False, location=None,
                 server_class=http.server.ThreadingHTTPServer, host="127.0.0.1"):
        self.requests = []
        self.arrived = threading.Event()
        self.released = threading.Event()
        if not stall:
            self.released.set()
        recorder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                recorder.requests.append(
                    Request(self.command, self.path, list(self.headers.items()), body)
                )
                recorder.arrived.set()
                recorder.released.wait(REPLY_TIMEOUT * 2)
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_any

            def log_message(self, *args):
                pass

        self.server = server_class((host, 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        # A short poll, because shutdown waits out one interval, and the
        # default of half a second across every test here doubled the suite.
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        # Cleanups run last first: release anything stalled, then stop the
        # server. sender() adds its own stop, and a second release after it.
        case.addCleanup(self.server.server_close)
        case.addCleanup(self.server.shutdown)
        case.addCleanup(self.released.set)
        self.case = case
        shown = f"[{host}]" if ":" in host else host
        self.url = f"http://{shown}:{self.server.server_address[1]}/hook?key=value"

    def sender(self, **kwargs):
        sender = smtp_sink.WebhookSender(self.url, **kwargs)
        sender.start()
        # Stopped after anything stalled is released, not before: joined
        # while its request is still held, it waits out its whole timeout.
        self.case.addCleanup(sender.stop, REPLY_TIMEOUT)
        self.case.addCleanup(self.released.set)
        return sender


def delivery(body=b"Subject: s\r\n\r\nbody\r\n", **overrides):
    fields = {
        "received": "2026-01-02T03:04:05+00:00",
        "peer": ("192.0.2.7", 40000),
        "helo": "ups.example.test",
        "mail_from": "<ups@example.test>",
        "rcpts": ["<ops@example.test>", "<oncall@example.test>"],
        "body": body,
    }
    fields.update(overrides)
    return smtp_sink.Delivery(**fields)


class WebhookPayloadTests(unittest.TestCase):
    """The JSON object a message becomes."""

    def test_carries_the_envelope_and_the_session(self):
        payload = smtp_sink.webhook_payload(delivery())
        self.assertEqual(payload["received"], "2026-01-02T03:04:05+00:00")
        self.assertEqual(payload["sink"], smtp_sink.HOSTNAME)
        self.assertEqual(payload["peer"], {"address": "192.0.2.7", "port": 40000})
        self.assertEqual(payload["helo"], "ups.example.test")
        self.assertEqual(payload["envelope"], {
            "from": "<ups@example.test>",
            "to": ["<ops@example.test>", "<oncall@example.test>"],
        })
        self.assertRegex(payload["id"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertNotEqual(payload["id"], smtp_sink.webhook_payload(delivery())["id"])

    def test_decodes_the_headers_and_says_which_are_missing(self):
        body = (
            b"Subject: =?utf-8?B?VVBTIG9uIGJhdHRlcnksIDE1wrBD?=\r\n"
            b"From: UPS <ups@example.test>\r\n"
            b"To: ops@example.test\r\n"
            b"Date: Fri, 02 Jan 2026 03:04:05 +0000\r\n"
            b"Message-ID: <1@example.test>\r\n"
            b"\r\nbody\r\n"
        )
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["subject"], "UPS on battery, 15°C")
        self.assertEqual(message["from"], "UPS <ups@example.test>")
        self.assertEqual(message["to"], "ops@example.test")
        self.assertEqual(message["date"], "Fri, 02 Jan 2026 03:04:05 +0000")
        self.assertEqual(message["message_id"], "<1@example.test>")
        self.assertIsNone(message["cc"])
        self.assertIsNone(message["reply_to"])
        self.assertEqual(message["size"], len(body))
        self.assertEqual(message["content_type"], "text/plain")
        self.assertEqual(
            [h["name"] for h in message["headers"]],
            ["Subject", "From", "To", "Date", "Message-ID"],
        )
        self.assertEqual(message["headers"][0]["value"], "UPS on battery, 15°C")
        self.assertEqual(message["text"], "body\r\n")
        self.assertIsNone(message["html"])
        self.assertEqual(message["attachments"], [])

    def test_takes_the_text_and_html_and_describes_the_attachments(self):
        body = (
            'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
            "--M\r\n"
            'Content-Type: multipart/alternative; boundary="A"\r\n\r\n'
            "--A\r\nContent-Type: text/plain\r\n\r\nplain words\r\n"
            "--A\r\nContent-Type: text/html\r\n\r\n<p>html words</p>\r\n"
            "--A--\r\n"
            "--M\r\n"
            "Content-Type: text/plain\r\n"
            'Content-Disposition: attachment; filename="notes.txt"\r\n\r\n'
            "not the text\r\n"
            "--M\r\n"
            "Content-Type: application/octet-stream\r\n"
            'Content-Disposition: attachment; filename="blob.bin"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\n"
            + ATTACHMENT_BASE64 + "\r\n"
            "--M--\r\n"
        ).encode("ascii")
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["content_type"], "multipart/mixed")
        self.assertEqual(message["text"], "plain words")
        self.assertEqual(message["html"], "<p>html words</p>")
        self.assertEqual(message["attachments"], [
            {"filename": "notes.txt", "content_type": "text/plain",
             "disposition": "attachment", "size": len(b"not the text")},
            {"filename": "blob.bin", "content_type": "application/octet-stream",
             "disposition": "attachment", "size": len(b"\x00\x01binary junk")},
        ])

    def test_an_attached_message_or_named_file_is_not_the_text(self):
        # An email forwarded as an attachment has text parts of its own, and
        # a text file sent inline still has a filename. Neither is what the
        # sender wrote, and both are attachments. Both come before the real
        # text on purpose: a walker that went into the attached message, or
        # took a named file for the text, would find theirs first.
        body = (
            'Subject: outer\r\nContent-Type: multipart/mixed; boundary="M"\r\n\r\n'
            "--M\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment\r\n\r\n"
            "Subject: inner\r\n\r\nthe inner words\r\n"
            "--M\r\nContent-Type: text/html\r\n"
            'Content-Disposition: inline; filename="page.html"\r\n\r\n<p>a file</p>\r\n'
            "--M\r\nContent-Type: text/plain\r\n\r\nthe outer words\r\n"
            "--M--\r\n"
        ).encode("ascii")
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["text"], "the outer words")
        self.assertIsNone(message["html"])
        self.assertEqual(
            [(a["filename"], a["content_type"], a["disposition"]) for a in message["attachments"]],
            [(None, "message/rfc822", "attachment"), ("page.html", "text/html", "inline")],
        )
        self.assertGreater(message["attachments"][0]["size"], len(b"the inner words"))
        self.assertEqual(message["attachments"][1]["size"], len(b"<p>a file</p>"))

    def test_an_attached_multipart_part_is_one_attachment(self):
        # Its text belongs to the attachment. Placed first, so a walker that
        # went into it would find that text before the real one.
        body = (
            'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
            '--M\r\nContent-Type: multipart/alternative; boundary="A"\r\n'
            'Content-Disposition: attachment; filename="bundle"\r\n\r\n'
            "--A\r\nContent-Type: text/plain\r\n\r\ninside the attachment\r\n"
            "--A\r\nContent-Type: text/html\r\n\r\n<p>inside</p>\r\n--A--\r\n"
            "--M\r\nContent-Type: text/plain\r\n\r\nthe real text\r\n"
            "--M--\r\n"
        ).encode("ascii")
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["text"], "the real text")
        self.assertIsNone(message["html"])
        self.assertEqual(
            [(a["filename"], a["content_type"]) for a in message["attachments"]],
            [("bundle", "multipart/alternative")],
        )

    def test_raw_is_the_message_byte_for_byte(self):
        body = b"Subject: s\r\n\r\nlatin-1 \xe9 and a bare\nline feed\r\n"
        raw = smtp_sink.webhook_payload(delivery(body))["message"]["raw"]
        self.assertEqual(base64.b64decode(raw), body)

    def test_raw_eight_bit_text_still_makes_valid_json(self):
        # Surrogates from the parser would come out of json.dumps as \udcxx,
        # which a strict receiver refuses.
        body = b"Subject: caf\xe9 \xff\r\nX-Raw: \xe9\r\n\r\nbody \xe9\r\n"
        text = json.dumps(smtp_sink.webhook_payload(delivery(body)))
        self.assertNotIn("\\udc", text)
        message = json.loads(text)["message"]
        self.assertEqual(message["subject"], "caf� �")
        self.assertEqual(message["headers"][1]["value"], "�")

    def test_a_non_ascii_filename_is_decoded_either_way_it_is_sent(self):
        body = (
            'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
            "--M\r\nContent-Type: application/pdf\r\n"
            'Content-Disposition: attachment; filename="=?utf-8?B?UmVwb3J0IE3DvG5jaGVuLnBkZg==?="\r\n\r\nx\r\n'
            "--M\r\nContent-Type: application/pdf\r\n"
            "Content-Disposition: attachment; filename*=utf-8''Bericht%20M%C3%BCnchen.pdf\r\n\r\nx\r\n"
            "--M--\r\n"
        ).encode("ascii")
        names = [a["filename"] for a in smtp_sink.webhook_payload(delivery(body))["message"]["attachments"]]
        self.assertEqual(names, ["Report München.pdf", "Bericht München.pdf"])

    def test_a_filename_sent_as_raw_utf_8_keeps_its_letters(self):
        body = (
            'Content-Type: multipart/mixed; boundary="M"\r\n\r\n'
            "--M\r\nContent-Type: application/pdf\r\n"
            'Content-Disposition: attachment; filename="Bericht München \udcff.pdf"\r\n\r\nx\r\n'
            "--M--\r\n"
        ).encode("utf-8", "surrogateescape")
        names = [a["filename"] for a in smtp_sink.webhook_payload(delivery(body))["message"]["attachments"]]
        self.assertEqual(names, ["Bericht München �.pdf"])

    def test_one_malformed_part_costs_its_field_not_the_delivery(self):
        # Each of these once raised out of webhook_payload, and the receiver
        # got nothing, raw message and envelope included. Now even a message
        # that cannot be read at all goes out, so what is checked here is
        # that these are read, not merely sent.
        cases = {
            "a NUL in a text part's charset": (
                b'Content-Type: text/plain; charset="utf\x008"\r\n\r\nhi\r\n'),
            "a charset given both whole and in pieces": (
                b"Content-Type: text/plain; charset*=utf-8''x; charset*0=a\r\n\r\nhi\r\n"),
            "an attached message nested four hundred deep": (
                b"Content-Type: message/rfc822\r\n\r\n" * 400 + b"x"),
        }
        for name, body in cases.items():
            with self.subTest(name):
                message = smtp_sink.webhook_payload(delivery(body))["message"]
                self.assertIsNone(message["parse_error"])
                self.assertFalse(message["incomplete"])
                self.assertEqual(base64.b64decode(message["raw"]), body)
                if name != "an attached message nested four hundred deep":
                    self.assertEqual(message["text"], "hi\r\n")

    def test_a_charset_name_that_is_not_ascii_leaves_the_header_as_it_came(self):
        self.assertEqual(smtp_sink.header_text("=?utf-�?q?a?="), "=?utf-�?q?a?=")

    def test_a_message_the_parser_cannot_take_still_goes_out(self):
        # Multiparts nested a thousand deep run the parser out of stack. The
        # envelope and raw do not need it, so they go out regardless.
        depth = 1000
        body = (
            "".join(f'Content-Type: multipart/mixed; boundary="b{i}"\r\n\r\n--b{i}\r\n' for i in range(depth))
            + "Content-Type: text/plain\r\n\r\nx"
            + "".join(f"\r\n--b{i}--\r\n" for i in reversed(range(depth)))
        ).encode("ascii")
        payload = smtp_sink.webhook_payload(delivery(body))
        message = payload["message"]
        self.assertIn("RecursionError", message["parse_error"])
        self.assertTrue(message["incomplete"])
        self.assertIsNone(message["subject"])
        self.assertEqual(message["size"], len(body))
        self.assertEqual(base64.b64decode(message["raw"]), body)
        self.assertEqual(payload["envelope"]["from"], "<ups@example.test>")
        json.dumps(payload)

    def test_an_unread_message_has_the_same_fields_as_a_read_one(self):
        read = smtp_sink.webhook_payload(delivery())["message"]
        with mock.patch.object(smtp_sink, "parse_message", side_effect=ValueError("no")):
            unread = smtp_sink.webhook_payload(delivery())["message"]
        self.assertEqual(list(unread), list(read))
        self.assertIsNone(read["parse_error"])
        self.assertEqual(unread["parse_error"], "ValueError: no")

    def test_a_parse_keeps_only_so_many_headers_and_parts(self):
        # Two million five-byte headers were two million entries in memory
        # and in the JSON. Past the budget they are dropped as they arrive.
        body = (
            "".join(f"X-H{i}: {i}\r\n" for i in range(8))
            + 'Content-Type: multipart/mixed; boundary="B"\r\n\r\n'
            + "".join(f"--B\r\nContent-Disposition: attachment; filename=f{i}\r\n\r\nx\r\n" for i in range(3))
            + "--B--\r\n"
        ).encode("ascii")
        with (
            mock.patch.object(smtp_sink, "MAX_PARSED_HEADERS", 11),
            mock.patch.object(smtp_sink, "MAX_PARSED_PARTS", 2),
        ):
            message = smtp_sink.webhook_payload(delivery(body))["message"]
            self.assertFalse(smtp_sink.webhook_payload(delivery())["message"]["incomplete"])
        self.assertTrue(message["incomplete"])
        self.assertEqual(len(message["headers"]), 9)
        self.assertEqual(message["content_type"], "multipart/mixed")
        self.assertEqual([a["filename"] for a in message["attachments"]], ["f0", "f1"])
        self.assertEqual(base64.b64decode(message["raw"]), body)

    def test_parameter_headers_are_cut_before_they_are_split(self):
        # Splitting into parameters is quadratic, and the parser does it for
        # every multipart boundary: a message-sized Content-Type of them held
        # the thread for a quarter of an hour.
        params = ";a=b" * 100_000
        body = (
            f"Content-Type: multipart/mixed; boundary=B{params}\r\n\r\n"
            "--B\r\nContent-Type: text/plain; charset=utf-8\r\n"
            f'Content-Disposition: attachment; filename="notes.txt"{params}\r\n\r\nx\r\n'
            "--B--\r\n"
        ).encode("ascii")
        with mock.patch("email.message._parseparam", wraps=email.message._parseparam) as split:
            message = smtp_sink.webhook_payload(delivery(body))["message"]
            smtp_sink.syslog_line(("p", 1), "a", ["b"], body, True, 2000)
        self.assertTrue(split.called)
        self.assertLessEqual(max(len(call.args[0]) for call in split.call_args_list),
                             smtp_sink.MAX_HEADER_TEXT)
        self.assertEqual(message["content_type"], "multipart/mixed")
        self.assertEqual(message["attachments"][0]["filename"], "notes.txt")

    def test_a_huge_header_is_cut_before_it_is_decoded(self):
        # Decoding is quadratic in the length: a message-sized header of
        # encoded words, decoded whole, held the sender thread for hours.
        value = "=?utf-8?q?a?= " * 100_000
        with mock.patch.object(smtp_sink, "decode_header", wraps=smtp_sink.decode_header) as decode:
            text = smtp_sink.header_text(value)
        self.assertEqual(len(decode.call_args.args[0]), smtp_sink.MAX_HEADER_TEXT)
        self.assertTrue(text.startswith("aaaa"))
        self.assertTrue(text.endswith("..."))
        self.assertEqual(smtp_sink.header_text("short"), "short")

    def test_a_folded_header_is_unfolded_however_it_is_encoded(self):
        body = (
            b"Subject: a subject long enough\r\n\tto be folded\r\n"
            b"X-Encoded: =?utf-8?q?a_subject_long_enough?=\r\n =?utf-8?q?to_be_folded?=\r\n"
            b"X-Bare-LF: folded\n with a bare line feed\n"
            b"\r\nbody\r\n"
        )
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["subject"], "a subject long enough\tto be folded")
        values = [h["value"] for h in message["headers"]]
        self.assertEqual(values[1], "a subject long enoughto be folded")
        self.assertEqual(values[2], "folded with a bare line feed")
        self.assertFalse(any("\n" in v for v in values))

    def test_raw_utf_8_in_a_header_survives(self):
        # Sent as raw 8-bit rather than as encoded words. Only the byte that
        # is not UTF-8 is replaced; the rest reads as it was meant.
        body = "Subject: café 15°C \udcff\r\nX-Site: München\r\n\r\nbody\r\n".encode(
            "utf-8", "surrogateescape")
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["subject"], "café 15°C �")
        self.assertEqual(message["headers"][1], {"name": "X-Site", "value": "München"})

    def test_raw_text_and_encoded_words_in_one_header_both_read(self):
        # decode_header gives up on a value holding anything but ASCII, so
        # encoded words beside raw 8-bit text were left encoded.
        body = ("Subject: café =?utf-8?q?M=C3=BCnchen?= ok\r\n"
                "X-Long: é" + "=?utf-8?q?a?= " * 1000 + "\r\n\r\nbody\r\n").encode("utf-8")
        message = smtp_sink.webhook_payload(delivery(body))["message"]
        self.assertEqual(message["subject"], "café München ok")
        long_value = message["headers"][1]["value"]
        self.assertTrue(long_value.startswith("éaaa"))
        self.assertTrue(long_value.endswith("..."))
        self.assertLess(len(long_value), smtp_sink.MAX_HEADER_TEXT)

    def test_an_escaped_address_stays_escaped(self):
        payload = smtp_sink.webhook_payload(delivery(mail_from="<a\\r@example.test>"))
        self.assertEqual(payload["envelope"]["from"], "<a\\r@example.test>")


class WebhookOptionTests(unittest.TestCase):
    """--webhook-url and --header, checked before anything is sent."""

    def test_takes_http_and_https_urls(self):
        for url in ("http://192.0.2.1/", "https://hooks.example.test/a/b?c=d",
                    "HTTPS://hooks.example.test:8443", "http://[::1]:8080/hook"):
            with self.subTest(url=url):
                self.assertEqual(smtp_sink.webhook_url(url), url)

    def test_refuses_what_it_cannot_send_to(self):
        for url in ("ftp://example.test/", "hooks.example.test/path", "http://",
                    "http://user:pw@example.test/", "http://example.test:0/",
                    "http://example.test:99999/", "http://example.test:port/",
                    "http://[::1/x",
                    # What http.client would refuse on every request instead.
                    "http://example.test/a b", "http://example.test/?q=a b",
                    "http://example.test/café", "http://example.test/a\x01b",
                    "http://example.test/a\tb", "http://ex ample.test/", "http://example.test/\x7f"):
            with self.subTest(url=url), self.assertRaises(argparse.ArgumentTypeError):
                smtp_sink.webhook_url(url)

    def test_a_refusal_does_not_print_the_url(self):
        for url in ("ftp://example.test/s3cret", "http:///s3cret", "http://example.test:0/s3cret",
                    "http://u:s3cret@example.test/", "http://[::1/s3cret",
                    "http://example.test/s3cret token", "http://example.test/s3crét"):
            with self.subTest(url=url):
                with self.assertRaises(argparse.ArgumentTypeError) as refused:
                    smtp_sink.webhook_url(url)
                self.assertNotIn("s3cret", str(refused.exception))

    def test_reads_a_header(self):
        self.assertEqual(smtp_sink.header_pair("X-API-Key: abc"), ("X-API-Key", "abc"))
        self.assertEqual(smtp_sink.header_pair("Authorization:Bearer a:b"), ("Authorization", "Bearer a:b"))
        self.assertEqual(smtp_sink.header_pair("X-Empty:"), ("X-Empty", ""))

    def test_refuses_a_header_that_is_not_one(self):
        for text in ("no colon", ": no name", "Bad Name: x", "X-A: line\r\nX-B: forged",
                     "X-A: trailing\r\n", "X-A: \nleading",
                     "X-A: €", "Content-Length: 5", "transfer-encoding: chunked"):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                smtp_sink.header_pair(text)


class WebhookSenderTests(unittest.TestCase):
    """Requests made to a real server on the loopback address."""

    def setUp(self):
        err = io.StringIO()
        self.err = err
        quiet = contextlib.redirect_stderr(err)
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def send(self, hook, message=None, **kwargs):
        sender = hook.sender(**kwargs)
        self.assertTrue(sender.submit(message or delivery()))
        self.assertTrue(sender.wait_idle(REPLY_TIMEOUT))
        return sender

    def test_posts_the_json_with_the_headers_given(self):
        hook = RecordingServer(self)
        self.send(hook, headers=[("X-API-Key", "secret"), ("X-Tag", "a"), ("X-Tag", "b")])
        request = hook.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/hook?key=value")
        # Names as given, case and all, and a repeated one sent each time.
        self.assertIn(("X-API-Key", "secret"), request.headers)
        self.assertEqual(request.header("X-Tag"), ["a", "b"])
        self.assertEqual(request.header("Content-Type"), ["application/json; charset=utf-8"])
        self.assertEqual(request.header("User-Agent"), ["smtp-sink"])
        payload = json.loads(request.body.decode("utf-8"))
        self.assertEqual(payload["envelope"]["from"], "<ups@example.test>")
        self.assertEqual(self.err.getvalue(), "")

    def test_a_header_given_replaces_the_sinks_own(self):
        hook = RecordingServer(self)
        self.send(hook, headers=[("content-type", "application/vnd.alert+json"), ("User-Agent", "ups-bridge")])
        request = hook.requests[0]
        self.assertEqual(request.header("Content-Type"), ["application/vnd.alert+json"])
        self.assertEqual(request.header("User-Agent"), ["ups-bridge"])

    def test_every_method_but_get_carries_the_body(self):
        for method in ("PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                hook = RecordingServer(self)
                self.send(hook, method=method)
                self.assertEqual(hook.requests[0].method, method)
                self.assertIn("envelope", json.loads(hook.requests[0].body))

    def test_a_large_body_goes_in_pieces_and_arrives_whole(self):
        # One sendall of the whole body would have to finish inside the
        # socket's timeout however steadily it was going.
        hook = RecordingServer(self)
        body = b"Subject: big\r\n\r\n" + b"x" * 300_000 + b"\r\n"
        with mock.patch.object(http.client.HTTPConnection, "send",
                               autospec=True, side_effect=http.client.HTTPConnection.send) as send:
            self.send(hook, delivery(body))
        sizes = [len(call.args[1]) for call in send.call_args_list]
        self.assertGreater(len(sizes), 4)
        self.assertLessEqual(max(sizes), smtp_sink.WEBHOOK_CHUNK)
        payload = json.loads(hook.requests[0].body)
        self.assertEqual(base64.b64decode(payload["message"]["raw"]), body)

    def test_get_sends_no_body(self):
        hook = RecordingServer(self)
        self.send(hook, method="GET")
        request = hook.requests[0]
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.body, b"")
        self.assertEqual(request.header("Content-Type"), [])
        self.assertEqual(request.header("Content-Length"), [])

    def test_an_error_status_costs_a_line_and_the_next_still_goes(self):
        hook = RecordingServer(self, status=500)
        sender = self.send(hook)
        self.assertTrue(sender.submit(delivery()))
        self.assertTrue(sender.wait_idle(REPLY_TIMEOUT))
        self.assertEqual(len(hook.requests), 2)
        self.assertEqual(self.err.getvalue().count("HTTP 500"), 2)
        self.assertIn("the message is in the log file", self.err.getvalue())
        self.assertNotIn("Traceback", self.err.getvalue())

    def test_a_redirect_is_reported_not_followed(self):
        target = RecordingServer(self)
        hook = RecordingServer(self, status=302, location=target.url)
        self.send(hook)
        self.assertEqual(target.requests, [])
        self.assertIn("HTTP 302", self.err.getvalue())

    def test_a_server_that_hangs_up_costs_a_line(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def hang_up():
            conn, _ = listener.accept()
            conn.close()

        threading.Thread(target=hang_up, daemon=True).start()
        sender = smtp_sink.WebhookSender(f"http://127.0.0.1:{listener.getsockname()[1]}/")
        sender.start()
        self.addCleanup(sender.stop, REPLY_TIMEOUT)
        self.assertTrue(sender.submit(delivery()))
        self.assertTrue(sender.wait_idle(REPLY_TIMEOUT))
        self.assertIn("webhook http://127.0.0.1:", self.err.getvalue())
        self.assertNotIn("Traceback", self.err.getvalue())

    def test_what_the_webhook_says_reaches_stderr_escaped(self):
        # A reason phrase is the server's to choose, control characters and
        # all, and it goes into the one line the failure costs.
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)

        def answer():
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(REPLY_TIMEOUT)
                request = b""
                while b"\r\n\r\n" not in request:
                    request += conn.recv(65536)
                head, _, body = request.partition(b"\r\n\r\n")
                length = int(re.search(rb"Content-Length: (\d+)", head).group(1))
                while len(body) < length:
                    body += conn.recv(65536)
                conn.sendall(b"HTTP/1.1 500 bad\x1b[2J\rreason\r\nContent-Length: 0\r\n\r\n")

        threading.Thread(target=answer, daemon=True).start()
        sender = smtp_sink.WebhookSender(f"http://127.0.0.1:{listener.getsockname()[1]}/")
        sender.start()
        self.addCleanup(sender.stop, REPLY_TIMEOUT)
        self.assertTrue(sender.submit(delivery()))
        self.assertTrue(sender.wait_idle(REPLY_TIMEOUT))
        err = self.err.getvalue()
        self.assertIn("HTTP 500 bad\\x1b[2J\\rreason", err)
        self.assertNotIn("\x1b", err)
        self.assertNotIn("\r", err)

    def test_a_message_that_cannot_be_built_costs_its_send(self):
        hook = RecordingServer(self)
        with mock.patch.object(smtp_sink, "webhook_payload", side_effect=ValueError("unbuildable")):
            self.send(hook)
        self.assertEqual(hook.requests, [])
        self.assertIn("unbuildable", self.err.getvalue())

    def test_the_url_shown_leaves_out_the_path(self):
        sender = smtp_sink.WebhookSender("https://hooks.example.test:8443/services/T0/B0/secret?token=x")
        self.assertEqual(sender.shown, "https://hooks.example.test:8443")
        self.assertEqual(smtp_sink.WebhookSender("http://[::1]/x").shown, "http://[::1]")

    def test_a_url_without_a_port_gets_the_schemes_own(self):
        # Left to http.client, the colons of a bare IPv6 host are read as a
        # port: http://[::1]/ went to host ":" on port 1.
        for url, host, port in (("http://[::1]/x", "::1", 80), ("https://[::1]/x", "::1", 443),
                                ("http://192.0.2.1/", "192.0.2.1", 80)):
            with self.subTest(url=url):
                sender = smtp_sink.WebhookSender(url)
                self.assertEqual((sender.host, sender.port), (host, port))

    def test_reaches_an_ipv6_server(self):
        class V6Server(http.server.ThreadingHTTPServer):
            address_family = socket.AF_INET6

        hook = RecordingServer(self, server_class=V6Server, host="::1")
        self.send(hook)
        self.assertEqual(len(hook.requests), 1)
        self.assertEqual(hook.requests[0].header("Host"), [f"[::1]:{hook.server.server_address[1]}"])
        self.assertEqual(self.err.getvalue(), "")

    def test_certificates_are_checked_unless_told_otherwise(self):
        checked = smtp_sink.WebhookSender("https://hooks.example.test/").context
        self.assertEqual(checked.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(checked.check_hostname)
        unchecked = smtp_sink.WebhookSender("https://hooks.example.test/", verify=False).context
        self.assertEqual(unchecked.verify_mode, ssl.CERT_NONE)
        self.assertFalse(unchecked.check_hostname)

    def test_a_delivery_gives_its_room_in_the_queue_back(self):
        hook = RecordingServer(self)
        with (
            mock.patch.object(smtp_sink, "WEBHOOK_QUEUE_SIZE", 1),
            mock.patch.object(smtp_sink, "WEBHOOK_QUEUE_BYTES", 100),
        ):
            sender = self.send(hook, delivery(b"x" * 80))
            self.assertEqual((sender.waiting, sender.waiting_bytes), (0, 0))
            # Fits only if the first gave back both its place and its bytes.
            self.assertTrue(sender.submit(delivery(b"y" * 80)))
            self.assertTrue(sender.wait_idle(REPLY_TIMEOUT))
        self.assertEqual(len(hook.requests), 2)

    def test_the_certificate_setting_reaches_the_connection(self):
        for verify in (True, False):
            with self.subTest(verify=verify):
                sender = smtp_sink.WebhookSender("https://hooks.example.test/x", verify=verify)
                answer = mock.Mock(status=204, reason="No Content")
                answer.read.return_value = b""
                with mock.patch.object(http.client, "HTTPSConnection") as connection:
                    connection.return_value.getresponse.return_value = answer
                    sender.deliver(delivery())
                connection.assert_called_once_with(
                    "hooks.example.test", 443, timeout=smtp_sink.WEBHOOK_TIMEOUT, context=sender.context
                )
                expected = ssl.CERT_REQUIRED if verify else ssl.CERT_NONE
                self.assertEqual(connection.call_args.kwargs["context"].verify_mode, expected)

    def test_a_plain_http_connection_carries_the_timeout(self):
        sender = smtp_sink.WebhookSender("http://hooks.example.test/x")
        answer = mock.Mock(status=204, reason="No Content")
        answer.read.return_value = b""
        with mock.patch.object(http.client, "HTTPConnection") as connection:
            connection.return_value.getresponse.return_value = answer
            sender.deliver(delivery())
        connection.assert_called_once_with("hooks.example.test", 80, timeout=smtp_sink.WEBHOOK_TIMEOUT)

    def test_a_webhook_that_never_answers_times_out(self):
        hook = RecordingServer(self, stall=True)
        with mock.patch.object(smtp_sink, "WEBHOOK_TIMEOUT", 0.3):
            self.send(hook)
        self.assertEqual(len(hook.requests), 1)
        self.assertIn("timed out", self.err.getvalue())
        self.assertIn("failed, not sent", self.err.getvalue())

    def test_a_given_host_or_accept_encoding_is_sent_once(self):
        # http.client adds both of its own unless told not to, and a second
        # Host header is one a server may well refuse.
        hook = RecordingServer(self)
        self.send(hook, headers=[("Host", "hooks.example.test"), ("Accept-Encoding", "gzip")])
        request = hook.requests[0]
        self.assertEqual(request.header("Host"), ["hooks.example.test"])
        self.assertEqual(request.header("Accept-Encoding"), ["gzip"])

    def test_the_queue_is_capped_by_count_and_by_bytes(self):
        sender = smtp_sink.WebhookSender("http://192.0.2.1/")  # never started
        with mock.patch.object(smtp_sink, "WEBHOOK_QUEUE_SIZE", 2):
            self.assertTrue(sender.submit(delivery()))
            self.assertTrue(sender.submit(delivery()))
            self.assertFalse(sender.submit(delivery()))
        sender = smtp_sink.WebhookSender("http://192.0.2.1/")
        with mock.patch.object(smtp_sink, "WEBHOOK_QUEUE_BYTES", 100):
            self.assertTrue(sender.submit(delivery(b"x" * 60)))
            self.assertFalse(sender.submit(delivery(b"x" * 60)))
            self.assertTrue(sender.submit(delivery(b"x" * 40)))


class WebhookCommandLineTests(unittest.TestCase):
    """The webhook options as `main` reads them."""

    def run_main(self, *extra):
        class Stop(Exception):
            pass

        async def start_server(*args, **kwargs):
            raise Stop

        argv = ["smtp_sink.py", "--bind", "127.0.0.1", *extra]
        err = io.StringIO()
        self.addCleanup(setattr, smtp_sink, "webhook", None)
        with (
            mock.patch.object(sys, "argv", argv),
            mock.patch.object(smtp_sink.asyncio, "start_server", start_server),
            contextlib.redirect_stderr(err),
        ):
            try:
                asyncio.run(smtp_sink.main())
            except Stop:
                pass
            except SystemExit as exc:
                return exc.code, err.getvalue()
        if smtp_sink.webhook is not None:
            self.addCleanup(smtp_sink.webhook.stop, REPLY_TIMEOUT)
        return None, err.getvalue()

    def test_output_a_console_cannot_encode_is_escaped_not_raised(self):
        # A Windows console redirected to a file encodes cp1252 strictly, and
        # the address in "logged message" can hold anything.
        console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", newline="\n")
        with mock.patch.object(sys, "stdout", console):
            self.run_main()
            print("from <üćā@example.test>")
        console.flush()
        self.assertEqual(console.buffer.getvalue(), b"from <\xfc\\u0107\\u0101@example.test>\n")

    def test_only_the_url_is_needed(self):
        code, _ = self.run_main("--webhook-url", "https://hooks.example.test/mail")
        self.assertIsNone(code)
        sender = smtp_sink.webhook
        self.assertEqual(sender.method, "POST")
        self.assertEqual(sender.headers, [])
        self.assertEqual(sender.context.verify_mode, ssl.CERT_REQUIRED)

    def test_takes_every_option(self):
        code, _ = self.run_main(
            "--webhook-url", "https://hooks.example.test/mail", "--webhook-method", "put",
            "--webhook-disable-ssl-verify", "--header=X-A: 1", "--header", "X-B: 2",
        )
        self.assertIsNone(code)
        sender = smtp_sink.webhook
        self.assertEqual(sender.method, "PUT")
        self.assertEqual(sender.headers, [("X-A", "1"), ("X-B", "2")])
        self.assertEqual(sender.context.verify_mode, ssl.CERT_NONE)

    def test_a_webhook_option_without_the_url_is_refused(self):
        for extra in (("--webhook-method", "GET"), ("--webhook-disable-ssl-verify",),
                      ("--header", "X-A: 1")):
            with self.subTest(extra=extra):
                code, err = self.run_main(*extra)
                self.assertEqual(code, 2)
                self.assertIn("needs --webhook-url", err)

    def test_a_bad_url_or_header_is_refused_cleanly(self):
        for extra in (("--webhook-url", "ftp://example.test/"),
                      ("--webhook-url", "http://example.test/", "--header", "nonsense")):
            with self.subTest(extra=extra):
                code, err = self.run_main(*extra)
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)

    def test_a_refused_header_does_not_print_its_value(self):
        for header in ("X-Token: s3cret€", "X-Token: s3cret\r\nX-B: 1", "s3cret"):
            with self.subTest(header=header):
                code, err = self.run_main("--webhook-url", "http://example.test/", "--header", header)
                self.assertEqual(code, 2)
                self.assertNotIn("s3cret", err)


if __name__ == "__main__":
    unittest.main()
