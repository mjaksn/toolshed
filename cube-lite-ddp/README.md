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
- One controller on the lamp's UDP session at a time. Opening a second
  session while one is streaming froze the lamp's display in testing, until it
  was power-cycled; that was on a day of heavy interference, which may have
  done it instead, but it is not worth the risk. That rules out running the
  yeelight-cube-lite integration alongside this, and `--test` while the bridge
  is streaming. Home Assistant's Matter integration is a different path, and
  can stay.
- A power supply that keeps up. The lamp draws up to about 6 W on a bright
  picture, and the yeelight-cube-lite integration recommends a 12 W port. On a
  laptop's USB port it ran dim and dropped off the network, and a long, thin
  cable from a good adapter was not much better; a short, sturdy one is.
- A steady Wi-Fi link. The lamp is on 2.4 GHz, and under heavy interference
  there it hung while taking 30 frames a second, where 15 held up. Locking it
  to the nearest access point helped too.

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

Do this before starting the bridge, not while it streams: `--test` opens a
session of its own. The top row should turn red and the bottom row magenta,
with a white column at the left edge. If it does, what LedFx draws at its top
left appears at the lamp's top left. If not, see `--origin` below.

Start the bridge, as at the top, and leave it running. Then in LedFx:

1. Add a device of type **DDP**.
2. Give it the address of the machine running the bridge, `127.0.0.1` when it
   is the same machine as LedFx, port `4048`, and `100` pixels.
3. Set the device's refresh rate to 30, or to 15 when the lamp's Wi-Fi is
   noisy. LedFx sends 60 frames a second by default and the bridge drops every
   frame it cannot send on time, always keeping the newest, so 60 works; 30
   saves sending what is dropped, and 15 halves the load on the lamp's link.
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
| `--stats SECONDS` | Logs a status line every `SECONDS` while frames flow, and tries the lamp's TCP port as often; see below. Off by default, and at least 5 when on. |
| `--max-fps N` | Sends the lamp at most `N` frames a second, from 1 to 30, dropping the rest and keeping the newest as ever. 30 by default, as many as the lamp takes. LedFx sends no fewer than 10 a second, so this is the way to go lower. |
| `--hold SECONDS` | After the lamp ends a session, as it does once it has restarted, sends it nothing for `SECONDS` before opening a new one, to give it time to finish starting up. 0 by default, opening one at once; at most 600. |

## Tracking down a hang

`--stats 60` leaves a record to read after the lamp hangs. Once a minute,
while anything flows, it logs a line like this:

```
Status: from LedFx 900 frames, longest gap 0.081 s, 0 dropped; to the lamp 900 frames at 31% level; session 1, 3605 s old; message ID 57712; keep-alives 6 sent, 6 answered, slowest 0.012 s; last heard 4.2 s ago; 0 errors; TCP 55443 open (5 ms)
```

That is, for the minute since the last line: the frames LedFx sent, the
longest it went quiet, and the frames that arrived faster than the lamp takes
them and were replaced; the frames sent to the lamp and how bright they were on
average; the session's number and age, and the ID of the last message sent;
the keep-alives sent and answered, with the slowest answer; when the lamp was
last heard from; the errors it answered with; and the last try of its TCP port.

It also tries the lamp's documented TCP port, 55443, as often, from its own
thread: it opens a connection and closes it, sending nothing, and logs the
time it took, which stands in for a ping. A Cube Lite in direct mode answers
nothing there, not even a query of its power, so there is nothing to ask. A
change in how that goes, such as the port refusing a connection or taking none,
is logged when it happens. No status line is logged while nothing flows, but
the tries go on, and a change in them is still logged.

Run under Docker, keep the log somewhere that outlives the container, such
as the journal with `--log-driver journald`: the default log goes with the
container when it is recreated, and the record with it.

## What it does with the lamp

- Opens a UDP session with the lamp when the first frame arrives, and turns on
  direct mode in it, the mode that shows frames as sent.
