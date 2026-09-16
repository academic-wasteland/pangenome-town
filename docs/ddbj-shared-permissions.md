# DDBJ shared research permissions — 16 September 2026

Applied the operator's shared-access request to `/lustre10/home/leechuck/hla` and
`/lustre10/home/leechuck/wasteland`:

- 24,433 directories processed with mode 0777.
- 188,313 files processed; ordinary files mode 0666, 7,140 existing executables
  retain execute bits (0777).
- 7,718 entries skipped by the file walker (hidden files, symlinks or non-owned
  entries); hidden directories are not traversed. No permission errors occurred.
- A deterministic sample of 100 modified paths had zero mode mismatches.

The home root is 0755 and `.ssh` remains 0700 so SSH StrictModes continues to accept
key authentication. Dotfile/configuration trees and software environments outside
`hla`/`wasteland` were not recursively changed. This is deliberately **not** a claim
that every home-directory entry is world writable.

`umask 000` was prepended to `.bashrc` (before its noninteractive early return) and
`.profile`. Fresh SSH sessions on both the DDBJ gateway and nested `a001` report
0000. A creation test produced a 0777 directory and 0666 ordinary file. Programs
that explicitly request private file modes can still create private files.

Shell-file backups and the permission-change log for the scripted sweep are at:
`/lustre10/home/leechuck/.config/wasteland-permission-backups/20260916T105704/`.
The backup directory is private. Symlinks were not followed.
