# file-deduper

A single-file Python command-line tool that finds duplicate files in a directory tree by comparing their SHA-256 hashes. It is **read-only by default** — it reports what it finds and only deletes anything when you explicitly opt in through two interactive confirmations.

## Why

Duplicate files quietly accumulate: downloads grabbed twice, photos copied between folders, project assets pasted into a dozen places. They waste disk space and make a directory tree harder to reason about.

Comparing by name or size alone is unreliable — different files can share a size, and identical files can have different names. `file-deduper` compares the actual *content* via SHA-256, so two files are only ever reported as duplicates when their bytes are genuinely identical. It is also built to be safe to point at real data: it never follows symlinks, never collapses hardlinks into false "wasted space," neutralizes hostile filenames before printing, and re-verifies every file's hash immediately before deleting it.

## Features

- **Content-based detection** — duplicates are identified by SHA-256, not by name or size.
- **Two-pass speed** — files are grouped by size first (free, no reads); only size-collision sets are actually hashed, which skips reading the often 60–80% of a tree whose sizes are unique.
- **Read-only by default** — running the tool only reports; deletion is a separate, opt-in interactive step.
- **Safe interactive cleanup** — keeps the oldest file in each group, requires two confirmations, lets you exclude specific groups, and re-hashes each file right before unlinking so anything changed since the scan is never deleted.
- **JSON output** — `--json` emits machine-readable results on stdout (progress stays on stderr) for piping into other tools.
- **Hardlink-aware** — multiple hardlinks to one inode are collapsed to a single entry, so they aren't reported as reclaimable duplicates.
- **Symlink / special-file safe** — symlinks, FIFOs, sockets, and device files are skipped; TOCTOU windows around hashing are guarded with `O_NOFOLLOW` (POSIX) and re-checks (Windows).
- **Sensible directory pruning** — skips common noise directories like `.git`, `node_modules`, `__pycache__`, `venv`, `dist`, `.idea`, `.vscode`, and more.
- **Minimum-size filter** — `--min-size` ignores small files so trivial matches don't clutter results.
- **Colorized, readable output** — semantic color highlights wasted space (yellow) and destructive actions (red); auto-disables when output isn't a terminal, when `NO_COLOR` is set, or with `--color never`.
- **Quiet / verbose modes** — `--quiet` silences the banner and progress; `--verbose` adds elapsed time and hashing throughput.
- **Live progress** — hashing progress is reported by bytes processed (not just file count), so scans of a few very large files still show movement.
- **Script-friendly exit codes** — `0` (no duplicates), `2` (duplicates found), `1` (error), so CI and shell scripts can branch on the result.
- **Hostile-filename safe output** — non-printable characters (newlines, ANSI escapes) are escaped on *every* path that reaches the terminal — the duplicate listing, the deletion plan, the scanned-root header, and even OS error messages (which embed the offending filename) — so a crafted filename can't rewrite your terminal or spoof the deletion plan.
- **Cross-platform** — works on Windows, macOS, and Linux; reconfigures stdout to UTF-8 on Windows so Unicode paths print correctly.

## Requirements

- **Python 3.8 or newer**
- No third-party dependencies — uses only the Python standard library.

## Installation

No package install is required. Just download the script.

```bash
# Clone or copy the repository
git clone https://github.com/RyanSecEng/file-deduper.git
cd file-deduper

# (Optional) make it executable on macOS/Linux
chmod +x file-deduper.py
```

You can then run it with `python file-deduper.py ...` (or `./file-deduper.py ...` on POSIX after `chmod`).

## Example usage

```bash
# Scan a directory and print a duplicate report
python file-deduper.py ~/Downloads

# Only consider files of at least 100 KB
python file-deduper.py . --min-size 100

# Emit JSON (results on stdout, progress on stderr)
python file-deduper.py ~/Documents --json

# Save JSON to a file while progress prints to the terminal
python file-deduper.py ~/Documents --json > duplicates.json

# Silence the banner/progress, or disable color for logging
python file-deduper.py ~/Downloads --quiet --color never

# Show elapsed time and hashing throughput
python file-deduper.py ~/Pictures --verbose
```

| Argument            | Description                                                              |
| ------------------- | ------------------------------------------------------------------------ |
| `directory`         | Directory to scan recursively (required).                                |
| `--json`            | Emit results as JSON on stdout; progress messages go to stderr.          |
| `--min-size KB`     | Skip files smaller than this many KB (default: 1).                       |
| `--color {auto,always,never}` | When to colorize output (default: `auto`). Also honors `NO_COLOR`. |
| `-q`, `--quiet`     | Suppress the banner and progress output on stderr.                       |
| `-v`, `--verbose`   | Print extra detail (elapsed time and hashing throughput).               |

After a text-mode report, if you are running in an interactive terminal you'll be offered an optional cleanup step. (When stdout is piped or `--json` is used, no prompt appears.)

### Exit codes