- Sends the lamp nothing faster than 30 datagrams a second, which is as fast
  as the lamp answers reliably. A frame that arrives before the last one has
  gone replaces it, so the lamp is never behind.
- Sends a keep-alive into the session every 10 seconds while frames flow,
  without waiting for its answer. The lamp ends a session about 40 seconds
  after its last keep-alive, and frames do not count, so three can be lost to
  a Wi-Fi stall before it does.
- Keeps the session however quiet the lamp goes. Over Wi-Fi its answers can be
  held up for seconds, and opening a second session to replace one that is
  only slow is what froze the lamp in testing. After 90 seconds without a word
  from the lamp it says so, and says again when the lamp answers.
- Opens a new session only when the lamp refuses the old one, which it does
  cleanly, by answering anything sent in it with an error, or when frames start
  again after a pause of more than 150 seconds, by when the old one has
  certainly expired. The new session turns direct mode on afresh, in case the
  lamp's app or Home Assistant gave the lamp something else to show meanwhile.
  With `--hold`, a session the lamp refused is followed by that long with
  nothing sent, frames dropped, before the new one is opened.
- When the lamp refuses frames as illegal requests, which it does once
  something else, such as its app, Home Assistant or a Matter controller, has
  given it an effect of its own, says so and turns direct mode on again in
  the same session, trying every 5 seconds until the lamp confirms it. It has
  done this a few minutes after being power-cycled, while frames flowed.
- Logs any other error the lamp answers with, the first time each comes in a
  session, with the whole answer and the kind of message it answered; the
  status line counts every one.
- When the lamp does not open a session at all, says so once, drops frames,
  tries again every 5 seconds, and says when the lamp is back.
- When the lamp hangs anyway, nothing sent over the network brings it back:
  power-cycle it. A hung lamp's display stops, it answers handshakes and
  keep-alives but no other command, and its sessions end after about a
  minute, so the bridge logs one session after another that did not confirm
  direct mode. Seen once: while frames went on arriving, a hung lamp also
  dropped off the network for about half a minute at a time, which it stopped
  doing while they were held back. In testing it hung on a weak power supply,
  and under heavy 2.4 GHz interference at 30 frames a second, which 15 rode
  out; it has also hung at 15 a second, after anything from minutes to
  hours. `--stats` records what led up to one.

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
refusing a token it no longer knows, never answering a frame in direct mode,
and refusing one outside it.

They cover:
- a frame from LedFx shown the right way up, in a session in direct mode, and
  passed through unchanged with `--origin bottom-left`;
- a frame split over packets, shown only once the last arrives;
- the DDP timecode header;
- a frame short of 100 pixels, the pixels it never fills left black, and one
  longer, cut;
- packets that are not pixels: queries and replies, another DDP version, a
  destination other than the display, and packets cut short;
- frames sent faster than the lamp takes them, held to its rate with the
  newest shown, and held to a lower rate set with `--max-fps`;
- a stream carried in one session;
- keep-alives sent while frames flow;
- a session the lamp has ended, found by a refused keep-alive or a refused
  frame, and a new one opened with direct mode on again;
- a lamp that stops answering keep-alives, or never confirms direct mode,
  keeping its one session, with a warning;
- a lamp taken out of direct mode, put back in it in the same session;
- errors the protocol does not explain, logged once a session each, naming
  what they answered;
- a fresh session after a pause;
- a lamp that ends a session sent nothing for the `--hold` time, then given a
  new one;
- a first frame that fails to send, retried in the session just opened;
- a lamp that opens no session, tried again until it does;
- the status line while frames flow, its counts of frames, keep-alives,
  answers and errors, and no line while nothing does;
- the TCP probe against a port that takes the connection, one that refuses
  it and a lamp that is gone, sending nothing, and logging only a change;
- the frame fitting in one of the lamp's datagrams, and the test pattern;
- `--test` against a lamp and against no lamp, a port in use, no lamp
  address given, a `--stats` interval that is too short, and a `--max-fps` or
  `--hold` out of range.

They take about sixteen seconds, or about twenty on Windows.
