#!/usr/bin/env python3
"""Rewrite pip requirements files so every pin carries the hashes pip must verify.

A requirements file names an exact version of each package. That says what to
install but not what the bytes should be, so an installer following it takes
whatever the index hands over. Adding the hashes lets pip be run with
--require-hashes, which refuses anything it was not told to expect.

Every published file for a pinned version is listed, not only the one this
machine would choose. The same file is usually consumed on more than one
platform, and each platform selects a different wheel. A hash list missing the
wheel that gets selected fails the install rather than merely skipping the
check, so the whole set goes in.

The hashes come from the PyPI JSON API, which reports the digest the index
itself holds for each file. Nothing is downloaded and nothing is executed.

    python lock_hashes.py requirements.txt            rewrite the file
    python lock_hashes.py --check requirements.txt    exit 1 if it would change

Any number of files can be named. The version pins, the environment markers,
the comments and everything else that is not a hash line are left exactly as
they are. To move a version, edit the pin by hand as before and then run this.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# name==version, then an optional ; marker. Anything already carrying hashes is
# matched too, so this is safe to run over its own output.
PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)(?P<marker>\s*;[^\\\n]*)?")

TIMEOUT = 30


def hashes_for(name: str, version: str) -> list[str]:
    """Return the sha256 of every file PyPI holds for this exact version."""
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        raise SystemExit(f"{name} {version}: the index returned {err.code}") from err

    files = payload.get("urls", [])
    if not files:
        raise SystemExit(f"{name} {version}: the index lists no files for it")

    return sorted({entry["digests"]["sha256"] for entry in files})


def rewrite(text: str) -> str:
    """Return the requirements file with a hash block under every pin."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()

        # Continuation lines belong to a pin this loop has already rewritten.
        if line.lstrip().startswith("--hash="):
            continue

        found = PIN.match(line)
        if found is None:
            out.append(line)
            continue

        name = found.group("name")
        version = found.group("version")
        marker = (found.group("marker") or "").rstrip()

        digests = hashes_for(name, version)
        print(f"{name:<20} {version:<12} {len(digests)} file(s)", file=sys.stderr)

        out.append(f"{name}=={version}{marker} \\")
        for index, digest in enumerate(digests):
            joiner = "" if index == len(digests) - 1 else " \\"
            out.append(f"    --hash=sha256:{digest}{joiner}")

    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Rewrite the named files, or report whether they are already current."""
    parser = argparse.ArgumentParser(
        prog="lock_hashes.py",
        description=(
            "Rewrite pip requirements files so every pinned version carries the "
            "sha256 of every file the index publishes for it, which is what "
            "pip --require-hashes checks against."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="FILE",
        help="a requirements file to rewrite; name as many as you like",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if any file is not what this would produce",
    )
    args = parser.parse_args(argv)

    stale = False
    for name in args.paths:
        path = Path(name)
        try:
            current = path.read_text(encoding="utf-8")
        except OSError as err:
            raise SystemExit(f"{name}: {err.strerror}") from err
        updated = rewrite(current)

        if current == updated:
            print(f"{name} is current")
            continue

        if args.check:
            print(f"{name} is stale; run lock_hashes.py on it", file=sys.stderr)
            stale = True
            continue

        path.write_text(updated, encoding="utf-8")
        print(f"wrote {name}")

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
