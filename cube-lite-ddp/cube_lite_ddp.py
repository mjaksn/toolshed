#!/usr/bin/env python3
"""Show LedFx output on a Yeelight Cube Lite.

Listens for DDP, the pixel protocol LedFx and WLED speak, and forwards each
frame to a Yeelight Cube Lite, a lamp with a matrix of 20 by 5 RGB pixels, over
the lamp's own LAN protocol. Standard library only.

    python cube_lite_ddp.py 192.168.1.40

The lamp's documented LAN protocol (TCP port 55443) only sets one colour for the
whole lamp, about once a second. Its firmware also has an undocumented UDP
command session on port 55444, worked out by the yeelight-cube-lite Home
Assistant integration, which takes a whole frame of pixels at a time and up to
30 datagrams a second. That is what this uses:

- ``udp_sess_new`` is answered by ``udp_sess_token`` with a token, which every
  later datagram carries, and which works only from the socket that asked;
- ``activate_fx_mode`` with ``{"mode": "direct"}`` puts the lamp in direct mode,
  for that session only;
- ``update_leds`` carries a frame as base64 of the R, G, B bytes of its 100
  pixels, row by row from the bottom left, and is never answered;
- a token the lamp no longer knows is refused with error code -1, and a new
  session has to be opened, and direct mode turned on again in it;
- the lamp can stop answering one client socket altogether, while it answers
  a new one at once, so a session is opened again from a fresh socket when the
  lamp goes quiet.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import logging
import select
import socket
import sys
import threading
import time

LOG = logging.getLogger("cube_lite_ddp")

COLUMNS = 20
ROWS = 5
PIXELS = COLUMNS * ROWS
FRAME_BYTES = PIXELS * 3

# DDP's own port, and the one LedFx's DDP device sends to unless told otherwise.
DDP_PORT = 4048

# The lamp's UDP command session.
LAMP_PORT = 55444
# The lamp answers every datagram up to 30 a second and loses about half of
# them at 40, so nothing is sent to it faster than this. LedFx sends 60 frames
# a second by default; the ones in between are dropped, never queued, so what
# the lamp shows is never behind.
MIN_SEND_INTERVAL = 1 / 30
# The lamp drops a datagram bigger than this. A frame is about 480 bytes.
MAX_DATAGRAM = 760
# One wait for an answer. The lamp usually answers in a few milliseconds, but
# Wi-Fi can hold one up for nearly 200. A command is sent up to ATTEMPTS times,
# which is harmless, as every command here sets something absolutely.
REPLY_TIMEOUT = 0.3
HANDSHAKE_TIMEOUT = 0.6
ATTEMPTS = 3
# Frames are never answered, so a session that has quietly died looks the same
# as one that works. After this long with no answer from the lamp, the session
# is checked with a keep-alive before the next frame.
CONFIRM_AFTER = 20.0
# The keep-alive's own setting, misspelt as the lamp spells it.
KEEP_ALIVE = ["keeplive_interval", 10]
# After this long without a frame, the next one opens a new session. Direct
# mode lasts only until the lamp is told to do something else, by its app or
# by Home Assistant, so a stream starting again after a pause turns it on
# afresh rather than trusting it is still on.
RESTART_AFTER = 10.0
# After the lamp fails to answer, frames are dropped for this long before it
# is tried again.
RETRY_AFTER = 5.0
# What the lamp answers a token it does not know with.
TOKEN_REFUSED = -1

# Where Yeelight lamps listen for discovery, and what they answer.
DISCOVERY_ADDRESS = ("239.255.255.250", 1982)
SEARCH = (
    b"M-SEARCH * HTTP/1.1\r\n"
    b"HOST: 239.255.255.250:1982\r\n"
    b'MAN: "ssdp:discover"\r\n'
    b"ST: wifi_bulb\r\n"
    b"\r\n"
)

# DDP header flags. The version is the top two bits of the first byte.
DDP_VERSION_MASK = 0xC0
DDP_VERSION_1 = 0x40
DDP_TIMECODE = 0x10
DDP_QUERY = 0x08
DDP_REPLY = 0x02
DDP_PUSH = 0x01
# Destination IDs that name something other than the pixels: 0 is reserved,
# 246 is control, 250 config, 251 status and 254 DMX. LedFx sends to 1.
DDP_NOT_DISPLAY = frozenset({0, 246, 250, 251, 254})


class LampError(Exception):
    """The lamp did not answer, or refused what it was sent."""


def parse_ddp(packet: bytes) -> tuple[int, bytes, bool] | None:
    """The byte offset, pixel data and PUSH flag of a DDP data packet for a
    display, or None for anything else: a query, a reply, another version, a
    packet for a non-display destination, or one cut short."""
    if len(packet) < 10:
        return None
    flags = packet[0]
    if flags & DDP_VERSION_MASK != DDP_VERSION_1:
        return None
    if flags & (DDP_QUERY | DDP_REPLY):
        return None
    if packet[3] in DDP_NOT_DISPLAY:
        return None
    header = 14 if flags & DDP_TIMECODE else 10
    offset = int.from_bytes(packet[4:8], "big")
    length = int.from_bytes(packet[8:10], "big")
    data = packet[header:header + length]
    if len(data) < length:
        return None
    return offset, data, bool(flags & DDP_PUSH)


def lamp_order(frame: bytes, origin: str) -> bytes:
    """A frame rearranged from input order into the lamp's, which starts at the
    bottom left. ``origin`` is where the input's first pixel is: ``top-left``,
    the order LedFx lays a matrix out in, or ``bottom-left``, the lamp's own."""
    if origin == "bottom-left":
        return frame
    row = COLUMNS * 3
    return b"".join(frame[r * row:(r + 1) * row] for r in reversed(range(ROWS)))


