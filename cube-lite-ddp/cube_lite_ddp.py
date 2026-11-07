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
  pixels, row by row from the bottom left, and is never answered in direct
  mode. Once something else has given the lamp an effect of its own, each
  frame is answered with error code 6, "illegal request", and asking for
  direct mode again in the same session puts it back;
- a session ends about four keep-alive intervals after its last
  ``udp_sess_keep_alive``, frames not counting: about 40 seconds with the
  interval of 10 that this sets, about two minutes with none set. After that
  every datagram in it, frames included, is refused with error code -1, so a
  new session has to be opened, and direct mode turned on again in it;
- the lamp can hang: its display stops, it answers handshakes and keep-alives
  but no other command, its sessions end after about a minute however they
  are kept alive, and nothing but a power cycle brings it back. In testing it
  hung on a weak power supply, under heavy 2.4 GHz interference while taking
  30 frames a second, which 15 a second rode out, and once when a second
  session was opened and direct mode turned on in it while the first was
  streaming. A second session beside an idle one did not hang it, but that
  one hang may have come from the interference rather than the second
  session, so a session is never given up because the lamp is quiet, which
  over Wi-Fi it can be for seconds at a time, only when the lamp refuses it,
  or when it has certainly expired. It has also hung at 15 frames a second
  after anything from minutes to hours, and once hung, it dropped off the
  network for about half a minute at a time while frames went on arriving;
  ``--stats`` records what leads up to a hang.
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
# the lamp shows is never behind. --max-fps can send it fewer.
MAX_FPS = 30
MIN_SEND_INTERVAL = 1 / MAX_FPS
# The lamp drops a datagram bigger than this. A frame is about 480 bytes.
MAX_DATAGRAM = 760
# How long to wait for the answer to the commands that set a session up.
# Over Wi-Fi the lamp usually answers in a few milliseconds, but its answers
# can be held up for seconds when the link stalls, so the waits are long. A
# command is sent up to ATTEMPTS times; that is harmless, as each one sets
# something absolutely, except udp_sess_new, which opens a session each time
# and so is sent again only after a long wait.
REPLY_TIMEOUT = 1.0
HANDSHAKE_TIMEOUT = 3.0
ATTEMPTS = 3
HANDSHAKE_ATTEMPTS = 2
# While frames flow, a keep-alive goes into the session this often, without
# waiting for its answer: it is what keeps the session alive, as frames do
# not. Its answer, read whenever it comes, also shows the lamp is there. Once
# every interval it announces, so three in a row can be lost to a Wi-Fi stall
# before the session ends; at once every 30 seconds, losing one ended it.
KEEP_ALIVE_EVERY = 10.0
# The keep-alive's own setting, the interval in seconds, misspelt as the lamp
# spells it.
KEEP_ALIVE = ["keeplive_interval", 10]
# After this long with frames going out and nothing at all coming back, say
# so. The session is kept all the same: see the module docstring.
SILENT_WARN_AFTER = 90.0
# After this long without a frame, the session has certainly expired, and the
# next frame opens a new one, which also turns direct mode on afresh in case
# the lamp's app or Home Assistant gave it something else to show meanwhile.
RESTART_AFTER = 150.0
# After the lamp fails to open a session, frames are dropped for this long
# before it is tried again.
RETRY_AFTER = 5.0
# What the lamp answers a token it does not know with.
TOKEN_REFUSED = -1
# --hold takes no longer than this, in seconds.
MAX_HOLD = 600.0
# What it answers a frame with when its session is no longer in direct mode,
# as when something else has given the lamp an effect of its own to show.
ILLEGAL_REQUEST = 6
# A keep-alive still unanswered after this long is taken as lost, and no
# longer waited for.
ANSWER_GIVEN_UP_AFTER = 60.0

