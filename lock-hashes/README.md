# lock-hashes

Rewrites a pip requirements file so that every pinned version carries the
sha256 of every file PyPI publishes for it. That is the form
`pip install --require-hashes` checks against, and without it a pin says which
version to install but nothing about what the bytes should be.

```
python lock_hashes.py requirements.txt
python lock_hashes.py --check requirements.txt
```

The first rewrites the file in place. The second writes nothing and exits 1 if
the file is not what the first would produce, which is the form for CI. Any
number of files can be named in one run.

## What it does

For each line of the form `name==version`, with or without an environment
marker after a semicolon, it asks the PyPI JSON API for the files published
under that exact version and writes their digests under the pin, one
`--hash=sha256:` continuation line each, sorted so the output is stable. A hash
block already there is replaced, so it is safe to run over its own output.
Everything else, including comments, blank lines, `-r` includes and any other
pip option, is kept exactly as it was.

Every published file is listed, not only the one this machine would choose.
The same requirements file is usually consumed on more than one platform, and
each selects a different wheel. A hash list missing the wheel that gets
selected fails the install rather than merely skipping the check, so the whole
set goes in.

The hashes are the digests the index reports for each file. Nothing is
downloaded and nothing is executed.

It does not resolve dependencies or choose versions. Every package that needs
pinning has to be named in the file already, with `==`, and moving a version
is done by editing the pin by hand and then running this.

## What it needs

Python 3.9 or later and nothing else. It runs anywhere Python does, and needs
to reach `pypi.org`.

## Using it here

The shed's own hash-pinned files were made this way, and `--check` over them
is the way to see that they still match what the index holds. From the root,
for the ruff pin the lint job installs:

```
python lock-hashes/lock_hashes.py --check .github/requirements-lint.txt
```

The two requirements files in `WinEvents` take the same command, and all three
were current when this was written.

## Tests

```
python3 -m unittest
```

13 tests, standard library only, and they never contact the index. `check.sh`
runs exactly that and is what CI runs, on `ubuntu-latest`, per `ci.json`.

## History

This started as a script inside two projects of mine, nettail and readerboard,
each carrying a copy with the paths of its own lock files written into it. The
copies had already begun to drift. This one takes the paths as arguments
instead and is otherwise the same program.
