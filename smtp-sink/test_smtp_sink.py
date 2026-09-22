"""Tests for smtp_sink.py. Standard library only, so nothing to install.

    python -m unittest

Three layers. The first two call `write_entry` and `forward_syslog` directly,
because what they write is the whole point of the tool and neither needs a
socket. The third starts the real server on an ephemeral port on the loopback
address and speaks SMTP to it, so the replies and the log file are checked
against the same code path a mail client drives.

Nothing here contacts a syslog server. The module level `syslog` logger is
replaced with a mock, which is also how the "no syslog configured" case is
tested: that is the module's own default.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import shutil
import smtplib
import tempfile
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

    def write(self, body, mail_from="<a@example.test>", rcpts=("<b@example.test>",)):
        smtp_sink.write_entry(str(self.log), ("192.0.2.7", 41234), mail_from, list(rcpts), body)
        return self.log.read_text(encoding="utf-8")

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

    def test_appends_rather_than_replacing(self):
        self.write("first\n")
        text = self.write("second\n")
        self.assertIn("first", text)
        self.assertIn("second", text)
        self.assertEqual(text.count("Received:"), 2)


class ForwardSyslogTests(unittest.TestCase):
    def setUp(self):
        self.logger = mock.Mock()
        patcher = mock.patch.object(smtp_sink, "syslog", self.logger)
        patcher.start()
        self.addCleanup(patcher.stop)

    def forward(self, body, include_body=False, max_len=2000):
        smtp_sink.forward_syslog(
            ("192.0.2.7", 41234), "<a@example.test>", ["<b@example.test>"],
            body, include_body, max_len,
        )
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

    def test_a_multipart_message_with_no_text_part_forwards_an_empty_body(self):
        body = MULTIPART.replace("text/plain", "text/html")
        self.assertIn('body=""', self.forward(body, include_body=True))

    def test_the_subject_of_a_multipart_message_is_still_summarised(self):
        self.assertIn('subject="an alert"', self.forward(MULTIPART))

    def test_truncates_a_long_line(self):
        line = self.forward("Subject: " + "x" * 500 + "\n\nbody\n", max_len=80)
        self.assertEqual(len(line), 80)
        self.assertTrue(line.endswith("..."))

    def test_does_nothing_when_no_syslog_is_configured(self):
        with mock.patch.object(smtp_sink, "syslog", None):
            smtp_sink.forward_syslog(("192.0.2.7", 1), "a", ["b"], "Subject: s\n\nx\n", False, 2000)
        self.logger.info.assert_not_called()


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
            lambda r, w: smtp_sink.handle_client(r, w, self.args), "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]
        self.clients = []
        # The handler prints a line per accepted message, which is noise here.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    async def asyncTearDown(self):
        # The clients go first, and this is not tidiness. `wait_closed` waits
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

        text = self.log.read_text(encoding="utf-8")
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

    async def test_the_connection_survives_a_refused_message(self):
        reader, writer = await self.connect()
        with mock.patch.object(smtp_sink, "MAX_MESSAGE_BYTES", 200):
            await self.send_message(reader, writer, "x" * 500)
        # The same connection, with a message that fits. This also shows the
        # refusal cleared the envelope rather than leaving it behind.
        reply = await self.send_message(reader, writer, "Subject: small\r\n\r\nfits")
        self.assertTrue(reply[0].startswith("250"), reply)
        self.assertIn("fits", self.log.read_text(encoding="utf-8"))

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

        text = self.log.read_text(encoding="utf-8")
        self.assertIn("\n.hidden line\n", text)
        self.assertIn("plain line", text)
        self.assertIn("a@example.test", text)


if __name__ == "__main__":
    unittest.main()
