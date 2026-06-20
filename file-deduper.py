#!/usr/bin/env python3
"""file-deduper.py - find duplicate files by SHA-256 hash (read-only).

Requires Python 3.8+

Usage:
    python file-deduper.py <directory> [options]

Options:
    --json              Output results as JSON (progress goes to stderr)
    --min-size KB       Minimum file size in KB to consider (default: 1)
    --color WHEN        Colorize output: auto (default) | always | never
    -q, --quiet         Suppress the banner and progress output
    -v, --verbose       Print elapsed time and throughput

Exit codes: 0 = no duplicates, 2 = duplicates found, 1 = error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat as _stat
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__",
    "venv", ".venv", "env", ".env",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".build",
    ".idea", ".vscode",
})

DEFAULT_MIN_SIZE_KB = 1
_HASH_CHUNK = 65_536  # 64 KB read buffer

# Exit codes, so scripts can branch on the result.
EXIT_OK = 0          # no duplicates
EXIT_ERROR = 1       # bad path / usage error
EXIT_DUPLICATES = 2  # duplicates found


# Color / output styling

class Style:
    """ANSI color wrapper. When disabled it returns text unchanged, so call
    sites never have to check whether color is on."""
    _CODES = {
        "cyan": "36", "dim": "2", "yellow": "33", "bold_yellow": "1;33",
        "red": "31", "bold_red": "1;31", "green": "32", "bold": "1",
    }

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.enabled else s

    def __getattr__(self, name: str):
        # style.yellow(...) etc., resolved from _CODES on first access.
        if name in Style._CODES:
            return lambda s: self._wrap(Style._CODES[name], s)
        raise AttributeError(name)


def _enable_windows_vt() -> bool:
    """Turn on ANSI handling for legacy Windows conhost (newer terminals
    already do it). Returns True if color is usable, False if not."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        ENABLE_VT = 0x0004
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VT))
    except Exception:
        return False


def make_style(mode: str, stream) -> Style:
    """Decide whether to colorize from --color, NO_COLOR, and TTY state."""
    if mode == "never":
        return Style(False)
    if mode == "auto":
        # NO_COLOR overrides auto, but an explicit --color always still wins.
        if os.environ.get("NO_COLOR") is not None:
            return Style(False)
        if not (hasattr(stream, "isatty") and stream.isatty()):
            return Style(False)
    return Style(_enable_windows_vt())


BANNER = r"""
  __ _ _        _        _
 / _(_) |___ __| |___ __| |_  _ _ __  ___ _ _
|  _| | / -_) _` / -_) _` | || | '_ \/ -_) '_|
|_| |_|_\___\__,_\___\__,_|\_,_| .__/\___|_|
   find duplicate files by SHA-256  |  read-only
"""


def print_banner(style: Style, stream) -> None:
    """Print the ASCII-art banner to the given stream (text mode, non-quiet)."""
    print(style.cyan(BANNER.rstrip("\n")), file=stream)
    print(file=stream)


# Core logic

def _open_nofollow(path: Path) -> int:
    # O_NOFOLLOW (Unix) closes the gap between an earlier lstat and this open:
    # a symlink swapped in now raises instead of being followed. O_BINARY is for
    # Windows, no-op elsewhere.
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    if getattr(os, "O_NOFOLLOW", 0) == 0:
        # Windows has no O_NOFOLLOW, so re-check the open fd and bail on a
        # symlink or non-regular file. Shrinks the race; doesn't close it.
        try:
            if _stat.S_ISLNK(os.lstat(path).st_mode) or not _stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(f"refusing to open non-regular or symlinked file: {_display(path)}")
        except Exception:
            os.close(fd)
            raise
    return fd


def _hash_fd(fd: int) -> str:
    h = hashlib.sha256()
    while chunk := os.read(fd, _HASH_CHUNK):
        h.update(chunk)
    return h.hexdigest()


