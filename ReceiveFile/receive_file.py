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
import ipaddress
import select
import socket
import sys
import threading
import unicodedata
import urllib.parse
from pathlib import Path

# The port to listen on. Edit this line to use another.
PORT = 3000

# How much of an upload is read into memory at a time on its way to disk.
CHUNK = 1024 * 1024

# How long a connection may sit idle, in seconds, before it is dropped, so a
# client that stalls partway through an upload cannot hold the server forever.
IDLE_TIMEOUT = 60

# How often, in seconds, an upload waiting for data checks whether to stop.
POLL = 0.25

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
<p id="message" role="status"></p>
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
                                 {method: "POST", body: file.files[0],
                                  headers: {"X-ReceiveFile": "upload"}});
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


# Names Windows keeps for devices, in any folder, in any case and whatever
# follows a dot. Saving as NUL would throw the file away and report it saved.
# They are refused everywhere, so a name works or not whatever the platform.
DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{port}{number}" for port in ("COM", "LPT") for number in "0123456789¹²³"
}


def unusable(name: str) -> str | None:
    """Why a name cannot be saved as, or None when it can.

    Only a plain file name is taken, never a path, so nothing can be written
    outside the directory the program was started in.
    """
    if not name:
        return "No name to save as was given."
    if name in (".", ".."):
        return f"{name!r} is not a file name."
    if "/" in name or "\\" in name:
        return f"{name!r} is not a plain file name; a path cannot be given."
    # Line breaks, escape sequences and the like would reach the terminal the
    # saved name is printed to, and Windows refuses most of them anyway. The
    # Unicode class covers all three ranges: C0, DEL and C1, where U+009B
    # starts an escape sequence much as ESC [ does.
    if any(unicodedata.category(char) == "Cc" for char in name):
        return f"{name!r} has a control character in it."
    # On Windows a colon names a drive, as in D:name, which joins to a path on
    # that drive, or a hidden stream inside a file, as in name:stream.
    if ":" in name:
        return f"{name!r} has a colon in it, which a file name cannot."
    # Windows drops these from the end of a name when it creates the file, so
    # it would be saved under a different name from the one reported.
    if name.endswith((".", " ")):
        return f"{name!r} ends in a dot or a space, which Windows would drop."
    if name.split(".")[0].rstrip(" ").upper() in DEVICES:
        return f"{name!r} is the name of a device on Windows, not a file."
    return None


WRONG_HOST = ("ReceiveFile answers only at this machine's own name or address, "
              "such as the ones it printed when it started.")


def direct_host(host: str | None) -> bool:
    """Whether a Host header names this machine directly.

    That is an IP address, localhost, or this machine's own name, which
    covers every address the program prints. A web page elsewhere can point
    a domain it controls at this machine's address, which makes the browser
    treat the page and this server as one site, so that it sends the upload
    header with no preflight to stop it. Such a request still names that
    domain in Host, and is refused.
    """
    if not host:
        return False
    host = host.strip()
    if host.startswith("["):
        name = host[1:host.find("]")]
    elif host.count(":") == 1:
        name = host.split(":")[0]
    else:
        name = host
    name = name.rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    here = socket.gethostname().lower()
    return name in {"localhost", here, f"{here}.local", socket.getfqdn().lower()}


