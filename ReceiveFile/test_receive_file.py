"""Tests for receive_file.py. Standard library only, so nothing to install.

    python -m unittest

Each test starts the real server on the loopback address, on a port the
operating system picks, saving into a fresh temporary directory, and talks
HTTP to it. Nothing leaves the machine.
"""

from __future__ import annotations

import http.client
import socket
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import receive_file


class ReceiveFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
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

    def request(self, method: str, path: str,
                body: bytes | None = None) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body)
            response = connection.getresponse()
            return response.status, response.read().decode()
        finally:
            connection.close()

    def upload(self, name: str, body: bytes) -> tuple[int, str]:
        return self.request("POST", "/?name=" + urllib.parse.quote(name, safe=""), body)

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

    def test_the_page_has_a_picker_a_name_box_and_a_button(self) -> None:
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn('<input type="file" id="file"', page)
        self.assertIn('<input type="text" id="dest"', page)
        self.assertIn("<button", page)
        # The name box is filled from the picked file, and is what is sent.
        self.assertIn("dest.value = file.files.length ? file.files[0].name", page)
        self.assertIn('"/?name=" + encodeURIComponent(dest.value)', page)

    def test_any_other_path_is_not_found(self) -> None:
        self.assertEqual(self.request("GET", "/favicon.ico")[0], 404)

    def test_a_file_is_saved_as_sent_and_the_server_stops(self) -> None:
        body = bytes(range(256)) * 5000
        status, text = self.upload("copy of data.bin", body)
        self.assertEqual(status, 200)
        self.assertIn("Saved copy of data.bin", text)
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
        self.assert_still_serving()

    def test_names_that_are_not_plain_file_names_are_refused(self) -> None:
        for name in ["", ".", "..", "sub/x.txt", "../x.txt", "sub\\x.txt", "x\0y",
                     "D:escape.txt", "C:x.txt", "x.txt:stream"]:
            with self.subTest(name=name):
                status, _ = self.upload(name, b"data")
                self.assertEqual(status, 400)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertFalse((self.directory.parent / "x.txt").exists())
        self.assert_still_serving()

    def test_an_upload_without_a_length_is_refused(self) -> None:
        reply = self.raw(b"POST /?name=x.txt HTTP/1.1\r\nHost: t\r\n\r\n")
        self.assertTrue(reply.startswith(b"HTTP/1.0 411"), reply)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_still_serving()

    def test_an_upload_cut_short_leaves_nothing_behind(self) -> None:
        reply = self.raw(b"POST /?name=part.bin HTTP/1.1\r\nHost: t\r\n"
                         b"Content-Length: 100\r\n\r\n" + b"x" * 10)
        self.assertIn(b"stopped after 10 of 100 bytes", reply)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assert_still_serving()

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