def hash_file(path: Path) -> str:
    fd = _open_nofollow(path)
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def walk_files(
    root: Path, min_size: int, skip_dirs: frozenset[str]
) -> list[tuple[int, Path, tuple[int, int] | None]]:
    """(size, path, inode_key) for every regular file >= min_size under root.

    lstat skips symlinks/FIFOs/sockets/devices in one syscall and hands back the
    size so callers don't stat again. The (device, inode) key is recorded so a
    later delete can confirm it is unlinking the same file that was scanned;
    hardlinks are collapsed by that same key, since deleting one frees nothing.
    """
    found: list[tuple[int, Path, tuple[int, int] | None]] = []
    seen_inodes: set[tuple[int, int]] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        # prune in place so os.walk never descends into skipped dirs
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                st = p.lstat()
                if not _stat.S_ISREG(st.st_mode):  # skips symlinks, devices, FIFOs
                    continue
                if st.st_size < min_size:
                    continue
                # st_ino == 0 means no inode from the platform; don't collapse
                # then (a false dup is safer than dropping a file).
                key = (st.st_dev, st.st_ino) if st.st_ino else None
                if key is not None:
                    if key in seen_inodes:
                        continue
                    seen_inodes.add(key)
                found.append((st.st_size, p, key))
            except OSError:
                pass  # unreadable or gone since the walk started
    return found


def find_duplicates(
    files: list[tuple[int, Path, tuple[int, int] | None]],
    style: Style | None = None,
    quiet: bool = False,
) -> dict[str, tuple[int, list[Path]]]:
    """Map hash -> (size, paths) for every SHA-256 seen more than once.

    Bucket by size first, then hash only the size collisions; the files whose
    size is already unique are never read.
    """
    style = style or Style(False)

    # group by size (already known from the walk, no stat needed)
    by_size: dict[int, list[Path]] = defaultdict(list)
    for size, p, _ in files:
        by_size[size].append(p)

    candidates = [(size, p) for size, ps in by_size.items() if len(ps) > 1 for p in ps]

    # Hash the candidates. Progress is measured in bytes, not file count, so a
    # handful of huge files still shows movement.
    by_hash: dict[str, list[Path]] = defaultdict(list)
    hash_sizes: dict[str, int] = {}
    total = len(candidates)
    total_bytes = sum(size for size, _ in candidates)
    done_bytes = 0
    last_tick = 0.0
    for i, (size, p) in enumerate(candidates, 1):
        try:
            h = hash_file(p)
            by_hash[h].append(p)
            hash_sizes.setdefault(h, size)
        except OSError:
            pass
        done_bytes += size
        now = time.monotonic()
        # throttle to ~5 updates/sec so we don't flood stderr, plus a final line
        if not quiet and (now - last_tick > 0.2 or i == total):
            last_tick = now
            pct = (done_bytes / total_bytes * 100) if total_bytes else 100
            line = (f"  hashing {i}/{total} files  "
                    f"({fmt_size(done_bytes)}/{fmt_size(total_bytes)}, {pct:.0f}%)")
            end = "\n" if i == total else "\r"
            print(style.dim(line.ljust(60)), end=end, file=sys.stderr, flush=True)

    return {h: (hash_sizes[h], ps) for h, ps in by_hash.items() if len(ps) > 1}


# Formatting helpers

def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def _display(path: object) -> str:
    """Escape non-printable characters (newlines, ANSI escapes) in a path so a
    hostile filename can't rewrite the terminal. Ordinary Unicode is left as-is.
    """
    return "".join(
        ch if (ch == " " or ch.isprintable()) else f"\\x{ord(ch):02x}"
        for ch in str(path)
    )


def _display_err(e: object) -> str:
    """Like _display, but for exceptions. OSError puts the offending filename in
    its message, so escapes can ride in through the error text too.
    """
    return _display(str(e))


# Reporters

def report_text(
    root: Path,
    duplicates: dict[str, tuple[int, list[Path]]],
    style: Style | None = None,
) -> None:
    style = style or Style(False)
    print(f"Scanned: {_display(root)}")
    if not duplicates:
        print(style.green("No duplicates found."))
        return

    total_wasted = 0
    groups = sorted(duplicates.items(), key=lambda kv: kv[1][0], reverse=True)

    for i, (h, (size, paths)) in enumerate(groups, 1):
        wasted = size * (len(paths) - 1)
        total_wasted += wasted
        print(
            f"\n{style.bold(f'Group {i}')}: {len(paths)} copies  "
            f"{fmt_size(size)} each  "
            f"{style.yellow(f'{fmt_size(wasted)} wasted')}"
        )
        print(style.dim(f"  hash: {h[:16]}..."))
        for p in sorted(paths):
            print(f"  {_display(p)}")

    bar = "-" * 60
    print(f"\n{bar}")
    print(
        f"{len(duplicates)} group(s)  |  "
        f"{style.bold_yellow(f'{fmt_size(total_wasted)} wasted in total')}"
    )