class Handler(http.server.BaseHTTPRequestHandler):
    server: Server
    timeout = IDLE_TIMEOUT
    # Unbuffered, so that waiting on the socket says whether there is more to
    # read: a buffer could hold data the wait cannot see. See read_some().
    rbufsize = 0

    def do_GET(self) -> None:
        if not direct_host(self.headers.get("Host")):
            self.reply(403, WRONG_HOST)
            return
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
        if not direct_host(self.headers.get("Host")):
            self.refuse(length, 403, WRONG_HOST)
            return
        # Only this page's own script sends this. A page from anywhere else
        # open in a browser that can reach the server cannot add it without
        # first asking, in a preflight request this server does not answer,
        # so it cannot use up the one upload. See direct_host() for a page
        # that tries to pass as this one.
        if self.headers.get("X-ReceiveFile") != "upload":
            self.refuse(length, 403, "Uploads are taken only from ReceiveFile's own page.")
            return

        # One upload at a time, so that only one can ever be saved.
        with self.server.lock:
            saved = self.receive(name, length)
        if saved:
            # Stopping does not wait on the sender hearing about it: the file
            # is saved, and a sender that has gone must not keep it running.
            self.try_reply(200, f"Saved {name} ({length} bytes). ReceiveFile has now stopped.")
            # shutdown() waits for serve_forever() to return, which it cannot
            # do while this handler is running, so it has to be another thread.
            threading.Thread(target=self.server.shutdown).start()

    def receive(self, name: str, length: int) -> bool:
        """Saves the body as name and says so, or refuses it and says why."""
        if self.server.stopping:
            self.refuse(length, 503, "ReceiveFile is stopping.")
            return False
        if self.server.saved is not None:
            self.refuse(length, 503, "A file has already been received.")
            return False
        reason = unusable(name)
        if reason:
            self.refuse(length, 400, reason)
            return False
        target = self.server.directory / name
        # Whatever else the platform makes of a name, it must land here.
        if target.parent != self.server.directory or target.name != name:
            self.refuse(length, 400, f"{name!r} is not a plain file name.")
            return False
        created = False
        try:
            # "x" creates it, and fails if it is there already, in one step.
            with open(target, "xb") as out:
                created = True
                received = self.copy(out, length)
        except FileExistsError:
            self.refuse(length, 409, f"A file called {name!r} is already there. "
                                     "Choose another name.")
            return False
        except OSError as error:
            if not created:
                self.refuse(length, 400, f"Cannot save as {name!r}: {error.strerror}.")
                return False
            target.unlink(missing_ok=True)
            self.try_reply(500, f"Receiving {name!r} failed, so nothing was kept: {error}.")
            return False
        if received < length:
            target.unlink(missing_ok=True)
            self.try_reply(400, f"The upload stopped after {received} of "
                                f"{length} bytes, so nothing was kept.")
            return False
        self.server.saved = target
        return True

    def copy(self, out, length: int) -> int:
        """Copies up to length bytes of the body into out, and says how many."""
        received = 0
        while received < length:
            chunk = self.read_some(min(CHUNK, length - received))
            if not chunk:
                break
            out.write(chunk)
            received += len(chunk)
        return received

    def read_some(self, limit: int) -> bytes:
        """Reads up to limit bytes of the body as they arrive, or b"" at its end.

        It waits in short steps rather than blocking in one read, so that it
        notices the server stopping even while a sender is slow or has
        stalled; a read blocked in another thread cannot be woken the same
        way on every platform. It gives up once nothing has arrived for
        IDLE_TIMEOUT seconds.
        """
        waited = 0.0
        while True:
            if self.server.stopping:
                raise ConnectionAbortedError("ReceiveFile is stopping")
            if select.select([self.connection], [], [], POLL)[0]:
                return self.rfile.read(limit)
            waited += POLL
            if waited >= IDLE_TIMEOUT:
                raise TimeoutError(f"nothing arrived for {IDLE_TIMEOUT} seconds")

    def refuse(self, length: int, status: int, text: str) -> None:
        """Replies after reading the body and throwing it away.

        A browser still sending the body when the reply arrives and the
        connection closes may report a dropped connection rather than the
        reply, so the body is taken in full first.
        """
        try:
            remaining = length
            while remaining:
                chunk = self.read_some(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # Stopping, or the sender has gone: the reply is still worth a try.
            pass
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
    # On Windows SO_REUSEADDR, which HTTPServer sets, lets a second program
    # bind a port already in use, so a second ReceiveFile would start cleanly
    # and never be sent anything. There the port is claimed exclusively
    # instead, which makes a second one fail to start, as it does elsewhere.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, address: tuple[str, int], directory: Path) -> None:
        super().__init__(address, Handler)
        self.directory = directory
        self.lock = threading.Lock()
        self.saved: Path | None = None
        self.stopping = False

    def abort(self) -> None:
        """Stops for good, keeping nothing that has not finished arriving.

        An upload in progress sees the server stopping at its next read and
        deletes what it had written, and this waits for that before
        returning, so the program cannot exit and leave part of a file
        behind. Uploads waiting their turn are refused.
        """
        self.stopping = True
        # However long that takes, as on a stalled network drive. The wait is
        # in short steps so that a second Ctrl+C can still end it, which
        # a single blocking acquire does not allow on every platform.
        while not self.lock.acquire(timeout=POLL):
            pass
        self.lock.release()


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


def safe_output() -> None:
    """Escapes what the console cannot show, rather than failing on it.

    Output redirected on Windows is written in the ANSI code page, which has
    no way to write most names outside western Europe, and printing one would
    otherwise fail after the file had been saved.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")


def main() -> int:
    safe_output()
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
        print("Ctrl+C stops it. An upload still arriving is not kept.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            try:
                server.abort()
            except KeyboardInterrupt:
                print("Stopped before an upload had finished clearing up, so part of "
                      "it may be left behind.")
                return 1
    # Ctrl+C can land just after a file was saved, in which case it was.
    if server.saved is None:
        print("Stopped. Nothing was saved.")
        return 1
    print(f"Saved {server.saved}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