def pattern_frame() -> bytes:
    """The frame --test sends, in input order: rows red, green, blue, yellow and
    magenta from the first row on, with the first pixel of every row white."""
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255)]
    pixels = []
    for colour in colours:
        pixels.append((255, 255, 255))
        pixels.extend([colour] * (COLUMNS - 1))
    return bytes(channel for pixel in pixels for channel in pixel)


class Lamp:
    """One UDP command session with a Cube Lite, kept in direct mode."""

    def __init__(self, ip: str, port: int | None = None) -> None:
        self.ip = ip
        self._address = (ip, LAMP_PORT if port is None else port)
        self.sock = self._connect()
        self.token: str | None = None
        self.sessions = 0
        self._ids = itertools.count(1)
        # The first message ID of the current session. An error about an
        # earlier message belongs to a session already given up on.
        self._session_from = 0
        self._last_send = float("-inf")
        self._last_answer = float("-inf")
        self._last_frame = float("-inf")

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(self._address)
        except OSError:
            sock.close()
            raise
        sock.setblocking(False)
        return sock

    def reconnect(self) -> None:
        """Move to a new socket, and so a new source port, and forget the
        session. The lamp can stop answering one client socket for good while
        it answers a new one at once (seen within two minutes of a stream), so
        a lamp that has gone quiet is tried again from a fresh one."""
        try:
            sock = self._connect()
        except OSError as err:
            raise LampError(f"cannot reach the lamp at {self.ip}: {err}") from err
        self.sock.close()
        self.sock = sock
        self.token = None

    def close(self) -> None:
        self.sock.close()

    def next_send(self) -> float:
        """The earliest time.monotonic() at which the lamp may be sent anything."""
        return self._last_send + MIN_SEND_INTERVAL

    def _send(self, message: dict) -> None:
        data = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\r\n"
        if len(data) > MAX_DATAGRAM:
            raise LampError(
                f"{message['method']} would be {len(data)} bytes, more than the lamp takes"
            )
        wait = self.next_send() - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            self.sock.send(data)
        except OSError as err:
            raise LampError(f"cannot send to the lamp at {self.ip}: {err}") from err
        self._last_send = time.monotonic()

    def _messages(self) -> list[dict]:
        """Every message waiting from the lamp, without blocking."""
        found = []
        while True:
            try:
                data = self.sock.recv(4096)
            except OSError:
                # Nothing waiting, or, on Windows, the ICMP "port unreachable"
                # of an earlier datagram turning up here. Both mean no answer.
                return found
            for line in data.splitlines():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    self._last_answer = time.monotonic()
                    found.append(message)

    def _note(self, message: dict) -> None:
        """Act on a message nobody was waiting for. A refused token in the
        current session means the lamp has dropped it."""
        error = message.get("error")
        message_id = message.get("id")
        if (
            self.token is not None
            and isinstance(error, dict)
            and error.get("code") == TOKEN_REFUSED
            and isinstance(message_id, int)
            and message_id >= self._session_from
        ):
            LOG.info("The lamp dropped the session. The next frame opens a new one.")
            self.token = None

    def drain(self) -> None:
        """Read whatever the lamp sent unasked."""
        for message in self._messages():
            self._note(message)

    def _wait(self, match, timeout: float) -> dict | None:
        end = time.monotonic() + timeout
        while True:
            for message in self._messages():
                if match(message):
                    return message
                self._note(message)
            left = end - time.monotonic()
            if left <= 0:
                return None
            select.select([self.sock], [], [], left)

    def request(self, method: str, params: list) -> dict:
        """Send a command in the session and return the lamp's answer, a dict
        holding ``result`` or ``error``."""
        message = {"id": next(self._ids), "method": method, "params": params, "token": self.token}
        for _ in range(ATTEMPTS):
            self._send(message)
            answer = self._wait(lambda m: m.get("id") == message["id"], REPLY_TIMEOUT)
            if answer is not None:
                return answer
        raise LampError(f"the lamp at {self.ip} did not answer {method}")

    def open(self) -> None:
        """Open a new session and turn direct mode on in it."""
        self.token = None
        first = next(self._ids)
        message = {"id": first, "method": "udp_sess_new", "params": []}
        token = None
        for _ in range(ATTEMPTS):
            self._send(message)
            answer = self._wait(lambda m: m.get("method") == "udp_sess_token", HANDSHAKE_TIMEOUT)
            params = (answer or {}).get("params")
            if isinstance(params, dict) and isinstance(params.get("token"), str):
                token = params["token"]
                break
        if token is None:
            raise LampError(f"the lamp at {self.ip} did not open a session")
        self.token = token
        self._session_from = first
        answer = self.request("activate_fx_mode", [{"mode": "direct"}])
        if "error" in answer:
            self.token = None
            raise LampError(f"the lamp at {self.ip} refused direct mode: {answer['error']}")
        self.sessions += 1
        LOG.info("Opened a session with the lamp at %s, in direct mode", self.ip)

    def _prepare(self) -> None:
        """Make sure the next frame lands in a live session in direct mode."""
        now = time.monotonic()
        if self.token is None or now - self._last_frame > RESTART_AFTER:
            self.open()
            return
        if now - self._last_answer <= CONFIRM_AFTER:
            return
        if "error" in self.request("udp_sess_keep_alive", KEEP_ALIVE):
            LOG.info("The lamp dropped the session. Opening a new one.")
            self.open()

    def send_frame(self, frame: bytes) -> None:
        """Show a frame of FRAME_BYTES bytes, in the lamp's own pixel order."""
        self._prepare()
        data = base64.b64encode(frame).decode("ascii")
        self._send({"id": next(self._ids), "method": "update_leds", "params": [data], "token": self.token})
        self._last_frame = time.monotonic()


