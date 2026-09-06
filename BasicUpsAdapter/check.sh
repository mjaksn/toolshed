#!/usr/bin/env bash
# The check CI runs for BasicUpsAdapter, and the way to run the suite by hand.
# A check nobody can run locally is a check nobody can debug.
#
# There is nothing to install. The tool has no dependencies, so there is no
# lockfile here and no `npm ci` step: the suite is standard library only and
# injects a fake command runner, so it needs neither pwrstat nor a UPS.
set -euo pipefail

# Printed rather than assumed. The tools job deliberately has no setup-node step,
# so this runs against whatever Node the runner image ships, and that is the
# first thing worth knowing when a run fails. package.json asks for 20 or newer.
node --version

node --test