# The lamp's documented LAN protocol, whose port --stats tries. A hung lamp
# has refused or dropped connections here, so it tells a hang from a quiet link.
TCP_PORT = 55443
PROBE_TIMEOUT = 3.0
# --stats takes no shorter interval than this, to keep the probes gentle.
MIN_STATS_EVERY = 5.0

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
DDP_QUERY = 0x02
DDP_REPLY = 0x04
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

    def __init__(self, ip: str, port: int | None = None, hold: float = 0.0) -> None:
        self.ip = ip
        # After the lamp ends a session, which it does once it has restarted,
        # it is sent nothing for this many seconds, until hold_until.
        self.hold = hold
        self.hold_until = float("-inf")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.connect((ip, LAMP_PORT if port is None else port))
        except OSError:
            self.sock.close()
            raise
        self.sock.setblocking(False)
        self.token: str | None = None
        self.sessions = 0
        self._ids = itertools.count(1)
        # The first message ID of the current session. An error about an
        # earlier message belongs to a session already given up on.
        self._session_from = 0
        self._last_send = float("-inf")
        self._last_answer = float("-inf")
        self._last_frame = float("-inf")
        self._last_keep_alive = float("-inf")
        self._opened_at = float("-inf")
        self._silent = False
        # Set when the lamp refuses a frame because its session has left direct
        # mode; direct mode is then asked for again, in the same session, no
        # sooner than _reactivate_at.
        self._left_direct = False
        self._reactivate_at = float("-inf")
        # The message ID direct mode was last asked for with.
        self._direct_from = 0
        # For the status line: the last message ID sent, the keep-alives still
        # waiting for an answer by message ID, and counts since it was last
        # taken.
        self.last_id = 0
        self._waiting: dict[int, float] = {}
        # The method and send time of each recent message that was not a
        # frame, by message ID, to say what an error answered. Any ID missing
        # from it was a frame.
        self._sent: dict[int, tuple[str, float]] = {}
        # The errors the bridge does not act on that have been logged in this
        # session; each is logged once a session, and counted every time.
        self._reported: set[str] = set()
        self._reset_counts()

    def _reset_counts(self) -> None:
        self.keep_alives_sent = 0
        self.keep_alives_answered = 0
        self.slowest_answer: float | None = None
        self.errors = 0

    def close(self) -> None:
        self.sock.close()

    def counts(self) -> dict:
        """What the status line reports about the lamp, counted since this was
        last called, which starts the counts afresh."""
        now = time.monotonic()
        self._waiting = {i: t for i, t in self._waiting.items() if now - t < ANSWER_GIVEN_UP_AFTER}
        self._sent = {i: s for i, s in self._sent.items() if now - s[1] < ANSWER_GIVEN_UP_AFTER}
        counts = {
            "session": self.sessions if self.token is not None else None,
            "age": now - self._opened_at if self.token is not None else None,
            "last_id": self.last_id,
            "keep_alives_sent": self.keep_alives_sent,
            "keep_alives_answered": self.keep_alives_answered,
            "slowest_answer": self.slowest_answer,
            "errors": self.errors,
            "quiet": now - self._last_answer if self._last_answer > float("-inf") else None,
        }
        self._reset_counts()
        return counts

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
        self.last_id = message["id"]
        if message["method"] != "update_leds":
            self._sent[message["id"]] = (message["method"], self._last_send)

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
                    self._heard()
                    self._count(message)
                    found.append(message)

    def _count(self, message: dict) -> None:
        message_id = message.get("id")
        sent = self._waiting.pop(message_id, None) if isinstance(message_id, int) else None
        if sent is not None:
            self.keep_alives_answered += 1
            delay = time.monotonic() - sent
            self.slowest_answer = delay if self.slowest_answer is None else max(self.slowest_answer, delay)
        if "error" in message:
            self.errors += 1

    def _heard(self) -> None:
        self._last_answer = time.monotonic()
        if self._silent:
            LOG.info("The lamp at %s is answering again", self.ip)
            self._silent = False

    def _note(self, message: dict) -> None:
        """Act on a message nobody was waiting for. A refused token in the
        current session means the lamp has ended it; a frame refused as an
        illegal request, that the session has left direct mode. Any other
        error is logged the first time it comes in a session."""
        error = message.get("error")
        message_id = message.get("id")
        if (
            self.token is None
            or not isinstance(error, dict)
            or not isinstance(message_id, int)
            or message_id < self._session_from
        ):
            return
        if error.get("code") == TOKEN_REFUSED:
            self.token = None
            if self.hold:
                self.hold_until = time.monotonic() + self.hold
                LOG.info(
                    "The lamp ended the session, as it does after restarting. Sending it nothing "
                    "for %g seconds, then opening a new one.", self.hold,
                )
            else:
                LOG.info("The lamp ended the session. The next frame opens a new one.")
        elif (
            error.get("code") == ILLEGAL_REQUEST
            and not self._left_direct
            # Not a refusal of a frame sent before direct mode was last asked for.
            and message_id > self._direct_from
        ):
            LOG.warning(
                "The lamp is refusing frames as illegal requests: something has taken it out "
                "of direct mode, such as its app, Home Assistant or a Matter controller. "
                "Turning direct mode on again in the same session."
            )
            self._left_direct = True
        elif error.get("code") not in (TOKEN_REFUSED, ILLEGAL_REQUEST):
            key = json.dumps(error, sort_keys=True)
            if key in self._reported:
                return
            self._reported.add(key)
            method = self._sent.get(message_id, ("update_leds", 0.0))[0]
            LOG.warning(
                "The lamp answered message %d, %s, with an error the bridge does not act on: %s. "
                "Any more like it in this session are counted in the status line, not logged.",
                message_id, method, json.dumps(message)[:300],
            )

    def drain(self) -> None:
        """Read whatever the lamp sent unasked, keep-alive answers among it."""
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

    def request(self, method: str, params: list) -> dict | None:
        """Send a command in the session and return the lamp's answer, a dict
        holding ``result`` or ``error``, or None when none came."""
        message = {"id": next(self._ids), "method": method, "params": params, "token": self.token}
        for _ in range(ATTEMPTS):
            self._send(message)
            answer = self._wait(lambda m: m.get("id") == message["id"], REPLY_TIMEOUT)
            if answer is not None:
                return answer
        return None

    def open(self) -> None:
        """Open a new session and turn direct mode on in it. Only for when no
        session is alive: the lamp has refused the last, or it has expired."""
        self.token = None
        first = next(self._ids)
        message = {"id": first, "method": "udp_sess_new", "params": []}
        token = None
        for _ in range(HANDSHAKE_ATTEMPTS):
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
        self._left_direct = False
        self._reported.clear()
        self._opened_at = time.monotonic()
        self._last_keep_alive = self._opened_at
        self.sessions += 1
        answer = self.request("activate_fx_mode", [{"mode": "direct"}])
        self._direct_from = self.last_id
        if answer is None:
            # Most likely the answer was lost. Opening another session to ask
            # again is what can freeze the lamp, so this one is kept: its
            # frames either show, or are refused, which ends it cleanly.
            LOG.warning(
                "Opened a session with the lamp at %s, but it did not confirm direct "
                "mode. Sending frames to it anyway. If the panel stays frozen, "
                "power-cycle the lamp.", self.ip,
            )
            return
        if "error" in answer:
            self.token = None
            raise LampError(f"the lamp at {self.ip} refused direct mode: {answer['error']}")
        LOG.info("Opened a session with the lamp at %s, in direct mode", self.ip)

    def _reactivate(self) -> None:
        """Ask for direct mode again in the session the lamp has taken out of
        it. A new session is not opened for this: see the module docstring."""
        answer = self.request("activate_fx_mode", [{"mode": "direct"}])
        self._direct_from = self.last_id
        if answer is not None and "error" not in answer:
            LOG.info("Direct mode is on again.")
            self._left_direct = False
            return
        # Refused, or unanswered; a refused token has already ended the
        # session, and the next frame opens a new one in direct mode.
        if self.token is None:
            self._left_direct = False
            return
        self._reactivate_at = time.monotonic() + RETRY_AFTER

    def _prepare(self) -> None:
        """Make sure the next frame goes to a session that is alive, and keep
        it alive."""
        now = time.monotonic()
        # Idle since the last frame, or since the session opened when no frame
        # has gone into it yet: a first frame that failed to send must not
        # make the retry open a second session beside this one.
        idle = now - max(self._last_frame, self._opened_at)
        if self.token is None or idle > RESTART_AFTER:
            self.open()
            return
        if self._left_direct and now >= self._reactivate_at:
            self._reactivate()
            if self.token is None:
                return
        if now - self._last_keep_alive >= KEEP_ALIVE_EVERY:
            message_id = next(self._ids)
            self._send({
                "id": message_id, "method": "udp_sess_keep_alive",
                "params": KEEP_ALIVE, "token": self.token,
            })
            self._last_keep_alive = time.monotonic()
            self._waiting[message_id] = self._last_keep_alive
            self.keep_alives_sent += 1
        quiet = now - max(self._last_answer, self._opened_at)
        if quiet > SILENT_WARN_AFTER and not self._silent:
            LOG.warning(
                "The lamp at %s has not answered for %d seconds. Frames still go to "
                "its session. If the panel is frozen, power-cycle the lamp.",
                self.ip, quiet,
            )
            self._silent = True

    def send_frame(self, frame: bytes) -> None:
        """Show a frame of FRAME_BYTES bytes, in the lamp's own pixel order."""
        self._prepare()
        data = base64.b64encode(frame).decode("ascii")
        self._send({"id": next(self._ids), "method": "update_leds", "params": [data], "token": self.token})
        self._last_frame = time.monotonic()