class Bridge:
    """Takes DDP frames on a UDP port and shows them on a lamp."""

    def __init__(self, lamp: Lamp, host: str = "0.0.0.0", port: int = DDP_PORT, origin: str = "top-left") -> None:
        self.lamp = lamp
        self.origin = origin
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((host, port))
        except OSError:
            self.sock.close()
            raise
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.frames_shown = 0
        # DDP can split a frame over several packets and says it is complete
        # with PUSH. Pixels a sender never fills stay black.
        self._buffer = bytearray(FRAME_BYTES)
        # The newest complete frame not yet sent to the lamp.
        self._pending: bytes | None = None
        self._retry_at = float("-inf")
        self._failing = False

    def close(self) -> None:
        self.sock.close()

    def receive(self, packet: bytes) -> None:
        parsed = parse_ddp(packet)
        if parsed is None:
            return
        offset, data, push = parsed
        if offset < FRAME_BYTES:
            chunk = data[:FRAME_BYTES - offset]
            self._buffer[offset:offset + len(chunk)] = chunk
        if push:
            self._pending = bytes(self._buffer)

    def _read_ddp(self) -> None:
        while True:
            try:
                packet = self.sock.recv(65535)
            except OSError:
                return
            self.receive(packet)

    def _forward(self) -> None:
        now = time.monotonic()
        if self._pending is None or now < self._retry_at or now < self.lamp.next_send():
            return
        frame, self._pending = self._pending, None
        try:
            self._show(lamp_order(frame, self.origin))
        except LampError as err:
            self.lamp.token = None
            self._retry_at = time.monotonic() + RETRY_AFTER
            if not self._failing:
                LOG.warning(
                    "%s. Frames are dropped until it answers, trying again every %g seconds.",
                    str(err).capitalize(), RETRY_AFTER,
                )
                self._failing = True
            return
        if self._failing:
            LOG.info("The lamp at %s is answering again", self.lamp.ip)
            self._failing = False
        self.frames_shown += 1

    def _show(self, frame: bytes) -> None:
        """Send a frame to the lamp. When the lamp does not answer, send it
        once more at once from a new socket, which is usually all it takes."""
        try:
            self.lamp.send_frame(frame)
        except LampError:
            self.lamp.reconnect()
            self.lamp.send_frame(frame)

    def step(self, wait: float = 0.25) -> None:
        """Wait up to ``wait`` seconds for something to do, then do it."""
        if self._pending is not None:
            due = max(self.lamp.next_send(), self._retry_at) - time.monotonic()
            wait = max(0.0, min(wait, due))
        readable, _, _ = select.select([self.sock, self.lamp.sock], [], [], wait)
        if self.sock in readable:
            self._read_ddp()
        if self.lamp.sock in readable:
            self.lamp.drain()
        self._forward()

    def run(self, stop: threading.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            self.step()


def discover(wait: float = 3.0) -> list[dict[str, str]]:
    """The Yeelight lamps that answer a search on this network, each as the
    headers of its answer, with names in lower case, plus its ``ip``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        found: dict[str, dict[str, str]] = {}
        start = time.monotonic()
        end = start + wait
        searches = 0
        while True:
            now = time.monotonic()
            if now >= end:
                break
            # Three searches half a second apart, as UDP may lose one.
            if searches < 3 and now >= start + searches * 0.5:
                sock.sendto(SEARCH, DISCOVERY_ADDRESS)
                searches += 1
            readable, _, _ = select.select([sock], [], [], min(0.25, end - now))
            if not readable:
                continue
            data, (ip, _) = sock.recvfrom(4096)
            headers = {"ip": ip}
            for line in data.decode("utf-8", "replace").splitlines()[1:]:
                name, sep, value = line.partition(":")
                if sep:
                    headers[name.strip().lower()] = value.strip()
            found.setdefault(headers.get("id", ip), headers)
        return list(found.values())
    finally:
        sock.close()


def run_discover() -> int:
    lamps = discover()
    if not lamps:
        print(
            "No Yeelight lamp answered. Check that LAN Control is on for the lamp in "
            "the Yeelight app, and that this machine is on the same network."
        )
        return 1
    for lamp in lamps:
        note = "" if lamp.get("model") == "CubeLite" else "  (not a Cube Lite)"
        print(
            f"{lamp['ip']}  model {lamp.get('model', '?')}  name {lamp.get('name', '?')}"
            f"  id {lamp.get('id', '?')}{note}"
        )
    return 0


def open_lamp(ip: str) -> Lamp | None:
    try:
        return Lamp(ip)
    except OSError as err:
        print(f"Cannot use {ip} as the lamp's address: {err}", file=sys.stderr)
        return None


def run_test(ip: str, origin: str) -> int:
    lamp = open_lamp(ip)
    if lamp is None:
        return 1
    try:
        lamp.send_frame(lamp_order(pattern_frame(), origin))
    except LampError as err:
        print(f"{str(err).capitalize()}.", file=sys.stderr)
        return 1
    finally:
        lamp.close()
    first, last = ("top", "bottom") if origin == "top-left" else ("bottom", "top")
    print(
        f"Sent the test pattern. The {first} row should be red and the {last} row "
        "magenta, with a white column at the left edge."
    )
    return 0


def run_bridge(ip: str, host: str, port: int, origin: str) -> int:
    lamp = open_lamp(ip)
    if lamp is None:
        return 1
    try:
        bridge = Bridge(lamp, host, port, origin)
    except OSError as err:
        lamp.close()
        print(f"Cannot listen on {host}:{port}: {err}", file=sys.stderr)
        return 1
    where = "every interface" if host in ("", "0.0.0.0") else host
    LOG.info(
        "Listening for DDP on %s, port %d, and showing it on the Cube Lite at %s. Ctrl+C stops it.",
        where, bridge.port, ip,
    )
    try:
        bridge.run()
    except KeyboardInterrupt:
        LOG.info("Stopped after showing %d frames.", bridge.frames_shown)
    finally:
        bridge.close()
        lamp.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Show DDP output, such as LedFx's, on a Yeelight Cube Lite. In LedFx, add a "
            f"DDP device with {PIXELS} pixels at this machine's address, and give its "
            f"virtual {ROWS} rows."
        ),
    )
    parser.add_argument("lamp", nargs="?", help="the lamp's IP address, which --discover finds")
    parser.add_argument(
        "--discover", action="store_true",
        help="list the Yeelight lamps that answer on this network, and exit",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="send the lamp a test pattern, to check which way up frames arrive, and exit",
    )
    parser.add_argument(
        "--listen", default="0.0.0.0", metavar="ADDRESS",
        help="the address to listen for DDP on (default: every interface)",
    )
    parser.add_argument(
        "--port", type=int, default=DDP_PORT,
        help="the UDP port to listen for DDP on (default: %(default)s, DDP's own)",
    )
    parser.add_argument(
        "--origin", choices=("top-left", "bottom-left"), default="top-left",
        help="where the first pixel of a frame belongs (default: %(default)s, LedFx's matrix order)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.discover:
        return run_discover()
    if not args.lamp:
        parser.error("give the lamp's IP address, or use --discover to find it")
    if args.test:
        return run_test(args.lamp, args.origin)
    return run_bridge(args.lamp, args.listen, args.port, args.origin)


if __name__ == "__main__":
    sys.exit(main())
