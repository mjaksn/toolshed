#!/usr/bin/env bash
# The check CI runs for lock-hashes. Also the way to run the tests by hand,
# which is the point of keeping it here rather than inline in ci.json: a check
# nobody can run locally is a check nobody can debug.
#
# Nothing to install. The tool is standard library only and so are its tests,
# which never contact the index.
set -euo pipefail

# Printed rather than assumed. The tools job deliberately has no setup-python
# step, so this runs against whatever interpreter the runner image ships, and
# that is the first thing worth knowing when a run fails.
python3 --version

python3 -m unittest
