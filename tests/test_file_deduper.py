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
        names = sorted(p.name for _, p, _ in found)
        self.assertEqual(names, ["a.bin", "b.bin"])

    def test_min_size_filter(self):
        self.write("small.bin", b"x" * 10)
        self.write("big.bin", b"x" * 5000)
        found = fd.walk_files(self.root, min_size=1000, skip_dirs=fd.SKIP_DIRS)
        names = [p.name for _, p, _ in found]
        self.assertEqual(names, ["big.bin"])

    def test_prunes_skip_dirs(self):
        self.write("keep.bin", b"a" * 50)
        self.write("node_modules/pkg/index.js", b"b" * 50)
        self.write(".git/objects/deadbeef", b"c" * 50)
        found = fd.walk_files(self.root, min_size=0, skip_dirs=fd.SKIP_DIRS)
        names = [p.name for _, p, _ in found]
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
        names = [p.name for _, p, _ in found]
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

    def test_inode_mismatch_skips_file(self):
        # If the inode recorded at scan time no longer matches the file on disk,
        # it was swapped after the scan and must not be deleted.
        old = self.write("old.bin", b"identical", mtime=1000)
        new = self.write("new.bin", b"identical", mtime=2000)
        files = fd.walk_files(self.root, 0, fd.SKIP_DIRS)
        dups = fd.find_duplicates(files, quiet=True)

        inodes = {p: key for _, p, key in files}
        inodes[new] = (-1, -1)  # pretend new.bin was a different inode at scan

        fake_stdin = mock.Mock()
        fake_stdin.isatty.return_value = True
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", fake_stdin), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, inodes=inodes)

        self.assertTrue(old.exists(), "kept original untouched")
        self.assertTrue(new.exists(), "swapped file must be skipped, not deleted")

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


# Safety guard helpers

