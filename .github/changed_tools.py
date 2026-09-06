#!/usr/bin/env python3
"""Work out which tools a change touched, and print them as a CI matrix.

A tool is a top level directory holding a `ci.json`. That file is how a tool
asks to be checked and says what checking it means: which runner it needs and
what to run there. Nothing at the root knows anything else
about any tool, which is the rule the repository is built on, so this walks the
directories rather than consulting a list that would have to be edited every
time a tool arrives.

A tool without a `ci.json` is not checked. That is a deliberate state and not a
failure: most of what lands here is a script with nothing to run against it.

The output is a JSON array of matrix entries on stdout, and the same string as
`tools=<json>` appended to $GITHUB_OUTPUT when that variable is set. An empty
array means nothing needs checking, which the workflow turns into a skipped job
rather than a job with an empty matrix, because GitHub treats the latter as an
error.
"""

import json
import os
import pathlib
import subprocess
import sys

ZERO = "0" * 40
REQUIRED = ("runner", "check")


def git(*args):
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def declared_tools():
    """Every directory that asks to be checked, in a stable order."""
    found = {}
    for manifest in sorted(pathlib.Path(".").glob("*/ci.json")):
        entry = json.loads(manifest.read_text(encoding="utf-8"))
        missing = [key for key in REQUIRED if key not in entry]
        if missing:
            sys.exit(f"{manifest}: missing {', '.join(missing)}")
        found[manifest.parent.name] = {
            "name": manifest.parent.name,
            "runner": entry["runner"],
            "check": entry["check"],
        }
    return found


def touched(base, head):
    """The top level directory names a diff touched, or None for everything.

    None is the answer whenever the range cannot be trusted: a manual run has
    no range at all, and a first push or a force push leaves a base that is
    either all zeros or no longer in the history. Checking everything is the
    safe reading of "I do not know what changed".
    """
    if not base or base == ZERO:
        return None
    try:
        git("cat-file", "-e", f"{base}^{{commit}}")
    except subprocess.CalledProcessError:
        return None
    # Three dots, not two. A two-dot diff between the base branch's tip and the
    # head compares the two commits directly, so on a branch that is behind its
    # base every tool the base gained since the branch point comes back as
    # "changed" and is checked for nothing. Three dots diffs from the merge base,
    # which is what a pull request actually changes. On a fast-forward push the
    # merge base is the old tip and the two forms agree.
    changed = git("diff", "--name-only", f"{base}...{head}").splitlines()
    return {path.split("/", 1)[0] for path in changed if "/" in path}


def main():
    if len(sys.argv) != 4:
        sys.exit("usage: changed_tools.py <event> <base sha> <head sha>")
    event, base, head = sys.argv[1:]

    tools = declared_tools()
    if event == "workflow_dispatch":
        # Nobody dispatches a run by hand to check a subset. Run the lot.
        selected = list(tools.values())
    else:
        dirs = touched(base, head)
        selected = [
            tool
            for name, tool in tools.items()
            if dirs is None or name in dirs
        ]

    out = json.dumps(selected, separators=(",", ":"))
    print(out)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            handle.write(f"tools={out}\n")


if __name__ == "__main__":
    main()