def report_json(root: Path, duplicates: dict[str, tuple[int, list[Path]]]) -> None:
    total_wasted = 0
    groups = []
    for h, (size, paths) in duplicates.items():
        wasted = size * (len(paths) - 1)
        total_wasted += wasted
        groups.append({
            "hash": h,
            "file_size_bytes": size,
            "wasted_bytes": wasted,
            "files": sorted(str(p) for p in paths),
        })
    groups.sort(key=lambda g: g["wasted_bytes"], reverse=True)

    print(json.dumps({
        "scanned_root": str(root),
        "duplicate_groups": len(groups),
        "total_wasted_bytes": total_wasted,
        "groups": groups,
    }, indent=2, ensure_ascii=False))


# Interactive cleanup

def _delete_verified(path: Path, expected_key: tuple[int, int] | None, expected_hash: str) -> str:
    """Confirm the file is still the scanned copy, then remove it.

    Opens it without following symlinks, checks the (device, inode) recorded at
    scan time and the SHA-256 both still match, then unlinks relative to the
    parent directory so the entry removed is the one that was checked. Returns
    "deleted" or "skipped"; raises OSError on an open or unlink failure.
    """
    fd = _open_nofollow(path)
    try:
        st = os.fstat(fd)
        if expected_key is not None and (st.st_dev, st.st_ino) != expected_key:
            return "skipped"
        if _hash_fd(fd) != expected_hash:
            return "skipped"
    finally:
        os.close(fd)

    if os.unlink in os.supports_dir_fd:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.unlink(path.name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    else:
        path.unlink()
    return "deleted"


def interactive_delete(
    duplicates: dict[str, tuple[int, list[Path]]],
    style: Style | None = None,
    inodes: dict[Path, tuple[int, int] | None] | None = None,
) -> None:
    """Offer to delete the redundant copies, keeping the oldest in each group.

    Requires two confirmations and does nothing when stdin isn't a terminal.
    """
    style = style or Style(False)
    if not sys.stdin.isatty():
        return

    # same order as report_text (largest first)
    groups = sorted(duplicates.items(), key=lambda kv: kv[1][0], reverse=True)

    try:
        answer = input(
            "\nDelete redundant copies, keeping the oldest in each group? [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer != "y":
        print("No files deleted.")
        return

    try:
        raw = input(
            f"Exclude groups by number (1-{len(groups)}), comma-separated"
            " -- or Enter to include all: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return

    excluded: set[int] = set()
    for tok in (t.strip() for t in raw.split(",") if t.strip()):
        try:
            n = int(tok)
            if 1 <= n <= len(groups):
                excluded.add(n)
            else:
                print(f"  warning: group {n} is out of range, ignoring.", file=sys.stderr)
        except ValueError:
            print(f"  warning: '{_display(tok)}' is not a number, ignoring.", file=sys.stderr)

    # Build the plan: keep oldest, delete the rest. Each entry carries its
    # group hash for the re-check just before unlinking.
    plan: list[tuple[int, Path, int, float, str]] = []  # (group_num, path, size, mtime, hash)
    for i, (h, (size, paths)) in enumerate(groups, 1):
        if i in excluded:
            continue
        # stat each copy on its own so one bad file doesn't sink the group;
        # tiebreak on path for a stable order
        valid: list[tuple[float, str, Path]] = []
        for p in paths:
            try:
                valid.append((p.stat().st_mtime, str(p), p))
            except OSError as e:
                print(f"  warning: group {i}: {_display_err(e)} -- skipping that file.", file=sys.stderr)
        if len(valid) < 2:
            print(f"  warning: group {i}: fewer than 2 readable copies -- skipping group.",
                  file=sys.stderr)
            continue
        valid.sort(key=lambda x: (x[0], x[1]))
        for mtime, _, p in valid[1:]:
            plan.append((i, p, size, mtime, h))

    if not plan:
        print("No files to delete after exclusions.")
        return

    total_freed = sum(size for _, _, size, _, _ in plan)
    print(f"\nDeletion plan -- {len(plan)} file(s), "
          f"freeing {style.bold_yellow(fmt_size(total_freed))}:")
    for group_num, path, size, mtime, _ in plan:
        ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
        print(f"  [group {group_num}]  {_display(path)}")
        print(style.dim(f"             size: {fmt_size(size)}  |  last modified: {ts}"))

    try:
        confirm = input(
            style.bold_red("\nProceed with deletion? This cannot be undone.") + " [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return
    if confirm != "y":
        print("Aborted. No files deleted.")
        return

    deleted = 0
    errors = 0
    skipped = 0
    try:
        for _, path, _, _, group_hash in plan:
            expected_key = inodes.get(path) if inodes else None
            try:
                result = _delete_verified(path, expected_key, group_hash)
            except OSError as e:
                print(style.bold_red(f"  error: {_display(path)}: {_display_err(e)}"), file=sys.stderr)
                errors += 1
                continue
            if result == "skipped":
                print(style.yellow(f"  skipped (changed since scan): {_display(path)}"),
                      file=sys.stderr)
                skipped += 1
            else:
                print(style.red(f"  deleted: {_display(path)}"))
                deleted += 1
    except KeyboardInterrupt:
        print("\nInterrupted -- stopping deletion.", file=sys.stderr)

    summary = f"\n{deleted} file(s) deleted"
    if skipped:
        summary += f", {skipped} skipped (changed since scan)"
    if errors:
        summary += f", {errors} could not be deleted (see errors above)"
    print(summary + ".")


# CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="file-deduper",
        description="Find duplicate files by SHA-256 hash (read-only by default).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python file-deduper.py ~/Downloads
  python file-deduper.py ~/Documents --json
  python file-deduper.py . --min-size 100
""",
    )
    p.add_argument("directory", help="Directory to scan recursively")
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit results as JSON on stdout (progress still goes to stderr)",
    )
    p.add_argument(
        "--min-size",
        type=int,
        default=DEFAULT_MIN_SIZE_KB,
        metavar="KB",
        help=f"Skip files smaller than this many KB (default: {DEFAULT_MIN_SIZE_KB})",
    )
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="When to colorize output (default: auto, color only at a TTY; "
             "also honors the NO_COLOR env var)",
    )
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress the banner and progress output on stderr",
    )
    verbosity.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Print extra detail (elapsed time and throughput)",
    )
    return p


def main() -> int:
    # Windows streams default to cp1252; force UTF-8 so Unicode paths print.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args()

    if args.min_size < 1:
        parser.error("--min-size must be at least 1")

    # judge color against the stream that carries the report: stdout for JSON,
    # stderr for the human-facing text report and banner
    style = make_style(args.color, sys.stdout if args.json else sys.stderr)

    if not args.json and not args.quiet:
        print_banner(style, sys.stderr)

    root = Path(args.directory).expanduser().resolve()
    if not root.exists():
        print(style.bold_red(f"error: directory not found: {_display(root)}"), file=sys.stderr)
        return EXIT_ERROR
    if not root.is_dir():
        print(style.bold_red(f"error: not a directory: {_display(root)}"), file=sys.stderr)
        return EXIT_ERROR

    min_bytes = args.min_size * 1024
    started = time.monotonic()

    # Progress always goes to stderr so --json stdout stays clean.
    if not args.quiet:
        print(style.dim(f"Scanning {_display(root)}"), file=sys.stderr)
        print(style.dim(f"Min size: {fmt_size(min_bytes)}  |  "
                        f"skip dirs: {len(SKIP_DIRS)}"), file=sys.stderr)

    files = walk_files(root, min_bytes, SKIP_DIRS)
    inodes = {p: key for _, p, key in files}
    if not args.quiet:
        print(style.dim(f"Files eligible: {len(files)}"), file=sys.stderr)

    if not files:
        if args.json:
            print(json.dumps({"scanned_root": str(root), "duplicate_groups": 0,
                               "total_wasted_bytes": 0, "groups": []}, ensure_ascii=False))
        else:
            print(style.green("No eligible files found."))
        return EXIT_OK

    duplicates = find_duplicates(files, style=style, quiet=args.quiet)

    if not args.quiet:
        elapsed = time.monotonic() - started
        msg = f"Done - {len(duplicates)} duplicate group(s) found."
        if args.verbose:
            scanned = sum(size for size, _, _ in files)
            rate = scanned / elapsed if elapsed else 0
            msg += (f"  [{elapsed:.1f}s, {len(files)} files, "
                    f"{fmt_size(scanned)} eligible, {fmt_size(rate)}/s]")
        print(style.dim(msg), file=sys.stderr)

    if args.json:
        report_json(root, duplicates)
    else:
        report_text(root, duplicates, style=style)
        interactive_delete(duplicates, style=style, inodes=inodes)

    return EXIT_DUPLICATES if duplicates else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
