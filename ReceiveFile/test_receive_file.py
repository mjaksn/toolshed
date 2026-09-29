"""Tests for receive_file.py. Standard library only, so nothing to install.

    python -m unittest

Each test starts the real server on the loopback address, on a port the
operating system picks, saving into a fresh temporary directory, and talks
HTTP to it. Nothing leaves the machine.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import receive_file

# Where receive_file.py is, for running it in a separate process.
HERE = Path(__file__).resolve().parent

# What the page sends with an upload, which the server insists on.
UPLOAD_HEADER = {"X-ReceiveFile": "upload"}


def unused_drive() -> str:
    """A drive letter with no drive behind it on this machine."""
    for letter in "ZYXWVUTSRQPONMLKJIHG":
        if not os.path.exists(f"{letter}:\\"):
            return letter
    raise unittest.SkipTest("every drive letter from G to Z is in use")


def eventually(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    """Waits for condition to hold, and says whether it did in time."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


class ReceiveFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # The directory saved into sits inside one of the test's own, so that
        # anything written beside it rather than in it can be seen.
        self.root = Path(self.tmp.name)
        self.directory = self.root / "incoming"
        self.directory.mkdir()
        self.server = receive_file.Server(("127.0.0.1", 0), self.directory)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05})
        self.thread.start()

    def tearDown(self) -> None:
        if self.thread.is_alive():
            self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def upload(self, name: str, body: bytes,
               headers: dict[str, str] | None = None) -> tuple[int, str]:
        """Uploads as the page does, with its header unless others are given."""
        return self.request("POST", "/?name=" + urllib.parse.quote(name, safe=""), body,
                            UPLOAD_HEADER if headers is None else headers)

    def raw(self, data: bytes) -> bytes:
        """Sends bytes as written, then reads whatever comes back."""
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
            return b"".join(chunks)

    def assert_still_serving(self) -> None:
        self.assertTrue(self.thread.is_alive())
        self.assertEqual(self.request("GET", "/")[0], 200)

    def test_the_port_is_3000_unless_edited(self) -> None:
        self.assertEqual(receive_file.PORT, 3000)

    def test_a_second_server_cannot_take_a_port_in_use(self) -> None:
        # On Windows the default socket options would let it, so the second
        # would wait for a file that always goes to the first.
        for host in ("127.0.0.1", ""):
            with self.subTest(host=host), self.assertRaises(OSError):
                receive_file.Server((host, self.port), self.directory).server_close()

    def test_the_port_can_be_used_again_straight_after(self) -> None:
        # Claiming the port exclusively must not stop the next run starting
        # while the connections of the last are still closing.
        self.assertEqual(self.upload("first.txt", b"1")[0], 200)
        self.thread.join(5)
        self.server.server_close()
        receive_file.Server(("127.0.0.1", self.port), self.directory).server_close()

    def test_the_page_has_a_picker_a_name_box_and_a_button(self) -> None:
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('<input type="file" id="file"', page)
        self.assertIn('<input type="text" id="dest"', page)
        self.assertIn("<button", page)
        # The name box is filled from the picked file, and is what is sent.
        self.assertIn("dest.value = file.files.length ? file.files[0].name", page)
        self.assertIn('"/?name=" + encodeURIComponent(dest.value)', page)
        self.assertIn('headers: {"X-ReceiveFile": "upload"}', page)

    def test_any_other_path_is_not_found(self) -> None:
        self.assertEqual(self.request("GET", "/favicon.ico")[0], 404)

    def test_a_file_is_saved_as_sent_and_the_server_stops(self) -> None:
        body = bytes(range(256)) * 5000
        status, text = self.upload("copy of data.bin", body)
        self.assertEqual(status, 200)
        # The README promises the page says what was saved and how big it is.
        self.assertIn(f"Saved copy of data.bin ({len(body)} bytes)", text)
        self.assertEqual((self.directory / "copy of data.bin").read_bytes(), body)
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.server.saved, self.directory / "copy of data.bin")

    def test_the_server_stops_even_when_the_sender_has_gone(self) -> None:
        # As when the sender disconnects between sending the file and reading
        # the reply, so that writing the reply fails.
        failing = mock.patch.object(receive_file.Handler, "reply",
                                    side_effect=ConnectionResetError)
        with failing, self.assertRaises(http.client.HTTPException):
            self.upload("gone.txt", b"kept")
        self.assertEqual((self.directory / "gone.txt").read_bytes(), b"kept")
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive())

    def test_an_empty_file_is_saved(self) -> None:
        self.assertEqual(self.upload("empty.txt", b"")[0], 200)
        self.assertEqual((self.directory / "empty.txt").read_bytes(), b"")

    def test_an_existing_file_is_never_replaced(self) -> None:
        (self.directory / "taken.txt").write_bytes(b"original")
        status, text = self.upload("taken.txt", b"replacement")
        self.assertEqual(status, 409)
        self.assertIn("Choose another name", text)
        self.assertEqual((self.directory / "taken.txt").read_bytes(), b"original")
        # The page says to choose another name, which then works.
        self.assertEqual(self.upload("taken (2).txt", b"replacement")[0], 200)
        self.assertEqual((self.directory / "taken (2).txt").read_bytes(), b"replacement")

    def test_names_that_are_not_plain_file_names_are_refused(self) -> None:
        # sub exists, so a path into it would be saved there if one got
        # through, rather than failing for want of the directory.
        sub = self.directory / "sub"
        sub.mkdir()
        # A name on another drive uses one with no drive behind it, so that if
        # the checks ever let it through it fails rather than writing there.
        # One on this drive would land here, where the listing below sees it.
        this_drive = self.directory.drive or "C:"
        for name in ["", ".", "..", "sub/x.txt", "../x.txt", "sub\\x.txt", "x\0y",
                     f"{unused_drive()}:escape.txt", f"{this_drive}x.txt", "x.txt:stream"]:
            with self.subTest(name=name):
                status, _ = self.upload(name, b"data")
                self.assertEqual(status, 400)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["sub"])
        self.assertEqual(list(sub.iterdir()), [])
        self.assertEqual([path.name for path in self.root.iterdir()], ["incoming"])
        self.assert_still_serving()

    def test_an_upload_not_from_the_page_is_refused(self) -> None:
        # As a page from anywhere else would send it, with no way to add the
        # header without a preflight request this server does not answer.
        for headers in ({}, {"X-ReceiveFile": "something else"}):
            with self.subTest(headers=headers):
                status, text = self.upload("forged.txt", b"data", headers)
                self.assertEqual(status, 403)
                self.assertIn("own page", text)
        self.assertEqual(self.request("OPTIONS", "/")[0], 501)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIsNone(self.server.saved)
        self.assert_still_serving()

    def test_windows_device_names_are_refused_everywhere(self) -> None:
        # On Windows these open a device, so NUL would throw the upload away
        # and report it saved. Any case, any extension, spaces before the dot.
        for name in ["NUL", "nul", "NUL .txt", "con.txt", "Aux.tar.gz", "COM1", "lpt9.log",
                     "COM¹", "CONIN$"]:
            with self.subTest(name=name):
                status, text = self.upload(name, b"data")
                self.assertEqual(status, 400)
                self.assertIn("device", text)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_still_serving()

    def test_names_with_control_characters_are_refused(self) -> None:
        # They would reach the terminal the saved name is printed to, where an
        # escape sequence can rewrite what is on the screen.
        for name in ["x\0y", "clear\x1b[2Jscreen.txt", "two\nlines.txt", "tab\t.txt",
                     "del\x7f.txt"]:
            with self.subTest(name=name):
                status, text = self.upload(name, b"data")
                self.assertEqual(status, 400)
                self.assertIn("control character", text)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_still_serving()

    def test_names_ending_in_a_dot_or_space_are_refused(self) -> None:
        # Windows would save report.pdf. as report.pdf, which is not the name
        # the page and the terminal report.
        for name in ["report.pdf.", "report.pdf ", "notes...", "x. "]:
            with self.subTest(name=name):
                status, text = self.upload(name, b"data")
                self.assertEqual(status, 400)
                self.assertIn("dot or a space", text)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_still_serving()

    def test_an_upload_without_a_length_is_refused(self) -> None:
        reply = self.raw(b"POST /?name=x.txt HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 411"), reply)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_still_serving()

    def test_an_upload_cut_short_leaves_nothing_behind(self) -> None:
        reply = self.raw(b"POST /?name=part.bin HTTP/1.1\r\nHost: t\r\nX-ReceiveFile: upload\r\n"
                         b"Content-Length: 100\r\n\r\n" + b"x" * 10)
        self.assertIn(b"stopped after 10 of 100 bytes", reply)
        self.assertEqual(list(self.directory.iterdir()), [])
        # Nothing was kept, so the same name is free for the upload to be tried
        # again.
        self.assertEqual(self.upload("part.bin", b"x" * 100)[0], 200)
        self.assertEqual((self.directory / "part.bin").read_bytes(), b"x" * 100)

    def test_stopping_mid_upload_keeps_nothing(self) -> None:
        # What Ctrl+C does, while a file is still arriving.
        partial = self.directory / "big.bin"
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(b"POST /?name=big.bin HTTP/1.1\r\nHost: t\r\nX-ReceiveFile: upload\r\n"
                         b"Content-Length: 1000000\r\n\r\n" + b"x" * 1000)
            self.assertTrue(eventually(partial.exists))
            self.server.abort()
            self.assertFalse(partial.exists())
        self.assertIsNone(self.server.saved)
        self.assertEqual(self.upload("later.txt", b"late")[0], 503)
        self.assertEqual(list(self.directory.iterdir()), [])

    def run_main(self, saved: Path | None, again: bool = False) -> tuple[int, str]:
        """Runs main() as far as a Ctrl+C, with saved as what had been saved.

        With again, a second Ctrl+C lands while the first is clearing up.
        """

        class Interrupted:
            def __init__(self, address: tuple[str, int], directory: Path) -> None:
                self.saved = None

            def __enter__(self):
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def abort(self) -> None:
                if again:
                    raise KeyboardInterrupt
                # As an upload that finishes while Ctrl+C is handled.
                self.saved = saved

        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(receive_file, "Server", Interrupted))
            stack.enter_context(mock.patch.object(receive_file, "lan_address",
                                                  return_value=None))
            stack.enter_context(contextlib.redirect_stdout(out))
            safe_output = stack.enter_context(mock.patch.object(receive_file, "safe_output"))
            status = receive_file.main()
        safe_output.assert_called_once_with()
        return status, out.getvalue()

    def test_ctrl_c_before_a_save_says_nothing_was_saved(self) -> None:
        status, out = self.run_main(None)
        self.assertEqual(status, 1)
        self.assertIn("Nothing was saved", out)

    def test_stopping_waits_however_long_the_upload_takes_to_clear_up(self) -> None:
        # As a handler slow to finish, writing to a stalled network drive. An
        # earlier version gave up after a fixed wait, set tiny here so that
        # it shows at once, and could exit with part of the file behind.
        self.server.lock.acquire()
        aborting = threading.Thread(target=self.server.abort)
        with mock.patch.object(receive_file, "ABORT_WAIT", 0.2, create=True):
            aborting.start()
            time.sleep(1)
            still_waiting = aborting.is_alive()
            self.server.lock.release()
            aborting.join(5)
        self.assertTrue(still_waiting)
        self.assertFalse(aborting.is_alive())

    def test_a_second_ctrl_c_stops_at_once_and_says_so(self) -> None:
        status, out = self.run_main(None, again=True)
        self.assertEqual(status, 1)
        self.assertIn("part of it may be left behind", out)

    def test_ctrl_c_just_after_a_save_still_reports_it(self) -> None:
        saved = self.directory / "done.txt"
        status, out = self.run_main(saved)
        self.assertEqual(status, 0)
        self.assertIn(f"Saved {saved}", out)
        self.assertNotIn("Nothing", out)

    def test_a_name_the_console_cannot_show_is_printed_escaped(self) -> None:
        # As when output is redirected on Windows, in a code page with no way
        # to write the name, which must not fail the run after the save.
        script = ("import receive_file\n"
                  "receive_file.safe_output()\n"
                  "print('Saved \\u8d44\\u6599.txt')\n")
        env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        result = subprocess.run([sys.executable, "-c", script], cwd=HERE, env=env,
                                capture_output=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), rb"Saved \u8d44\u6599.txt")

    def test_nothing_more_is_saved_once_a_file_has_been(self) -> None:
        # As an upload waiting on the lock finds it, when another has just
        # been saved and the server has not quite stopped.
        self.server.saved = self.directory / "earlier.txt"
        status, text = self.upload("second.txt", b"2")
        self.assertEqual(status, 503)
        self.assertIn("already been received", text)
        self.assertFalse((self.directory / "second.txt").exists())


if __name__ == "__main__":
    unittest.main()
