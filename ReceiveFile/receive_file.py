#!/usr/bin/env python3
"""Receive one file from a browser, save it, and exit.

Serves a small page with a file picker, a box for the name to save it as, and
an upload button, on every network interface. The first file sent to it is
saved into the current directory, and then the program exits. Standard library
only.

    python receive_file.py
"""

from __future__ import annotations

import http.server
import socket
import sys
import threading
import urllib.parse
from pathlib import Path

# The port to listen on. Edit this line to use another.
PORT = 3000

# How much of an upload is read into memory at a time on its way to disk.
CHUNK = 1024 * 1024

# How long a connection may sit idle, in seconds, before it is dropped, so a
# client that stalls partway through an upload cannot hold the server forever.
IDLE_TIMEOUT = 60

# The script sends the file as the raw body of the POST, with the name to save
# it as in the query string, which lets the server stream it straight to disk.
PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ReceiveFile</title>
<form id="form">
  <p><input type="file" id="file" required></p>
  <p><label>Save as <input type="text" id="dest" size="40" required></label></p>
  <p><button id="upload">Upload</button></p>
</form>
<p id="message"></p>
<script>
const file = document.getElementById("file");
const dest = document.getElementById("dest");
const upload = document.getElementById("upload");
const message = document.getElementById("message");
file.addEventListener("change", () => {
  dest.value = file.files.length ? file.files[0].name : "";
});
document.getElementById("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  upload.disabled = true;
  message.textContent = "Uploading " + file.files[0].name;
  try {
    const response = await fetch("/?name=" + encodeURIComponent(dest.value),
                                 {method: "POST", body: file.files[0]});
    message.textContent = await response.text();
    if (response.ok) {
      file.disabled = true;
      dest.disabled = true;
      return;
    }
  } catch (error) {
    message.textContent = "The upload failed: " + error;
  }
  upload.disabled = false;
});
</script>
</html>
"""


def unusable(name: str) -> str | None:
    """Why a name cannot be saved as, or None when it can.

    Only a plain file name is taken, never a path, so nothing can be written
    outside the directory the program was started in.
    """
    if not name:
        return "No name to save as was given."
    if name in (".", ".."):
        return f"{name!r} is not a file name."
    if "/" in name or "\\" in name or "\0" in name:
        return f"{name!r} is not a plain file name; a path cannot be given."
    # On Windows a colon names a drive, as in D:name, which joins to a path on
    # that drive, or a hidden stream inside a file, as in name:stream.
    if ":" in name:
        return f"{name!r} has a colon in it, which a file name cannot."
    return None


class Handler(http.server.BaseHTTPRequestHandler):
    server: Server
    timeout = IDLE_TIMEOUT

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/":
            self.send_error(404)
            return
        self.reply(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        name = query.get("name", [""])[0]
        try:
            length = int(self.headers["Content-Length"])
        except (TypeError, ValueError):
            self.reply(411, "The upload did not say how long it is.")
            return
        if length < 0:
            self.reply(400, "The upload gave a negative length.")
            return

        # One upload at a time, so that only one can ever be saved.
        with self.server.lock:
            if self.server.saved is not None:
                self.refuse(length, 503, "A file has already been received.")
                return
            reason = unusable(name)
            if reason:
                self.refuse(length, 400, reason)
                return
            target = self.server.directory / name
            # Whatever else the platform makes of a name, it must land here.
            if target.parent != self.server.directory or target.name != name:
                self.refuse(length, 400, f"{name!r} is not a plain file name.")
                return
            created = False
            try:
                # "x" creates it, and fails if it is there already, in one step.
                with open(target, "xb") as out:
                    created = True
                    received = self.copy(out, length)
            except FileExistsError:
                self.refuse(length, 409, f"A file called {name!r} is already there. "
                                         "Choose another name.")
                return
            except OSError as error:
                if not created:
                    self.refuse(length, 400, f"Cannot save as {name!r}: {error.strerror}.")
                    return
                target.unlink(missing_ok=True)
                self.try_reply(500, f"Receiving {name!r} failed, so nothing was kept: {error}.")
                return
            if received < length:
                target.unlink(missing_ok=True)
                self.try_reply(400, f"The upload stopped after {received} of "
                                    f"{length} bytes, so nothing was kept.")
                return

            self.server.saved = target
        # Stopping does not wait on the sender hearing about it: the file is
        # saved, and a sender that has gone must not keep the server running.
        self.try_reply(200, f"Saved {name} ({length} bytes). ReceiveFile has now stopped.")
        # shutdown() waits for serve_forever() to return, which it cannot do
        # while this handler is still running, so it has to be another thread.
        threading.Thread(target=self.server.shutdown).start()

    def copy(self, out, length: int) -> int:
        """Copies up to length bytes of the body into out, and says how many."""
        received = 0
        while received < length:
            chunk = self.rfile.read(min(CHUNK, length - received))
            if not chunk:
                break
            out.write(chunk)
            received += len(chunk)
        return received

    def refuse(self, length: int, status: int, text: str) -> None:
        """Replies after reading the body and throwing it away.

        A browser still sending the body when the reply arrives and the
        connection closes may report a dropped connection rather than the
        reply, so the body is taken in full first.
        """
        try:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(CHUNK, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
        except OSError:
            return
        self.try_reply(status, text)

    def try_reply(self, status: int, text: str) -> None:
        """Replies, unless the client has already gone."""
        try:
            self.reply(status, text)
        except OSError:
            pass

    def reply(self, status: int, body: bytes | str,
              content_type: str = "text/plain; charset=utf-8") -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], directory: Path) -> None:
        super().__init__(address, Handler)
        self.directory = directory
        self.lock = threading.Lock()
        self.saved: Path | None = None


def lan_address() -> str | None:
    """This machine's address on the network it would reach others through.

    Connecting a UDP socket sends nothing; it only makes the system choose the
    interface, whose address is then read back.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def main() -> int:
    directory = Path.cwd()
    try:
        server = Server(("", PORT), directory)
    except OSError as error:
        print(f"receive_file.py: cannot listen on port {PORT}: {error.strerror}",
              file=sys.stderr)
        return 1
    with server:
        print(f"Waiting for one file, to save into {directory}")
        print(f"  http://localhost:{PORT}/")
        address = lan_address()
        if address:
            print(f"  http://{address}:{PORT}/")
        print("Ctrl+C stops it without receiving anything.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("Stopped. Nothing was received.")
            return 1
    print(f"Saved {server.saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