class Bridge:
    """Takes DDP frames on a UDP port and shows them on a lamp."""

    def __init__(
        self, lamp: Lamp, host: str = "0.0.0.0", port: int = DDP_PORT, origin: str = "top-left",
        stats_every: float = 0.0, probe: TcpProbe | None = None, max_fps: int = MAX_FPS,
    ) -> None:
        self.lamp = lamp
        self.origin = origin
        # Frames go to the lamp no more often than this, on a steady beat;
        # the lamp's own limit, which keep-alives share, applies as well.
        self.frame_interval = 1 / max_fps
        self._next_frame = float("-inf")
        # A status line every stats_every seconds while anything flows; none
        # when it is 0. The probe's latest answer goes on the end of it.
        self.stats_every = stats_every
        self.probe = probe
        self._stats_due = time.monotonic() + stats_every
        self._last_packet: float | None = None
        self._reset_counts()
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

    def _reset_counts(self) -> None:
        self.frames_in = 0
        self.frames_dropped = 0
        self.frames_sent = 0
        self.longest_gap: float | None = None
        self._level_total = 0

    def receive(self, packet: bytes) -> None:
        parsed = parse_ddp(packet)
        if parsed is None:
            return
        now = time.monotonic()
        if self._last_packet is not None:
            gap = now - self._last_packet
            self.longest_gap = gap if self.longest_gap is None else max(self.longest_gap, gap)
        self._last_packet = now
        offset, data, push = parsed
        if offset < FRAME_BYTES:
            chunk = data[:FRAME_BYTES - offset]
            self._buffer[offset:offset + len(chunk)] = chunk
        if push:
            self.frames_in += 1
            if self._pending is not None:
                self.frames_dropped += 1
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
        if self._pending is None or now < max(
            self._retry_at, self._next_frame, self.lamp.next_send(), self.lamp.hold_until,
        ):
            return
        frame, self._pending = self._pending, None
        try:
            self.lamp.send_frame(lamp_order(frame, self.origin))
        except LampError as err:
            # The session, if there is one, is kept: see the module docstring.
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
        # Keep to the beat while frames keep coming, so that a frame sent a
        # little late does not slow the rate; after a pause, start it afresh.
        if now - self._next_frame < self.frame_interval:
            self._next_frame += self.frame_interval
        else:
            self._next_frame = now + self.frame_interval
        self.frames_shown += 1
        self.frames_sent += 1
        self._level_total += sum(frame)

    def status_line(self) -> str | None:
        """The status line for the time since the last one, or None when
        nothing came or went in it. Starts the counts afresh either way."""
        lamp = self.lamp.counts()
        bridge = {
            "frames_in": self.frames_in, "frames_dropped": self.frames_dropped,
            "frames_sent": self.frames_sent, "longest_gap": self.longest_gap,
            "level": self._level_total / (self.frames_sent * FRAME_BYTES * 255) if self.frames_sent else None,
        }
        self._reset_counts()
        if not (bridge["frames_in"] or bridge["frames_sent"] or lamp["keep_alives_sent"]):
            return None

        def seconds(value: float | None) -> str:
            return "none" if value is None else f"{value:.3f} s"

        parts = [
            (
                f"from LedFx {bridge['frames_in']} frames, longest gap {seconds(bridge['longest_gap'])}, "
                f"{bridge['frames_dropped']} dropped"
            ),
            f"to the lamp {bridge['frames_sent']} frames"
            + ("" if bridge["level"] is None else f" at {bridge['level']:.0%} level"),
            "no session" if lamp["session"] is None
            else f"session {lamp['session']}, {lamp['age']:.0f} s old",
            f"message ID {lamp['last_id']}",
            (
                f"keep-alives {lamp['keep_alives_sent']} sent, {lamp['keep_alives_answered']} answered, "
                f"slowest {seconds(lamp['slowest_answer'])}"
            ),
            "never heard from" if lamp["quiet"] is None else f"last heard {lamp['quiet']:.1f} s ago",
            f"{lamp['errors']} errors",
        ]
        if self.probe is not None:
            parts.append(f"TCP {self.probe.port} {self.probe.latest}")
        return "Status: " + "; ".join(parts)

    def _report(self) -> None:
        self._stats_due = time.monotonic() + self.stats_every
        line = self.status_line()
        if line is not None:
            LOG.info(line)

    def step(self, wait: float = 0.25) -> None:
        """Wait up to ``wait`` seconds for something to do, then do it."""
        if self._pending is not None:
            due = max(
                self.lamp.next_send(), self._retry_at, self._next_frame, self.lamp.hold_until,
            ) - time.monotonic()
            wait = max(0.0, min(wait, due))
        readable, _, _ = select.select([self.sock, self.lamp.sock], [], [], wait)
        if self.sock in readable:
            self._read_ddp()
        if self.lamp.sock in readable:
            self.lamp.drain()
        self._forward()
        if self.stats_every and time.monotonic() >= self._stats_due:
            self._report()

    def run(self, stop: threading.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            self.step()


def probe_tcp(ip: str, port: int = TCP_PORT, timeout: float = PROBE_TIMEOUT) -> tuple[str, str]:
    """Open and close a connection to the lamp's documented TCP port, sending
    nothing. Returns how it went, ``open``, ``refused`` or ``unreachable``, and
    a description for the log; ``open`` gives the time the connection took,
    which stands in for a ping. The lamp takes commands there, but a Cube Lite
    in direct mode leaves even get_prop unanswered, so there is nothing to ask."""
    start = time.monotonic()
    try:
        sock = socket.create_connection((ip, port), timeout)
    except ConnectionRefusedError:
        return "refused", "refused the connection"
    except OSError as err:
        return "unreachable", f"took no connection ({err})"
    sock.close()
    return "open", f"open ({(time.monotonic() - start) * 1000:.0f} ms)"


class TcpProbe(threading.Thread):
    """Tries the lamp's TCP port every so often, away from the bridge's own
    loop, logging each change in how it went."""

    def __init__(self, ip: str, every: float, port: int = TCP_PORT, timeout: float = PROBE_TIMEOUT) -> None:
        super().__init__(daemon=True)
        self.ip = ip
        self.every = every
        self.port = port
        self.timeout = timeout
        self.latest = "not asked yet"
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        last_kind = None
        while not self._halt.is_set():
            kind, text = probe_tcp(self.ip, self.port, self.timeout)
            self.latest = text
            if kind != last_kind:
                level = logging.INFO if kind == "open" else logging.WARNING
                LOG.log(level, "The lamp's TCP port %d %s", self.port, text)
                last_kind = kind
            self._halt.wait(self.every)


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


def open_lamp(ip: str, hold: float = 0.0) -> Lamp | None:
    try:
        return Lamp(ip, hold=hold)
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


def run_bridge(
    ip: str, host: str, port: int, origin: str, stats_every: float = 0.0, max_fps: int = MAX_FPS,
    hold: float = 0.0,
) -> int:
    lamp = open_lamp(ip, hold)
    if lamp is None:
        return 1
    probe = TcpProbe(ip, stats_every) if stats_every else None
    try:
        bridge = Bridge(lamp, host, port, origin, stats_every, probe, max_fps)
    except OSError as err:
        lamp.close()
        print(f"Cannot listen on {host}:{port}: {err}", file=sys.stderr)
        return 1
    where = "every interface" if host in ("", "0.0.0.0") else host
    LOG.info(
        "Listening for DDP on %s, port %d, and showing it on the Cube Lite at %s. Ctrl+C stops it.",
        where, bridge.port, ip,
    )
    if max_fps < MAX_FPS:
        LOG.info("Sending the lamp at most %d frames a second.", max_fps)
    if probe is not None:
        probe.start()
    try:
        bridge.run()
    except KeyboardInterrupt:
        LOG.info("Stopped after showing %d frames.", bridge.frames_shown)
    finally:
        if probe is not None:
            probe.stop()
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
    parser.add_argument(
        "--stats", type=float, default=0.0, metavar="SECONDS",
        help=(
            "log a status line every SECONDS while frames flow, and try the lamp's TCP port "
            f"{TCP_PORT} as often, for tracking down a hang (default: off; "
            f"at least {MIN_STATS_EVERY:g})"
        ),
    )
    parser.add_argument(
        "--max-fps", type=int, default=MAX_FPS, metavar="N",
        help=(
            f"send the lamp at most N frames a second, from 1 to {MAX_FPS} "
            "(default: %(default)s, as many as it takes)"
        ),
    )
    parser.add_argument(
        "--hold", type=float, default=0.0, metavar="SECONDS",
        help=(
            "after the lamp ends a session, as it does once it has restarted, send it nothing "
            "for SECONDS before opening a new one, to let it finish starting up "
            f"(default: 0, open one at once; at most {MAX_HOLD:g})"
        ),
    )
    args = parser.parse_args(argv)
    if args.stats and not args.stats >= MIN_STATS_EVERY:
        parser.error(f"--stats takes at least {MIN_STATS_EVERY:g} seconds, or 0 for none")
    if not 1 <= args.max_fps <= MAX_FPS:
        parser.error(f"--max-fps takes from 1 to {MAX_FPS}")
    if not 0 <= args.hold <= MAX_HOLD:
        parser.error(f"--hold takes from 0 to {MAX_HOLD:g} seconds")
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.discover:
        return run_discover()
    if not args.lamp:
        parser.error("give the lamp's IP address, or use --discover to find it")
    if args.test:
        return run_test(args.lamp, args.origin)
    return run_bridge(args.lamp, args.listen, args.port, args.origin, args.stats, args.max_fps, args.hold)


if __name__ == "__main__":
    sys.exit(main())
