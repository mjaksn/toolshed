# cube-lite-ddp

Shows [LedFx](https://github.com/LedFx/LedFx) on a Yeelight Cube Lite, the lamp
with a matrix of 20 by 5 RGB pixels. It listens for DDP, the pixel protocol
LedFx and WLED send, and passes each frame on to the lamp over its own LAN
protocol, up to 30 frames a second.

```
python cube_lite_ddp.py 192.168.1.40
```

```
Listening for DDP on every interface, port 4048, and showing it on the Cube Lite at 192.168.1.40. Ctrl+C stops it.
Opened a session with the lamp at 192.168.1.40, in direct mode
```

## Why it is needed

The lamp speaks Matter, but Matter gives it a single colour and a brightness.
Yeelight's published LAN protocol, the Inter-Operation Specification on TCP
port 55443, does no better: every colour command sets the whole lamp, and it
takes about one command a second. Neither can draw a picture, let alone 30 a
second.

The lamp's firmware has a third way in, undocumented: a UDP command session on
port 55444 that takes a whole frame of pixels in one datagram, and up to 30
datagrams a second. It was worked out by the
[yeelight-cube-lite](https://github.com/max-src/yeelight-cube-lite) Home
Assistant integration, and the comment at the top of `cube_lite_ddp.py` sets
out what this tool relies on. LedFx cannot speak it, but it can send DDP to any
address, so this sits in between.

## What it needs

- Python 3.10 or later and nothing else. It runs anywhere Python does.
- LAN Control turned on for the lamp in the Yeelight app. A lamp that answers
  `--discover` has it on.
- One controller on the lamp's UDP session at a time. The yeelight-cube-lite
  integration uses the same session, and the lamp can freeze when two talk to
  it at once, so stop that integration before starting this, or the other way
  round. Home Assistant's Matter integration is a different path, and can stay.

## Setting it up

Find the lamp's address:

```
python cube_lite_ddp.py --discover
```

```
192.168.1.40  model CubeLite  name CubeLite  id 3ea8da4f3cf5
```

Giving the lamp a fixed address in the router saves doing this again.

Check which way up frames arrive:

```
python cube_lite_ddp.py 192.168.1.40 --test
```

The top row should turn red and the bottom row magenta, with a white column
at the left edge. If it does, what LedFx draws at its top left appears at the
lamp's top left. If not, see `--origin` below.

Start the bridge, as at the top, and leave it running. Then in LedFx:

1. Add a device of type **DDP**.
2. Give it the address of the machine running the bridge, `127.0.0.1` when it
   is the same machine as LedFx, port `4048`, and `100` pixels.
3. Optionally set the device's refresh rate to 30. LedFx sends 60 frames a
   second by default and the bridge drops every frame it cannot send on time,
   always keeping the newest, so 60 works; 30 just saves sending what is
   dropped.
4. In the settings of the device's virtual, set **rows** to `5`. That makes
   the 100 pixels a matrix of 20 columns by 5 rows, which the 2D effects draw
   on. Without it LedFx treats them as one long strip.

A lamp stood on its end wants LedFx's own rotate setting on the virtual,
rather than anything here.

## Options

| Option | What it does |
| --- | --- |
| `--discover` | Lists the Yeelight lamps that answer on this network, and exits. |
| `--test` | Sends the lamp the test pattern described above, and exits. |
| `--listen ADDRESS` | The address to listen for DDP on. Every interface by default. |
| `--port PORT` | The UDP port to listen for DDP on. 4048 by default, DDP's own. |
| `--origin top-left` | Where the first pixel of a frame belongs. `top-left`, the default, is the order LedFx lays a matrix out in. `bottom-left` is the lamp's own order, for a sender that already uses it. |

## What it does with the lamp

- Opens a UDP session with the lamp when the first frame arrives, and turns on
  direct mode in it, the mode that shows frames as sent.
- Sends the lamp nothing faster than 30 datagrams a second, which is as fast
  as the lamp answers reliably. A frame that arrives before the last one has
  gone replaces it, so the lamp is never behind.
- Checks the session is alive with a keep-alive after 20 seconds without
  hearing from the lamp, because frames are never answered and a dead session
  looks the same as a live one. When the lamp has dropped it, a new one is
  opened.
- Opens a fresh session when frames start again after a pause of more than
  10 seconds, because the lamp leaves direct mode the moment its app or Home
  Assistant tells it to do something else.
- When the lamp stops answering, sends the frame again at once from a new
  socket. The lamp can stop answering one client socket altogether while it
  answers a new one straight away, and in a stream it has done so within two
  minutes. If the new socket gets no answer either, says so once, drops frames,
  tries again every 5 seconds, each time from a new socket, and says when the
  lamp is back.

The lamp keeps showing the last frame after the bridge stops, until it is
given something else to show from its app or Home Assistant.

## Who can send it frames

Anyone who can reach this machine on the DDP port can put pixels on the lamp:
DDP has no password. When LedFx runs on the same machine, `--listen 127.0.0.1`
keeps the port off the network. When it runs elsewhere, a firewall on this
machine may need to allow UDP on the port for LedFx to reach it.

## Running it all the time

On Linux, [easy-systemd](../easy-systemd/) in this shed installs it as a
service that starts at boot and is restarted when it stops, run from that
tool's directory:

```
sudo ./install.sh cube-lite -- python3 /opt/cube-lite-ddp/cube_lite_ddp.py 192.168.1.40
```

## Tests

```
bash ./check.sh
```

This is what CI runs. It needs nothing installed and no lamp: the tests run
the bridge against a stand-in lamp on the loopback address, on ports the
operating system picks, which keeps to what the real one does, down to
refusing a token it no longer knows and never answering a frame.

They cover:
- a frame from LedFx shown the right way up, in a session in direct mode, and
  passed through unchanged with `--origin bottom-left`;
- a frame split over packets, shown only once the last arrives;
- the DDP timecode header;
- a frame short of 100 pixels, the rest left black, and one longer, cut;
- packets that are not pixels: queries, another DDP version, a destination
  other than the display, and packets cut short;
- frames sent faster than the lamp takes them, held to its rate with the
  newest shown;
- a stream carried in one session;
- a session the lamp has dropped, found by the keep-alive or by a refused
  frame, and a new one opened with direct mode on again;
- a fresh session after a pause;
- a lamp that stops answering the bridge's socket, and is answered from a
  new one without a pause;
- a lamp that stops answering and comes back;
- the frame fitting in one of the lamp's datagrams, and the test pattern;
- `--test` against a lamp and against no lamp, a port in use, and no lamp
  address given.

They take about eight seconds.
