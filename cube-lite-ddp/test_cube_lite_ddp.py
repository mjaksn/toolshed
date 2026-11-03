"""Tests for cube_lite_ddp, against a stand-in lamp on the loopback address.

The stand-in keeps to what the real lamp does: a session token from
udp_sess_new, direct mode per session, error -1 for a token it does not know,
and no answer at all to update_leds. Run with ``python -m unittest``.
"""

from __future__ import annotations

import base64
import itertools
import json
import logging
import socket
import threading
import time
import unittest
import uuid
from unittest import mock

import cube_lite_ddp as cube

LOOPBACK = "127.0.0.1"


class FakeLamp(threading.Thread):
    """A Cube Lite's UDP command session, on a port the system picks."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((LOOPBACK, 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        self.lock = threading.Lock()
        self.tokens: list[str] = []
        self.direct: set[str] = set()
        self.methods: list[str] = []
        # Each frame shown: (time.monotonic(), token, whether direct mode was
        # on in its session, the decoded pixel bytes).
        self.frames: list[tuple[float, str, bool, bytes]] = []
        self.silent = False
        # Methods the lamp acts on but never answers, as when an answer is
        # lost on the way.
        self.unanswered: set[str] = set()
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()
        self.join()
        self.sock.close()

    def forget_tokens(self) -> None:
        """As if the lamp had restarted, or let every session expire."""
        with self.lock:
            self.tokens.clear()
            self.direct.clear()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                data, peer = self.sock.recvfrom(4096)
            except OSError:
                continue
            for line in data.splitlines():
                message = json.loads(line)
                with self.lock:
                    if self.silent:
                        continue
                    reply = self._handle(message)
                    if message.get("method") in self.unanswered:
                        reply = None
                if reply is not None:
                    self.sock.sendto(json.dumps(reply).encode() + b"\r\n", peer)

    def _handle(self, message: dict) -> dict | None:
        method = message["method"]
        self.methods.append(method)
        if method == "udp_sess_new":
            token = uuid.uuid4().hex
            self.tokens.append(token)
            return {"id": message["id"], "method": "udp_sess_token", "params": {"token": token}}
        token = message.get("token")
        if token not in self.tokens:
            return {"id": message["id"], "error": {"code": -1, "message": "invalid parameter"}}
        if method == "activate_fx_mode":
            if message["params"] == [{"mode": "direct"}]:
                self.direct.add(token)
            return {"id": message["id"], "result": ["ok"], "token": token}
        if method == "udp_sess_keep_alive":
            return {"id": message["id"], "result": ["ok"], "token": token}
        if method == "update_leds":
            pixels = base64.b64decode(message["params"][0])
            self.frames.append((time.monotonic(), token, token in self.direct, pixels))
            return None
        return {"id": message["id"], "error": {"code": -1, "message": "unsupported method"}}

    def frame_count(self) -> int:
        with self.lock:
            return len(self.frames)


def ddp(data: bytes, offset: int = 0, push: bool = True, flags: int = 0x40, dest: int = 1) -> bytes:
    """A DDP data packet as LedFx sends one."""
    flags |= cube.DDP_PUSH if push else 0
    header = bytes([flags, 1, 0x01, dest]) + offset.to_bytes(4, "big") + len(data).to_bytes(2, "big")
    if flags & cube.DDP_TIMECODE:
        header += b"\x00\x00\x00\x00"
    return header + data


def rows_frame(colours: list[tuple[int, int, int]]) -> bytes:
    """A frame of solid rows, in the order given."""
    return b"".join(bytes(colour) * cube.COLUMNS for colour in colours)


ROW_COLOURS = [(10, 0, 0), (20, 0, 0), (30, 0, 0), (40, 0, 0), (50, 0, 0)]


def wait_for(condition, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class BridgeTest(unittest.TestCase):
    """A bridge running in a thread, between a DDP sender and a stand-in lamp."""

    origin = "top-left"

    def setUp(self) -> None:
        logging.getLogger("cube_lite_ddp").setLevel(logging.CRITICAL)
        self.fake = FakeLamp()
        self.fake.start()
        self.lamp = cube.Lamp(LOOPBACK, self.fake.port)
        self.bridge = cube.Bridge(self.lamp, LOOPBACK, 0, self.origin)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.bridge.run, args=(self.stopping,), daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.stopping.set()
        self.thread.join()
        self.bridge.close()
        self.lamp.close()
        self.sender.close()
        self.fake.stop()

    def send(self, packet: bytes) -> None:
        self.sender.sendto(packet, (LOOPBACK, self.bridge.port))

    def shown(self, count: int = 1) -> list[tuple[float, str, bool, bytes]]:
        self.assertTrue(wait_for(lambda: self.fake.frame_count() >= count), "no frame reached the lamp")
        with self.fake.lock:
            return list(self.fake.frames)


class ForwardingTest(BridgeTest):
    def test_frame_is_shown_upside_down_from_ledfx_order(self):
        self.send(ddp(rows_frame(ROW_COLOURS)))
        _, _, direct, pixels = self.shown()[0]
        self.assertTrue(direct, "frame sent before direct mode was on")
        self.assertEqual(pixels, rows_frame(list(reversed(ROW_COLOURS))))
        self.assertEqual(self.fake.methods[:3], ["udp_sess_new", "activate_fx_mode", "update_leds"])

    def test_frame_split_over_packets_waits_for_push(self):
        frame = rows_frame(ROW_COLOURS)
        self.send(ddp(frame[:120], push=False))
        time.sleep(0.2)
        self.assertEqual(self.fake.frame_count(), 0)
        self.send(ddp(frame[120:], offset=120))
        self.assertEqual(self.shown()[0][3], rows_frame(list(reversed(ROW_COLOURS))))

    def test_timecode_header_is_skipped(self):
        self.send(ddp(rows_frame(ROW_COLOURS), flags=0x40 | cube.DDP_TIMECODE))
        self.assertEqual(self.shown()[0][3], rows_frame(list(reversed(ROW_COLOURS))))

    def test_short_frame_leaves_the_rest_black_and_long_one_is_cut(self):
        self.send(ddp(b"\xff" * 30))
        pixels = self.shown(1)[0][3]
        # The first ten pixels LedFx sends are its top row's first ten, which
        # are the lamp's top row, its last 60 bytes.
        self.assertEqual(pixels[-60:], b"\xff" * 30 + b"\x00" * 30)
        self.assertEqual(pixels[:-60], b"\x00" * (cube.FRAME_BYTES - 60))
        time.sleep(cube.MIN_SEND_INTERVAL)
        self.send(ddp(b"\x07" * (cube.FRAME_BYTES + 90)))
        self.assertEqual(self.shown(2)[1][3], b"\x07" * cube.FRAME_BYTES)

    def test_packets_that_are_not_pixels_are_ignored(self):
        frame = rows_frame([(9, 9, 9)] * cube.ROWS)
        self.send(ddp(frame, flags=0x40 | cube.DDP_QUERY))
        self.send(ddp(frame, flags=0x80))  # another DDP version
        self.send(ddp(frame, dest=251))  # status
        self.send(b"\x41\x01")  # too short for a header
        self.send(ddp(frame)[:100])  # cut short
        time.sleep(0.2)
        self.assertEqual(self.fake.frame_count(), 0)
        self.send(ddp(rows_frame(ROW_COLOURS)))
        self.assertEqual(self.shown()[0][3], rows_frame(list(reversed(ROW_COLOURS))))

    def test_rate_is_capped_and_newest_frame_wins(self):
        frames = [rows_frame([(n, n, n)] * cube.ROWS) for n in range(60)]
        for frame in frames:
            self.send(ddp(frame))
        self.assertTrue(wait_for(lambda: self.fake.frames and self.fake.frames[-1][3] == frames[-1]))
        time.sleep(0.2)
        shown = self.shown()
        self.assertLess(len(shown), len(frames))
        self.assertEqual(shown[-1][3], frames[-1])
        gaps = [b[0] - a[0] for a, b in itertools.pairwise(shown)]
        # Loopback delivery adds jitter at the receiving end, so a little slack.
        self.assertTrue(all(gap > cube.MIN_SEND_INTERVAL * 0.7 for gap in gaps), gaps)

    def test_one_session_carries_a_stream(self):
        # Frames a tenth of a second apart. Two may still share a send slot on
        # a coarse timer, and the older is then rightly dropped, so this asks
        # only that several arrive, the newest among them, in one session.
        frames = [rows_frame([(n, 0, 0)] * cube.ROWS) for n in range(5)]
        for frame in frames:
            self.send(ddp(frame))
            time.sleep(0.1)
        self.assertTrue(wait_for(lambda: self.fake.frames and self.fake.frames[-1][3] == frames[-1]))
        shown = self.shown()
        self.assertGreaterEqual(len(shown), 3)
        self.assertEqual(len({token for _, token, _, _ in shown}), 1)
        self.assertEqual(self.fake.methods.count("udp_sess_new"), 1)


class BottomLeftTest(BridgeTest):
    origin = "bottom-left"

    def test_frame_is_passed_through(self):
        self.send(ddp(rows_frame(ROW_COLOURS)))
        self.assertEqual(self.shown()[0][3], rows_frame(ROW_COLOURS))


class SessionTest(BridgeTest):
    def stream(self, seconds: float, frame: bytes | None = None) -> None:
        """LedFx's side: frames every 50 ms for a while."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.send(ddp(frame or rows_frame(ROW_COLOURS)))
            time.sleep(0.05)

    def test_keep_alives_go_out_while_frames_flow(self):
        with mock.patch.object(cube, "KEEP_ALIVE_EVERY", 0.2):
            self.stream(0.7)
            self.shown(3)
        self.assertGreaterEqual(self.fake.methods.count("udp_sess_keep_alive"), 2)
        self.assertEqual(self.fake.methods.count("udp_sess_new"), 1)

    def test_expired_session_refused_at_keep_alive_is_reopened(self):
        with mock.patch.object(cube, "KEEP_ALIVE_EVERY", 0.2):
            self.send(ddp(rows_frame(ROW_COLOURS)))
            first = self.shown(1)[0][1]
            self.fake.forget_tokens()
            later = rows_frame([(5, 5, 5)] * cube.ROWS)
            end = time.monotonic() + 3
            while time.monotonic() < end and not any(f[1] != first for f in self.fake.frames):
                self.send(ddp(later))
                time.sleep(0.05)
            _, _, direct, pixels = next(f for f in self.fake.frames if f[1] != first)
        self.assertTrue(direct, "direct mode not turned on in the new session")
        self.assertEqual(pixels, later)
        self.assertEqual(self.fake.methods.count("udp_sess_new"), 2)

    def test_refused_frame_makes_the_next_open_a_session(self):
        self.send(ddp(rows_frame(ROW_COLOURS)))
        self.shown(1)
        self.fake.forget_tokens()
        # Refused, so not shown, and the refusal ends the session.
        self.send(ddp(rows_frame(ROW_COLOURS)))
        self.assertTrue(wait_for(lambda: self.lamp.token is None))
        self.send(ddp(rows_frame(ROW_COLOURS)))
        _, _, direct, _ = self.shown(2)[1]
        self.assertTrue(direct)
        self.assertEqual(self.fake.methods.count("udp_sess_new"), 2)

    def test_quiet_lamp_keeps_its_session(self):
        # Keep-alives go unanswered, as over a stalled Wi-Fi link. Opening a
        # second session then is what froze the real lamp, so the bridge keeps
        # streaming into the one it has, and says the lamp is quiet.
        with self.fake.lock:
            self.fake.unanswered.add("udp_sess_keep_alive")
        with mock.patch.object(cube, "KEEP_ALIVE_EVERY", 0.1), \
                mock.patch.object(cube, "SILENT_WARN_AFTER", 0.3), \
                self.assertLogs("cube_lite_ddp", "WARNING") as logs:
            self.stream(0.8)
            self.assertTrue(wait_for(lambda: self.lamp._silent))
        self.assertEqual(self.fake.methods.count("udp_sess_new"), 1)
        self.assertEqual(len({f[1] for f in self.shown(5)}), 1)
        self.assertIn("has not answered", "\n".join(logs.output))
        self.assertFalse(self.bridge._failing)

    def test_unconfirmed_direct_mode_keeps_the_session(self):
        with self.fake.lock:
            self.fake.unanswered.add("activate_fx_mode")
        with mock.patch.object(cube, "REPLY_TIMEOUT", 0.05), \
                self.assertLogs("cube_lite_ddp", "WARNING") as logs:
            self.stream(0.6)
            shown = self.shown(3)
        self.assertEqual(self.fake.methods.count("udp_sess_new"), 1)
        self.assertEqual(len({f[1] for f in shown}), 1)
        self.assertTrue(all(direct for _, _, direct, _ in shown))
        self.assertIn("did not confirm direct mode", "\n".join(logs.output))

    def test_stream_after_a_pause_opens_a_fresh_session(self):
        with mock.patch.object(cube, "RESTART_AFTER", 0.2):
            self.send(ddp(rows_frame(ROW_COLOURS)))
            first = self.shown(1)[0][1]
            time.sleep(0.3)
            self.send(ddp(rows_frame(ROW_COLOURS)))
            second = self.shown(2)[1][1]
        self.assertNotEqual(first, second)

    def test_lamp_that_opens_no_session_is_retried_and_frames_resume(self):
        with mock.patch.object(cube, "RETRY_AFTER", 0.3), mock.patch.object(cube, "REPLY_TIMEOUT", 0.05), \
                mock.patch.object(cube, "HANDSHAKE_TIMEOUT", 0.05):
            with self.fake.lock:
                self.fake.silent = True
            self.send(ddp(rows_frame(ROW_COLOURS)))
            self.assertTrue(wait_for(lambda: self.bridge._failing))
            with self.fake.lock:
                self.fake.silent = False
            # Frames go on arriving while the lamp is away; the newest is shown.
            latest = rows_frame([(7, 7, 7)] * cube.ROWS)
            self.send(ddp(latest))
            self.assertEqual(self.shown(1)[0][3], latest)
        self.assertFalse(self.bridge._failing)


