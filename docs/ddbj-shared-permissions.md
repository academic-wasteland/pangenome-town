# DDBJ shared home permissions — 16 September 2026

At the operator’s explicit request, the final sweep covered the entire
`/lustre10/home/leechuck` tree, including hidden directories, configuration,
software environments, backups, and `.ssh`.

- 25,747 directories and 191,070 regular files examined.
- Directories set to 0777; regular files set to `0666 | (previous_mode & 0111)`.
- Existing execute bits preserved exactly on 7,178 files.
- 4,033 entries changed; other entries already had the requested permissions.
- Each changed mode immediately verified with stat.
- 7,686 symlinks left untouched and not followed; no special files encountered.
- Two permission errors: `hla/.claude` (0755) and
  `hla/.claude/settings.local.json` (0644). Both belong to `dawnxchen`;
  `leechuck` cannot chmod them and passwordless sudo is unavailable.
  Their owner or an administrator must change these two entries to finish.

The home root and `.ssh` are now 0777. This explicitly supersedes the earlier
research-only sweep and its SSH/configuration exclusions. World-writable SSH
paths may cause SSH StrictModes to reject key authentication.

`umask 000` was prepended to `.bashrc` (before its noninteractive early return)
and `.profile`. Before the final sweep, fresh SSH sessions on both the DDBJ
gateway and nested `a001` reported 0000. A creation test produced a 0777 directory
and 0666 ordinary file. Programs explicitly requesting private modes can still
create private files; umask cannot grant permissions absent from the requested
creation mode.

Shell-file backups and the earlier research-only sweep log are at:
`/lustre10/home/leechuck/.config/wasteland-permission-backups/20260916T105704/`.
These backups are now included in the shared permissions as well.
