#!/usr/bin/env bash
# The check CI runs for WinEvents. Also the way to run the suite by hand on a
# clean machine, which is the point of keeping it here rather than inline in
# ci.json: a check nobody can run locally is a check nobody can debug.
#
# Windows only, because the tool is. The tests mock the Win32 calls and the
# registry, so they need neither a real event log nor administrator rights, but
# pywin32 still has to import, and it only installs on Windows.
set -euo pipefail

# Printed rather than assumed. The tools job deliberately has no setup-python
# step, so this runs against whatever interpreter the runner image ships, and
# that is the first thing worth knowing when a run fails.
python --version

# --require-hashes is the half that matters. Left off, pip takes the versions
# and verifies nothing. This installs the test dependencies, which pull in the
# runtime ones, because the tests import the module under test and that imports
# win32evtlog at module scope.
python -m pip install --disable-pip-version-check --require-hashes --requirement requirements-dev.txt

python -m pytest -q
