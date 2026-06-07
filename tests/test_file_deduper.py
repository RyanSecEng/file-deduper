"""Tests for file-deduper.py (stdlib unittest, no third-party deps).

Run from the repo root with:

    python -m unittest discover -s tests

The script under test is named ``file-deduper.py`` (with a hyphen), which is
not a legal module name, so it is loaded by path via importlib rather than a
plain ``import``.
"""
import importlib.util
import io
import os
import stat as _stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


def _load_module():
    script = Path(__file__).resolve().parent.parent / "file-deduper.py"
    spec = importlib.util.spec_from_file_location("file_deduper", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fd = _load_module()


def _silence():
    """Swallow stdout+stderr; the functions under test print a lot."""
    return redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO())


class TempDirTestCase(unittest.TestCase):
    """Base class that gives each test an isolated, auto-removed directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, relpath, content=b"", mtime=None):
        """Create a file with bytes content; optionally pin its mtime."""
        p = self.root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        p.write_bytes(content)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p


# Formatting

class FmtSizeTests(unittest.TestCase):
    def test_units_scale(self):
        self.assertEqual(fd.fmt_size(0), "0.0 B")
        self.assertEqual(fd.fmt_size(512), "512.0 B")
        self.assertEqual(fd.fmt_size(1024), "1.0 KB")
        self.assertEqual(fd.fmt_size(1536), "1.5 KB")
        self.assertEqual(fd.fmt_size(1024 * 1024), "1.0 MB")
        self.assertEqual(fd.fmt_size(1024 ** 3), "1.0 GB")

    def test_boundary_just_below_unit(self):
        self.assertEqual(fd.fmt_size(1023), "1023.0 B")


# Hostile-filename neutralization

class DisplayTests(unittest.TestCase):
    def test_preserves_ordinary_unicode_and_space(self):
        self.assertEqual(fd._display("café report.pdf"), "café report.pdf")

    def test_escapes_newline_and_carriage_return(self):
        out = fd._display("evil\nname\r")
        self.assertNotIn("\n", out)
        self.assertNotIn("\r", out)
        self.assertIn("\\x0a", out)
        self.assertIn("\\x0d", out)

    def test_escapes_ansi_escape_sequence(self):
        # A filename trying to clear the screen / move the cursor.
        out = fd._display("\x1b[2Jgotcha")
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)
        self.assertTrue(out.endswith("gotcha"))

    def test_escapes_tab(self):
        self.assertIn("\\x09", fd._display("a\tb"))

    def test_display_err_neutralizes_escape_in_exception_text(self):
        # OSError stringifies with the offending path appended; make sure an
        # escape smuggled through the error message is also neutralized.
        err = OSError("No such file: '/x/\x1b[2J'")
        out = fd._display_err(err)
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)


# walk_files

class WalkFilesTests(TempDirTestCase):
    def test_finds_regular_files_above_min_size(self):
        self.write("a.bin", b"x" * 100)
        self.write("sub/b.bin", b"y" * 100)
        found = fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)
        names = sorted(p.name for _, p in found)
        self.assertEqual(names, ["a.bin", "b.bin"])

    def test_min_size_filter(self):
        self.write("small.bin", b"x" * 10)
        self.write("big.bin", b"x" * 5000)
        found = fd.walk_files(self.root, min_size=1000, skip_dirs=fd.SKIP_DIRS)
        names = [p.name for _, p in found]
        self.assertEqual(names, ["big.bin"])

    def test_prunes_skip_dirs(self):
        self.write("keep.bin", b"a" * 50)
        self.write("node_modules/pkg/index.js", b"b" * 50)
        self.write(".git/objects/deadbeef", b"c" * 50)
        found = fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)
        names = [p.name for _, p in found]
        self.assertEqual(names, ["keep.bin"])

    def test_returned_size_matches_bytes(self):
        self.write("a.bin", b"x" * 321)
        found = fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)
        self.assertEqual(found[0][0], 321)

    def test_hardlinks_collapsed_to_single_entry(self):
        target = self.write("orig.bin", b"z" * 200)
        link = self.root / "link.bin"
        try:
            os.link(target, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest(f"hardlinks unsupported here: {e}")
        if target.stat().st_ino == 0:
            self.skipTest("platform does not report inode numbers")
        found = fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)
        self.assertEqual(len(found), 1, "hardlink should not count as a duplicate")

    def test_symlink_is_skipped(self):
        target = self.write("real.bin", b"q" * 100)
        link = self.root / "link.bin"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest(f"symlinks unsupported here: {e}")
        found = fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)
        names = [p.name for _, p in found]
        self.assertEqual(names, ["real.bin"])


# find_duplicates

class FindDuplicatesTests(TempDirTestCase):
    def _walk(self):
        return fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)

    def test_identical_content_grouped(self):
        self.write("a.bin", b"hello world")
        self.write("sub/b.bin", b"hello world")
        dups = fd.find_duplicates(self._walk(), quiet=True)
        self.assertEqual(len(dups), 1)
        (size, paths), = dups.values()
        self.assertEqual(size, len(b"hello world"))
        self.assertEqual(sorted(p.name for p in paths), ["a.bin", "b.bin"])

    def test_unique_sizes_never_grouped(self):
        self.write("a.bin", b"a")
        self.write("b.bin", b"bb")
        self.write("c.bin", b"ccc")
        self.assertEqual(fd.find_duplicates(self._walk(), quiet=True), {})

    def test_same_size_different_content_not_grouped(self):
        # Same length, different bytes: collide on size, diverge on hash.
        self.write("a.bin", b"AAAA")
        self.write("b.bin", b"BBBB")
        self.assertEqual(fd.find_duplicates(self._walk(), quiet=True), {})

    def test_three_copies_one_group(self):
        for name in ("a", "b", "c"):
            self.write(f"{name}.bin", b"same bytes")
        dups = fd.find_duplicates(self._walk(), quiet=True)
        self.assertEqual(len(dups), 1)
        (_, paths), = dups.values()
        self.assertEqual(len(paths), 3)


# hash_file

class HashFileTests(TempDirTestCase):
    def test_matches_known_sha256(self):
        import hashlib
        content = b"the quick brown fox"
        p = self.write("a.bin", content)
        self.assertEqual(fd.hash_file(p), hashlib.sha256(content).hexdigest())

    def test_refuses_symlink(self):
        target = self.write("real.bin", b"data")
        link = self.root / "link.bin"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest(f"symlinks unsupported here: {e}")
        with self.assertRaises(OSError):
            fd.hash_file(link)


# report_json

class ReportJsonTests(TempDirTestCase):
    def test_structure_totals_and_sorting(self):
        import json
        # Small group: 2 copies of a 10-byte file -> 10 wasted.
        small = [self.root / "s1", self.root / "s2"]
        # Large group: 3 copies of a 100-byte file -> 200 wasted.
        large = [self.root / "l1", self.root / "l2", self.root / "l3"]
        duplicates = {
            "a" * 64: (10, small),
            "b" * 64: (100, large),
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            fd.report_json(self.root, duplicates)
        data = json.loads(buf.getvalue())

        self.assertEqual(data["scanned_root"], str(self.root))
        self.assertEqual(data["duplicate_groups"], 2)
        self.assertEqual(data["total_wasted_bytes"], 10 + 200)
        # Sorted by wasted bytes descending -> large group first.
        self.assertEqual(data["groups"][0]["wasted_bytes"], 200)
        self.assertEqual(data["groups"][1]["wasted_bytes"], 10)
        # Files are sorted strings.
        self.assertEqual(
            data["groups"][0]["files"],
            sorted(str(p) for p in large),
        )


# interactive_delete (the safety-critical path)

class InteractiveDeleteTests(TempDirTestCase):
    def _group(self, *paths_with_content):
        """Build a {hash: (size, [paths])} dict from (path, content) pairs."""
        import hashlib
        paths = []
        size = None
        h = None
        for p, content in paths_with_content:
            paths.append(p)
            size = len(content)
            h = hashlib.sha256(content).hexdigest()
        return {h: (size, paths)}

    def test_non_tty_is_a_noop(self):
        a = self.write("a.bin", b"dup")
        b = self.write("b.bin", b"dup")
        dups = fd.find_duplicates(
            fd.walk_files(self.root, 0, fd.SKIP_DIRS), quiet=True)
        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = False
        out, err = _silence()
        with out, err, mock.patch.object(fd.sys, "stdin", fake_stdin):
            fd.interactive_delete(dups)
        self.assertTrue(a.exists() and b.exists(), "nothing should be deleted")

    def test_keeps_oldest_deletes_rest(self):
        # oldest has the smallest mtime -> kept as canonical.
        old = self.write("old.bin", b"identical", mtime=1000)
        mid = self.write("mid.bin", b"identical", mtime=2000)
        new = self.write("new.bin", b"identical", mtime=3000)
        dups = fd.find_duplicates(
            fd.walk_files(self.root, 0, fd.SKIP_DIRS), quiet=True)

        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        out, err = _silence()
        # answers: opt-in "y", exclude-none "", confirm "y"
        with out, err, \
                mock.patch.object(fd.sys, "stdin", fake_stdin), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups)

        self.assertTrue(old.exists(), "oldest copy must be kept")
        self.assertFalse(mid.exists(), "newer copies must be deleted")
        self.assertFalse(new.exists(), "newer copies must be deleted")

    def test_decline_deletes_nothing(self):
        a = self.write("a.bin", b"dup", mtime=1000)
        b = self.write("b.bin", b"dup", mtime=2000)
        dups = fd.find_duplicates(
            fd.walk_files(self.root, 0, fd.SKIP_DIRS), quiet=True)

        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", fake_stdin), \
                mock.patch("builtins.input", side_effect=["n"]):
            fd.interactive_delete(dups)
        self.assertTrue(a.exists() and b.exists())

    def test_rehash_guard_skips_changed_file(self):
        # A duplicate that is modified between scan and delete must NOT be
        # deleted, because it is no longer a confirmed copy.
        old = self.write("old.bin", b"identical", mtime=1000)
        new = self.write("new.bin", b"identical", mtime=2000)
        dups = fd.find_duplicates(
            fd.walk_files(self.root, 0, fd.SKIP_DIRS), quiet=True)

        # Mutate the deletion candidate after the scan but keep its size so it
        # still appears in the (pre-built) plan.
        new.write_bytes(b"changed!!")

        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", fake_stdin), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups)

        self.assertTrue(old.exists(), "kept original untouched")
        self.assertTrue(new.exists(), "changed file must be skipped, not deleted")

    def test_excluded_group_is_untouched(self):
        a = self.write("a.bin", b"dup", mtime=1000)
        b = self.write("b.bin", b"dup", mtime=2000)
        dups = fd.find_duplicates(
            fd.walk_files(self.root, 0, fd.SKIP_DIRS), quiet=True)

        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        out, err = _silence()
        # opt-in "y", then exclude group "1", confirm "y"
        with out, err, \
                mock.patch.object(fd.sys, "stdin", fake_stdin), \
                mock.patch("builtins.input", side_effect=["y", "1", "y"]):
            fd.interactive_delete(dups)
        self.assertTrue(a.exists() and b.exists(),
                        "excluded group must be left intact")


if __name__ == "__main__":
    unittest.main()
