# file-deduper

A single-file Python command-line tool that finds duplicate files in a directory tree by comparing their SHA-256 hashes. It is read-only unless you pass `--delete`: a normal run only reports what it finds, and even with `--delete` it removes nothing until you pass two explicit interactive confirmations through a stack of safety guards.

## Why

Duplicate files pile up over time. You download the same installer twice, copy a photo folder "just in case," or paste the same asset into a few different projects. Comparing by name or size alone doesn't catch them reliably, since different files can share a size and identical files can have different names.

`file-deduper` compares the actual file content, so two files are only reported as duplicates when their bytes match. It's also meant to be safe to point at real data: it skips symlinks, doesn't count hardlinks as wasted space, escapes hostile filenames before printing them, and re-checks each file's inode and hash right before deleting it.

## Features

- **Content-based.** Matches by SHA-256 hash, not by name or size.
- **Fast.** Groups by size first and only hashes the size collisions, so most files in a typical tree are never read.
- **Read-only by default.** A normal run only reports; deletion is opt-in via `--delete` and always keeps the oldest copy in each group.
- **Safe to delete.** Re-checks each file's inode and hash right before unlinking, and never touches system files, files you don't own, or anything outside the scan (see [Safety](#safety-in-system-locations)).
- **Robust.** Skips symlinks, FIFOs, sockets, and devices; collapses hardlinks so they aren't counted as wasted space; escapes control and ANSI characters in every path and error it prints.
- **Scriptable.** `--json` on stdout with progress on stderr; exit codes `0`/`2`/`1` for no-duplicates/duplicates/error.
- **Cross-platform.** Windows, macOS, and Linux; color auto-disables when piped, when `NO_COLOR` is set, or with `--color never`.

## Requirements

- Python 3.8 or newer
- No third-party dependencies; standard library only.

## Installation

There's nothing to install. Download the script, or clone the repo:

```bash
git clone https://github.com/RyanSecEng/file-deduper.git
cd file-deduper

# (Optional) make it executable on macOS/Linux
chmod +x file-deduper.py
```

Then run `python file-deduper.py ...` (or `./file-deduper.py ...` on POSIX after `chmod`).

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

# Opt in to the guarded interactive cleanup (read-only without --delete)
python file-deduper.py ~/Downloads --delete
```

| Argument            | Description                                                              |
| ------------------- | ------------------------------------------------------------------------ |
| `directory`         | Directory to scan recursively (required).                                |
| `--json`            | Emit results as JSON on stdout; progress messages go to stderr.          |
| `--min-size KB`     | Skip files smaller than this many KB (default: 1).                       |
| `--color {auto,always,never}` | When to colorize output (default: `auto`). Also honors `NO_COLOR`. |
| `--delete`          | Enable the interactive deletion step. Off by default; without it the tool only reports. |
| `--max-deletes N`   | Refuse to delete more than N files in one run (default: 10000).          |
| `-q`, `--quiet`     | Suppress the banner and progress output on stderr.                       |
| `-v`, `--verbose`   | Print extra detail (elapsed time and throughput).                        |

A normal run is read-only and never prompts. With `--delete`, and only at an interactive terminal, you're offered the guarded cleanup step; when stdout is piped or `--json` is used, no prompt appears.

### Exit codes

| Code | Meaning                                  |
| ---- | ---------------------------------------- |
| `0`  | Ran successfully; no duplicates found.    |
| `2`  | Ran successfully; duplicate groups found. |
| `1`  | Error (e.g. the directory does not exist).|

For example, to fail a CI job when duplicates appear:

```bash
python file-deduper.py ./assets --json > dupes.json || \
  { [ $? -eq 2 ] && echo "Duplicates detected!" && exit 1; }
```

## Example output

Text report:

```
Scanned: /home/you/Downloads

-- Group 1 ---------------------------------------------------
  3 copies | 12.4 MB each | 24.8 MB wasted
  sha256: 9f86d081884c7d65...
    /home/you/Downloads/installer.dmg
    /home/you/Downloads/old/installer.dmg
    /home/you/Downloads/backup/installer (1).dmg

-- Group 2 ---------------------------------------------------
  2 copies | 1.2 MB each | 1.2 MB wasted
  sha256: e3b0c44298fc1c14...
    /home/you/Downloads/report.pdf
    /home/you/Downloads/report-copy.pdf

--------------------------------------------------------------
2 group(s) | 26.0 MB wasted in total
```

With `--delete`, the report is followed by the guarded confirmation prompts.

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

1. **Walk.** `os.walk` traverses the tree, pruning known noise directories (`.git`, `node_modules`, `__pycache__`, `venv`, and so on) in place. Each file gets a single `lstat`; anything that isn't a regular file (symlink, FIFO, socket, device) is skipped, as are files below `--min-size`. Hardlinks are collapsed by `(device, inode)`.
2. **Group by size.** Files are bucketed by byte size, which is already known from the walk. Any size that appears only once can't have a duplicate and is dropped, which avoids reading most of the files in a typical tree.
3. **Hash candidates.** Only files that share a size are read and SHA-256 hashed, in 64 KB chunks. Matching hashes are grouped together, with progress on stderr.
4. **Report.** Groups of more than one file are sorted largest first and printed as text or JSON, with per-group and total wasted bytes.
5. **Optional cleanup** (`--delete`, text mode, interactive terminal only). The oldest file in each group is kept. You opt in, can exclude specific groups, then review the full deletion plan and confirm again. The deletion itself is heavily guarded; see [Safety](#safety-in-system-locations).

## Safety in system locations

Pointed at a whole drive or a system tree, a content-based deduper will flag
thousands of files that are *meant* to exist in more than one place: shared
libraries, fonts, runtime DLLs, package caches. Deleting those can break the OS,
and an unsuspecting user could be talked into doing exactly that. The delete step
is fenced accordingly, while reporting stays completely read-only:

- **Read-only by default.** Deletion only happens with an explicit `--delete`
  flag. Without it the tool reports and exits, so a default run can never remove a
  file, and someone coaxed into running it without the flag is never in danger.
- **System files and locations are off-limits.** Files under known system roots
  (`C:\Windows`, `C:\Program Files`, `%ProgramData%`, `/usr`, `/etc`, `/bin`,
  `/lib`, `/System`, `/Library`, and similar) are never deletable; scanning a
  drive/system root, or running as root/Administrator, is refused outright. There
  is no override.
- **Only your files, only inside the scan.** A candidate is skipped unless you own
  it (POSIX uid check; Windows ACLs already enforce this) and it resolves within
  the scanned directory, re-checked immediately before each unlink. Paths are
  resolved with symlinks and junctions followed, so a link or case trick can't
  smuggle one past these checks.
- **Extra friction for risky scope.** A scan outside your home directory requires
  typing `DELETE` in full rather than a single `y`, and an unusually large result
  set is flagged as a likely system-wide scan before any prompt.
- **Capped blast radius.** No single run will delete more than `--max-deletes`
  files (default 10,000); a larger plan is refused with advice to narrow the scan.

## Testing

The test suite uses only the standard library (`unittest`):

```bash
python -m unittest discover -s tests
```

It covers the size and hash grouping, hardlink and symlink handling, the filename escaping, and the deletion path that matters most: keep-oldest, group exclusion, and the inode and re-hash guards before delete.

## Known limitations

- Matches rely on SHA-256, not a byte-by-byte comparison. A collision is cryptographically infeasible in practice, but it's worth knowing no separate byte check is done.
- Files that share a size are hashed in full, so scanning very large media libraries can be I/O-bound.
- Hashing is single-threaded; there's no parallelism.
- Interactive cleanup always keeps the oldest copy in a group. You can exclude whole groups but can't pick a different file to keep within one.
- Cleanup only runs in text mode at an interactive terminal. It's intentionally skipped for piped or scripted use, so `--json` never deletes anything.
- TOCTOU is reduced, not eliminated. The inode check, re-hash, and directory-relative unlink narrow the window between the scan and the delete, but Windows has no `O_NOFOLLOW` and no portable unlink-by-descriptor exists, so a small race remains.

## License

MIT. See the [LICENSE](LICENSE) file for details.
