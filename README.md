# file-deduper

A single-file Python command-line tool that finds duplicate files in a directory tree by comparing their SHA-256 hashes. It is read-only unless you pass `--delete`: a normal run only reports what it finds, and even with `--delete` it removes nothing until you pass two explicit interactive confirmations through a stack of safety guards.

## Why

Duplicate files pile up over time. You download the same installer twice, copy a photo folder "just in case," or paste the same asset into a few different projects. Comparing by name or size alone doesn't catch them reliably, since different files can share a size and identical files can have different names.

`file-deduper` compares the actual file content, so two files are only reported as duplicates when their bytes match. It's also meant to be safe to point at real data: it skips symlinks, doesn't count hardlinks as wasted space, escapes hostile filenames before printing them, and re-checks each file's inode and hash right before deleting it.

## Features

- Duplicates are matched by SHA-256 content hash, not by name or size.
- Files are grouped by size first (no reads needed), and only the size collisions get hashed, so most files in a typical tree are never read.
- Read-only by default. A normal run only reports. Deleting is a separate step that you opt into, and it always keeps the oldest file in each group.
- Before any file is unlinked it is reopened without following symlinks, checked against the device and inode recorded during the scan, re-hashed, and then removed relative to its parent directory, so a file edited or swapped between the scan and the delete is skipped instead of removed.
- Skips symlinks, FIFOs, sockets, and device files, and collapses hardlinks that point at the same inode so they aren't reported as reclaimable space.
- Won't delete inside the operating system. Files under protected system directories (Windows, `/usr`, `/etc`, and the like) are never offered for deletion, system/root scans and elevated runs are refused outright with no override, and a scan reaching outside your home directory requires typing `DELETE` in full.
- Escapes non-printable characters (newlines, ANSI escapes) on everything it prints, including OS error messages that embed a filename, so a crafted name can't rewrite your terminal or fake the deletion plan.
- `--json` writes machine-readable output to stdout while progress stays on stderr, for piping into other tools.
- Returns `0` (no duplicates), `2` (duplicates found), or `1` (error) so scripts and CI can branch on the result.
- Runs on Windows, macOS, and Linux. Color output turns itself off when piped, when `NO_COLOR` is set, or with `--color never`.

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

1. **Walk.** `os.walk` traverses the tree, pruning known noise directories (`.git`, `node_modules`, `__pycache__`, `venv`, and so on) in place. Each file gets a single `lstat`; anything that isn't a regular file (symlink, FIFO, socket, device) is skipped, as are files below `--min-size`. Hardlinks are collapsed by `(device, inode)`.
2. **Group by size.** Files are bucketed by byte size, which is already known from the walk. Any size that appears only once can't have a duplicate and is dropped, which avoids reading most of the files in a typical tree.
3. **Hash candidates.** Only files that share a size are read and SHA-256 hashed, in 64 KB chunks. Matching hashes are grouped together, with progress on stderr.
4. **Report.** Groups of more than one file are sorted largest first and printed as text or JSON, with per-group and total wasted bytes.
5. **Optional cleanup** (text mode, interactive terminal only). The oldest file in each group is kept as the canonical copy. You confirm once to opt in, can exclude specific groups, then review the full deletion plan and confirm again. Right before each file is unlinked it is reopened without following symlinks and checked against the device/inode recorded during the scan and re-hashed; if either no longer matches, it's skipped. The unlink itself is done relative to the parent directory where the platform supports it, so the entry removed is the one that was checked.

## Safety in system locations

Pointed at a whole drive or a system tree, a content-based deduper will flag
thousands of files that are *meant* to exist in more than one place: shared
libraries, fonts, runtime DLLs, package caches. Deleting those can break the OS,
and an unsuspecting user could be talked into doing exactly that. The delete step
is fenced accordingly, while reporting stays completely read-only:

- **Read-only by default.** Deletion only happens with an explicit `--delete`
  flag. Without it the tool reports and exits, so a default run, however the
  command is pointed, can never remove a file. Someone coaxed into running it
  without that flag is never in danger.
- **Protected system directories are off-limits.** Any candidate resolving under
  a known system root (`C:\Windows`, `C:\Program Files`, `%ProgramData%`, `/usr`,
  `/etc`, `/bin`, `/lib`, `/System`, `/Library`, and similar) is never added to
  the deletion plan. There is no flag that relaxes this; it is the hard floor.
- **Only files you own.** A candidate not owned by the invoking user is skipped
  (POSIX uid check; on Windows the filesystem ACL already enforces this), so the
  tool won't remove another user's or a service account's files.
- **Confined to the scan root.** Every candidate is checked to resolve inside the
  directory that was scanned, both when the plan is built and again immediately
  before each unlink, so no symlink or path trick can reach outside it.
- **Capped blast radius.** No single run will delete more than `--max-deletes`
  files (default 10,000); a larger plan is refused with advice to narrow the scan.
- **System and root scans are refused.** If the scan root is a filesystem/drive
  root or sits inside a protected directory, the delete step refuses outright,
  with no override.
- **Elevated runs are refused.** Running as root or Administrator disables
  deletion entirely, because that is when a mistake does the most damage. Run the
  tool as a normal user instead.
- **Out-of-home scans demand a typed confirmation.** When the scan reaches
  outside your home directory, the final prompt requires typing `DELETE` in full
  rather than a single `y`, so it can't be muscle-memoried or coached over a call.
- **Large result sets are flagged.** An unusually high group count prints a
  warning that the scan looks system-wide before any deletion prompt.

Resolution is done with symlinks and junctions followed and case-insensitivity
honored, so a link or a case trick can't smuggle a path past these checks.

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