class PieceTest(unittest.TestCase):
    def test_parse_ddp(self):
        self.assertEqual(cube.parse_ddp(ddp(b"abc", offset=6)), (6, b"abc", True))
        self.assertEqual(cube.parse_ddp(ddp(b"abc", push=False)), (0, b"abc", False))
        self.assertIsNone(cube.parse_ddp(ddp(b"abc", flags=0x40 | cube.DDP_REPLY)))
        self.assertIsNone(cube.parse_ddp(ddp(b"abc", dest=0)))
        self.assertEqual(cube.parse_ddp(ddp(b"abc", dest=255)), (0, b"abc", True))

    def test_pattern_frame(self):
        frame = cube.pattern_frame()
        self.assertEqual(len(frame), cube.FRAME_BYTES)
        row = cube.COLUMNS * 3
        self.assertEqual(frame[:3], b"\xff\xff\xff")
        self.assertEqual(frame[3:6], b"\xff\x00\x00")  # first row red
        self.assertEqual(frame[4 * row + 3:4 * row + 6], b"\xff\x00\xff")  # last row magenta
        self.assertEqual(frame[4 * row:4 * row + 3], b"\xff\xff\xff")

    def test_frame_fits_one_datagram(self):
        data = base64.b64encode(b"\xff" * cube.FRAME_BYTES).decode()
        message = {"id": 2**31, "method": "update_leds", "params": [data], "token": "f" * 32}
        self.assertLess(len(json.dumps(message, separators=(",", ":"))) + 2, cube.MAX_DATAGRAM)


class CommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        logging.getLogger("cube_lite_ddp").setLevel(logging.CRITICAL)

    def test_port_in_use_exits_1(self):
        taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        taken.bind((LOOPBACK, 0))
        try:
            with mock.patch("sys.stderr"):
                code = cube.main([LOOPBACK, "--listen", LOOPBACK, "--port", str(taken.getsockname()[1])])
        finally:
            taken.close()
        self.assertEqual(code, 1)

    def test_lamp_is_required(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as raised:
            cube.main([])
        self.assertEqual(raised.exception.code, 2)

    def test_test_pattern_reaches_lamp(self):
        fake = FakeLamp()
        fake.start()
        try:
            with mock.patch.object(cube, "LAMP_PORT", fake.port), mock.patch("sys.stdout"):
                code = cube.run_test(LOOPBACK, "top-left")
            self.assertEqual(code, 0)
            self.assertTrue(wait_for(lambda: fake.frame_count() == 1))
            self.assertEqual(fake.frames[0][3], cube.lamp_order(cube.pattern_frame(), "top-left"))
        finally:
            fake.stop()

    def test_test_with_no_lamp_exits_1(self):
        fake = FakeLamp()
        fake.silent = True
        fake.start()
        try:
            with mock.patch.object(cube, "LAMP_PORT", fake.port), mock.patch.object(cube, "HANDSHAKE_TIMEOUT", 0.05), \
                    mock.patch("sys.stderr"):
                self.assertEqual(cube.run_test(LOOPBACK, "top-left"), 1)
        finally:
            fake.stop()


if __name__ == "__main__":
    unittest.main()
