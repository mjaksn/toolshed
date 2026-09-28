# ReceiveFile

Receives one file from a browser and exits. It serves a small page with a file
picker, a box for the name to save it as, and an upload button. The first file
uploaded is saved into the directory it was started in, and then it stops.

```
python receive_file.py
```

It prints the addresses to open, one for this machine and one for others on the
network, and waits:

```
Waiting for one file, to save into /home/me/incoming
  http://localhost:3000/
  http://192.168.1.20:3000/
Ctrl+C stops it without receiving anything.
```

The name box fills in with the picked file's name, and can be changed to save
it as something else. After the upload the page says what was saved and how
big it is, the program prints where it saved it, and it exits with status 0.
Stopped with Ctrl+C before a file has been saved, it exits with status 1, and
an upload still arriving at the time is not kept.

## Changing the port

It listens on port 3000. To use another, edit the `PORT = 3000` line near the
top of `receive_file.py`. If something else already has the port, another
ReceiveFile included, it says so and exits with status 1 rather than starting.

## What it will and will not save

- Only a plain file name, never a path, so nothing lands outside the directory
  it was started in. A name with a `/`, `\` or `:` in it is refused, the colon
  because on Windows it names a drive or a hidden stream inside a file.
- Never under a name ending in a dot or a space, which Windows would quietly
  drop, saving the file under a different name from the one reported.
- Never under a name Windows keeps for a device, such as `NUL`, `CON` or
  `COM1`, with or without an extension, which would throw the file away. These
  are refused on every platform, so a name is accepted or not wherever it runs.
- Never over a file that is already there. The page says so, and a different
  name can be given and the upload tried again without restarting anything.
- Never part of a file. An upload that stops short is deleted, and the program
  carries on waiting. One still arriving when Ctrl+C is pressed is deleted too.

Only one file is ever saved: once one has been, any other upload is refused.
The file is written to disk as it arrives rather than held in memory, so its
size is limited only by the disk.

## Who can send it a file

Anyone who can reach this machine on that port, for as long as it is running.
It listens on every network interface, because the usual point is to send a
file from another device, and it asks for no password. Run it on a network you
trust, and only while you are expecting the file.

Uploads are taken only from its own page, whose script marks them with a
header. A web page from anywhere else, open in a browser on this network while
it runs, cannot add that header without a check the server never passes, so it
cannot use up the one upload.

The page is plain HTTP, so the file crosses the network unencrypted.

## What it needs

Python 3.9 or later and nothing else, and a browser with JavaScript on the
sending side, which fills in the name box and sends the file. It runs anywhere
Python does. A firewall on the receiving machine may need to allow the port for
another device to reach it.

## Tests

```
bash ./check.sh
```

This is what CI runs. It needs nothing installed: the tests start the real
server on the loopback address, on a port the operating system picks, saving
into a temporary directory, and talk HTTP to it. They cover the page, a file
saved byte for byte and the server stopping after it, including when the
sender has gone before the reply, an empty file, a name already taken, names
that are paths or not names at all, an upload with no length and one cut
short, and a second upload after the first was saved. They take about a
second.
