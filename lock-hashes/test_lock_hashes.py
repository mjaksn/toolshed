"""Tests for lock_hashes.py. Standard library only, so nothing to install.

    python -m unittest

The index is never contacted. Every test replaces the function that asks PyPI
with one answering from a fixed table, so the tests say what the rewrite does
with what the index returns and nothing about the index itself.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lock_hashes

# Two packages, one with a single file and one with several, so both the
# single-line and the continuation-line shapes of a hash block are exercised.
INDEX = {
    ("one", "1.0"): ["aa" * 32],
    ("many", "2.5.1"): ["cc" * 32, "bb" * 32, "dd" * 32],
}


def fake_hashes_for(name: str, version: str) -> list[str]:
    return sorted(INDEX[(name, version)])


def block(name: str, version: str, marker: str = "") -> str:
    """The hash block the rewrite is expected to produce for one pin."""
    digests = sorted(INDEX[(name, version)])
    lines = [f"{name}=={version}{marker} \\"]
    for index, digest in enumerate(digests):
        joiner = "" if index == len(digests) - 1 else " \\"
        lines.append(f"    --hash=sha256:{digest}{joiner}")
    return "\n".join(lines) + "\n"


class RewriteTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(lock_hashes, "hashes_for", fake_hashes_for)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The per-pin progress line goes to stderr and is noise here.
        quiet = contextlib.redirect_stderr(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_adds_a_hash_block_under_every_pin(self):
        text = "one==1.0\nmany==2.5.1\n"
        self.assertEqual(
            lock_hashes.rewrite(text),
            block("one", "1.0") + block("many", "2.5.1"),
        )

    def test_is_idempotent_over_its_own_output(self):
        once = lock_hashes.rewrite("one==1.0\nmany==2.5.1\n")
        self.assertEqual(lock_hashes.rewrite(once), once)

    def test_replaces_a_stale_hash_block(self):
        stale = "many==2.5.1 \\\n    --hash=sha256:" + "00" * 32 + "\n"
        self.assertEqual(lock_hashes.rewrite(stale), block("many", "2.5.1"))

    def test_keeps_the_environment_marker(self):
        text = 'one==1.0 ; sys_platform == "win32"\n'
        self.assertEqual(
            lock_hashes.rewrite(text),
            block("one", "1.0", ' ; sys_platform == "win32"'),
        )

    def test_leaves_everything_that_is_not_a_pin_alone(self):
        text = (
            "# a comment above\n"
            "\n"
            "-r other.txt\n"
            "one==1.0\n"
            "    # via something\n"
            "--no-binary :all:\n"
        )
        expected = (
            "# a comment above\n"
            "\n"
            "-r other.txt\n"
            + block("one", "1.0")
            + "    # via something\n"
            "--no-binary :all:\n"
        )
        self.assertEqual(lock_hashes.rewrite(text), expected)

    def test_hashes_are_sorted_so_the_output_is_stable(self):
        out = lock_hashes.rewrite("many==2.5.1\n")
        digests = [line.split("sha256:")[1].split()[0] for line in out.splitlines()[1:]]
        self.assertEqual(digests, sorted(digests))

    def test_strips_trailing_whitespace_from_kept_lines(self):
        self.assertEqual(lock_hashes.rewrite("# note   \n"), "# note\n")


class MainTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(lock_hashes, "hashes_for", fake_hashes_for)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(self._remove_dir)

    def _remove_dir(self):
        for child in self.dir.iterdir():
            child.unlink()
        self.dir.rmdir()

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = lock_hashes.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_rewrites_the_named_file(self):
        path = self.write("requirements.txt", "one==1.0\n")
        code, out, _ = self.run_main(str(path))
        self.assertEqual(code, 0)
        self.assertIn("wrote", out)
        self.assertEqual(path.read_text(encoding="utf-8"), block("one", "1.0"))

    def test_check_reports_stale_without_writing(self):
        path = self.write("requirements.txt", "one==1.0\n")
        code, _, err = self.run_main("--check", str(path))
        self.assertEqual(code, 1)
        self.assertIn("is stale", err)
        self.assertEqual(path.read_text(encoding="utf-8"), "one==1.0\n")

    def test_check_passes_a_current_file(self):
        path = self.write("requirements.txt", block("one", "1.0"))
        code, out, _ = self.run_main("--check", str(path))
        self.assertEqual(code, 0)
        self.assertIn("is current", out)

    def test_handles_several_files_and_reports_each(self):
        current = self.write("a.txt", block("one", "1.0"))
        stale = self.write("b.txt", "many==2.5.1\n")
        code, out, err = self.run_main("--check", str(current), str(stale))
        self.assertEqual(code, 1)
        self.assertIn("a.txt is current", out)
        self.assertIn("b.txt is stale", err)

    def test_missing_file_is_a_clean_error(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_main(str(self.dir / "absent.txt"))
        self.assertIn("absent.txt", str(raised.exception))

    def test_a_file_must_be_named(self):
        with self.assertRaises(SystemExit):
            self.run_main()


if __name__ == "__main__":
    unittest.main()