class SafetyHelperTests(TempDirTestCase):
    def test_is_within_true_for_descendant_and_self(self):
        sub = self.write("a/b.txt", b"x")
        self.assertTrue(fd._is_within(sub, self.root))
        self.assertTrue(fd._is_within(self.root, self.root))

    def test_is_within_false_for_sibling(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        self.assertFalse(fd._is_within(Path(other.name), self.root))

    def test_filesystem_root_detected(self):
        anchor = Path(self.root.anchor)  # "C:\\" on Windows, "/" on POSIX
        self.assertTrue(fd._is_filesystem_root(anchor))
        self.assertFalse(fd._is_filesystem_root(self.root))

    def test_protected_roots_all_exist(self):
        for r in fd._protected_roots():
            self.assertTrue(r.exists(), f"{r} should exist to be listed")


# Safety guards in interactive_delete

class DeletionGuardTests(TempDirTestCase):
    def _tty(self):
        m = mock.Mock()
        m.isatty.return_value = True
        return m

    def _dups(self):
        return fd.find_duplicates(
            fd.walk_files(self.root, 0, fd.SKIP_DIRS), quiet=True)

    def test_refuses_when_root_is_protected(self):
        a = self.write("a.bin", b"dup", mtime=1000)
        b = self.write("b.bin", b"dup", mtime=2000)
        dups = self._dups()
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[self.root]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]) as inp:
            fd.interactive_delete(dups, root=self.root)
        self.assertTrue(a.exists() and b.exists(), "must delete nothing")
        inp.assert_not_called()  # refused before any prompt

    def test_refuses_when_elevated(self):
        a = self.write("a.bin", b"dup", mtime=1000)
        b = self.write("b.bin", b"dup", mtime=2000)
        dups = self._dups()
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=True), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]) as inp:
            fd.interactive_delete(dups, root=self.root)
        self.assertTrue(a.exists() and b.exists())
        inp.assert_not_called()

    def test_allow_system_downgrades_refusal(self):
        old = self.write("old.bin", b"identical", mtime=1000)
        new = self.write("new.bin", b"identical", mtime=2000)
        dups = self._dups()
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=True), \
                mock.patch.object(fd, "_home_dir", return_value=self.root), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, root=self.root, allow_system=True)
        self.assertTrue(old.exists())
        self.assertFalse(new.exists(), "--allow-system should let deletion proceed")

    def test_protected_file_never_deleted_even_with_allow_system(self):
        prot = self.write("sys/keep.bin", b"identical", mtime=1000)
        n1 = self.write("n1.bin", b"identical", mtime=2000)
        n2 = self.write("n2.bin", b"identical", mtime=3000)
        dups = self._dups()
        sysdir = self.root / "sys"
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[sysdir]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch.object(fd, "_home_dir", return_value=self.root), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, root=self.root, allow_system=True)
        self.assertTrue(prot.exists(), "protected copy must never be deleted")
        self.assertTrue(n1.exists(), "oldest non-protected copy kept")
        self.assertFalse(n2.exists(), "redundant non-protected copy deleted")

    def test_outside_home_requires_typed_delete(self):
        old = self.write("old.bin", b"identical", mtime=1000)
        new = self.write("new.bin", b"identical", mtime=2000)
        dups = self._dups()
        home = self.root / "elsewhere"
        home.mkdir()
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch.object(fd, "_home_dir", return_value=home), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, root=self.root)
        self.assertTrue(old.exists() and new.exists(),
                        "a one-key 'y' must not satisfy the DELETE confirmation")

    def test_outside_home_typed_delete_proceeds(self):
        old = self.write("old.bin", b"identical", mtime=1000)
        new = self.write("new.bin", b"identical", mtime=2000)
        dups = self._dups()
        home = self.root / "elsewhere"
        home.mkdir()
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch.object(fd, "_home_dir", return_value=home), \
                mock.patch("builtins.input", side_effect=["y", "", "DELETE"]):
            fd.interactive_delete(dups, root=self.root)
        self.assertTrue(old.exists(), "oldest kept")
        self.assertFalse(new.exists(), "typed DELETE should proceed")

    def test_unowned_files_are_never_deleted(self):
        old = self.write("old.bin", b"identical", mtime=1000)
        new = self.write("new.bin", b"identical", mtime=2000)
        dups = self._dups()
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch.object(fd, "_home_dir", return_value=self.root), \
                mock.patch.object(fd, "_owned_by_caller", return_value=False), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, root=self.root)
        self.assertTrue(old.exists() and new.exists(),
                        "files the caller doesn't own must never be deleted")

    def test_max_deletes_cap_refuses_whole_run(self):
        a = self.write("a.bin", b"identical", mtime=1000)
        b = self.write("b.bin", b"identical", mtime=2000)
        c = self.write("c.bin", b"identical", mtime=3000)
        dups = self._dups()  # one group; plan would remove b and c
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch.object(fd, "_home_dir", return_value=self.root), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, root=self.root, max_deletes=1)
        self.assertTrue(a.exists() and b.exists() and c.exists(),
                        "a plan over the cap must be refused in full")

    def test_candidate_outside_scan_root_never_deleted(self):
        import hashlib
        inside = self.write("inside.bin", b"identical", mtime=1000)
        ext = tempfile.TemporaryDirectory()
        self.addCleanup(ext.cleanup)
        outside = Path(ext.name) / "outside.bin"
        outside.write_bytes(b"identical")
        os.utime(outside, (2000, 2000))
        h = hashlib.sha256(b"identical").hexdigest()
        dups = {h: (len(b"identical"), [inside, outside])}
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_protected_roots", return_value=[]), \
                mock.patch.object(fd, "_running_elevated", return_value=False), \
                mock.patch.object(fd, "_home_dir", return_value=self.root), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            fd.interactive_delete(dups, root=self.root)
        self.assertTrue(outside.exists(),
                        "a path outside the scan root must never be deleted")
        self.assertTrue(inside.exists(),
                        "with the outside copy filtered, the group has <2 candidates")


# Read-only-by-default at the main() level

class MainDeleteGatingTests(TempDirTestCase):
    def _tty(self):
        m = mock.Mock()
        m.isatty.return_value = True
        return m

    def test_no_delete_flag_is_read_only(self):
        # main() applies the default 1 KB minimum, so files must exceed it.
        blob = b"identical" * 256  # ~2.3 KB
        a = self.write("a.bin", blob, mtime=1000)
        b = self.write("b.bin", blob, mtime=2000)
        argv = ["file-deduper.py", str(self.root), "--color", "never"]
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "argv", argv), \
                mock.patch.object(fd.sys, "stdin", self._tty()):
            rc = fd.main()
        self.assertTrue(a.exists() and b.exists(),
                        "a run without --delete must never delete anything")
        self.assertEqual(rc, fd.EXIT_DUPLICATES)

    def test_delete_flag_enables_guarded_cleanup(self):
        blob = b"identical" * 256  # exceed the default 1 KB minimum
        old = self.write("old.bin", blob, mtime=1000)
        new = self.write("new.bin", blob, mtime=2000)
        argv = ["file-deduper.py", str(self.root), "--delete", "--color", "never"]
        out, err = _silence()
        with out, err, \
                mock.patch.object(fd.sys, "argv", argv), \
                mock.patch.object(fd.sys, "stdin", self._tty()), \
                mock.patch.object(fd, "_home_dir", return_value=self.root), \
                mock.patch("builtins.input", side_effect=["y", "", "y"]):
            rc = fd.main()
        self.assertTrue(old.exists(), "oldest kept")
        self.assertFalse(new.exists(), "--delete should enable guarded deletion")
        self.assertEqual(rc, fd.EXIT_DUPLICATES)


if __name__ == "__main__":
    unittest.main()