| Code | Meaning                                  |
| ---- | ---------------------------------------- |
| `0`  | Ran successfully; no duplicates found.    |
| `2`  | Ran successfully; duplicate groups found. |
| `1`  | Error (e.g. the directory does not exist).|

This makes the tool easy to use in scripts — for example, fail a CI job when duplicates appear:

```bash
python file-deduper.py ./assets --json > dupes.json || \
  { [ $? -eq 2 ] && echo "Duplicates detected!" && exit 1; }
```

## Example output

Text report:

```
Scanned: /home/you/Downloads

Group 1: 3 copies  12.4 MB each  24.8 MB wasted
  hash: 9f86d081884c7d65...
  /home/you/Downloads/installer.dmg
  /home/you/Downloads/old/installer.dmg
  /home/you/Downloads/backup/installer (1).dmg

Group 2: 2 copies  1.2 MB each  1.2 MB wasted
  hash: e3b0c44298fc1c14...
  /home/you/Downloads/report.pdf
  /home/you/Downloads/report-copy.pdf

------------------------------------------------------------
2 group(s)  |  26.0 MB wasted in total

Delete redundant copies, keeping the oldest in each group? [y/N]:
```

JSON report (`--json`):

```json
{
  "scanned_root": "/home/you/Downloads",
  "duplicate_groups": 2,
  "total_wasted_bytes": 27262976,
  "groups": [
    {
      "hash": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "file_size_bytes": 13002342,
      "wasted_bytes": 26004684,
      "files": [
        "/home/you/Downloads/backup/installer (1).dmg",
        "/home/you/Downloads/installer.dmg",
        "/home/you/Downloads/old/installer.dmg"
      ]
    },
    {
      "hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "file_size_bytes": 1258292,
      "wasted_bytes": 1258292,
      "files": [
        "/home/you/Downloads/report-copy.pdf",
        "/home/you/Downloads/report.pdf"
      ]
    }
  ]
}
```

## How it works

1. **Walk** — `os.walk` traverses the directory tree, pruning known noise directories in place so they're never descended into. Each file is examined with a single `lstat` call; symlinks, FIFOs, sockets, and device files (anything that isn't a regular file) are skipped, as are files below `--min-size`. Hardlinks are collapsed by `(device, inode)` so duplicate links to the same data aren't counted as wasted space.
2. **Group by size** — files are bucketed by their byte size (already known from the walk, so no extra `stat` calls). Any size that appears only once cannot have a duplicate and is dropped immediately. This avoids reading the majority of files in a typical tree.
3. **Hash candidates** — only the files sharing a size with at least one other file are read and SHA-256–hashed, in 64 KB chunks. Files with matching hashes are grouped together; progress is reported on stderr.
4. **Report** — groups containing more than one file are sorted by file size (largest first) and printed as a text report or JSON, including per-group wasted bytes and a total.
5. **Optional cleanup** (text mode, interactive terminal only) — you may delete redundant copies. The oldest file in each group is kept as the canonical original. You confirm once to opt in, may exclude specific groups, then review a full deletion plan and confirm a second time. Right before each file is unlinked it is re-hashed; if it changed since the scan it is skipped rather than deleted.

## Known limitations

- **Hash-based, not byte-by-byte.** Matches rely on SHA-256. A collision is cryptographically infeasible in practice, but no separate byte comparison is performed.
- **Reads candidate files fully.** Files that share a size are hashed in their entirety; scanning very large media libraries can be I/O-bound.
- **Single-threaded.** Hashing is sequential; there is no parallelism.
- **Snapshot in time.** Detection reflects the filesystem at scan time. The deletion step guards against changes by re-hashing, but the report itself can go stale if files change after scanning.
- **"Oldest is kept" is not configurable.** Interactive cleanup always keeps the oldest copy per group; you can exclude whole groups but cannot choose a different file to keep within a group.
- **No deletion in JSON / non-interactive mode.** Cleanup only runs in text mode at an interactive terminal; it is intentionally skipped for piped/scripted use.
- **TOCTOU is reduced, not eliminated.** On Windows there is no `O_NOFOLLOW`, so a small race window remains despite the re-checks.

## Roadmap

Engine and correctness work that is planned but not yet implemented:

- Optional parallel hashing for large trees.
- Optional byte-by-byte verification of hash matches.

## UX roadmap

User-experience improvements that make the tool friendlier and more flexible (some have placeholders in the argument parser):

- `--move DEST` — move duplicate copies into a destination folder instead of deleting them.
- `--ignore PATTERN` — skip paths matching an `fnmatch` glob pattern.
- Configurable "keep" strategy via `--keep {oldest,newest,shortest-path}` (currently always keeps the oldest).
- Per-group "keep" selection in the interactive prompt (choose which copy survives, not just exclude whole groups).
- `--dry-run` — print the deletion plan without a terminal prompt, for previewing in scripts.
- `--csv` output alongside `--json`, for spreadsheet users.
- `--follow-symlinks` — opt in to following symlinks (off by default).
- `--save-report FILE` — write the text report to a file.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
