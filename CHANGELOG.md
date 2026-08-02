# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added
- **Saved, named servers.** Configure multiple servers, each with a name, and
  switch the active one instead of re-entering IP/user/share every time. New
  CLI commands `servers` (list) and `use <name>` (switch active); `init`/`setup`
  now name the server. Existing single-server configs migrate automatically.

### Changed
- **TUI redesign around saved servers + native progress.** The server section
  is now a compact list of saved servers — pick one to activate; the connection
  fields live in an Add/Edit modal (with a Test button). This frees the left
  column for a larger **Sync pairs** area. Transfers show a dedicated
  **Progress** panel: a native progress bar with percentage + ETA, a live
  `X / N files` object counter, and elapsed time. Sync pairs can now be
  **edited** in place (Edit button / `update_pair`), not just added/removed.
- **Project layout:** renamed the Python package `samba_sync` → `nasync` (so
  the package, command, distribution, and config dir all share one name).
  Removed redundant `setup.py` and `requirements.txt` — packaging metadata now
  lives solely in `pyproject.toml`.
- **TUI: live per-file progress.** Push/pull now stream `smbclient` output and
  emit a per-file progress callback, so the TUI shows a dnf-style progress bar
  with an `X/total` counter and the current file name.
- **TUI: refined layout.** Section titles are embedded in rounded borders with
  consistent padding/spacing, and the **Actions** panel moved to the right
  column, directly above the **Log**.

### Added
- **Desktop launcher** (`nasync install-desktop`): installs a freedesktop
  `.desktop` entry and an app icon under `~/.local/share` so nasync shows up in
  the application menu and opens the TUI in a terminal. `--uninstall` removes
  it. Linux only. Ships an SVG app icon in the package.
- **Interactive terminal UI** (`nasync tui`, install with `nasync[tui]`): a
  Textual app with a server panel + Test-connection button, auto-populated
  share picker, a sync-pairs table, and Sync/Dry-run/Restore actions that
  stream progress into a log pane. Runs the same engine as the CLI.

### Changed
- **Internal:** `SAMBAClient`/`Syncer` now take an injectable `reporter`
  callback (defaults to `print`) and `Syncer` returns a `{"ok", "failed"}`
  summary instead of raising `SystemExit`. This lets non-CLI front-ends (the
  TUI) drive the same code and capture progress; CLI behaviour is unchanged.
- **Renamed the command from `samba-sync` to `nasync`** (shorter to type). The
  distribution and config directory are now `nasync` too (`~/.config/nasync/`).

### Added
- `setup` command: a guided first-run wizard that collects server details with
  a live connection test, auto-detects and lets you pick a share, and adds sync
  pairs — reusing the same validation as `test-connection`.
- `test-connection` command: verifies the server is reachable, credentials are
  accepted, and the configured share exists — and lists available shares when
  the configured one is wrong.
- Configurable SMB port (prompted during `init`, default 445).
- `restore --yes/-y` to skip the confirmation prompt (for automation).

### Fixed
- **Live progress froze after the first file** on multi-file uploads:
  `smbclient` block-buffers its output through a pipe, so per-file lines arrived
  in a lump. It now runs against a pseudo-terminal (line-buffered) with the
  command script fed on a writer thread, so progress streams smoothly.
- **Performance**: sync/restore now run in a single `smbclient` session per
  directory (native recursion) instead of opening a new SMB connection for
  every file — dramatically faster on large trees.
- **Auth**: pass the password via `PASSWD` (the variable `smbclient` actually
  reads); the previous `SMBCLIENT_PASSWORD` was never picked up, which could
  stall non-interactive syncs.
- **Nested directories**: parent directories are now created in order, so
  deeply nested trees push correctly.
- **Restore reliability**: replaced fragile `ls` output parsing (which
  misdetected any file modified in December as a directory) with `mget`.
- **Watch**: switched to a trailing-edge debounce so the final state after a
  burst of edits is always synced (previously trailing changes could be lost).
- `--dry-run` now actually previews instead of performing the transfer, and a
  push dry-run works without contacting the server.
- Duplicate sync-pair names are rejected; blank-password (guest) shares work.

## [0.1.0] - 2026-08-02

### Added
- Initial release of SAMBA Sync Auto
- `init` command for server configuration setup
- `add` command to create sync pairs with flexible naming
- `list` command to view all configured sync pairs
- `remove` command to delete sync pairs
- `sync` command for manual push (local → server)
- `restore` command for manual pull (server → local)
- `watch` command for automatic file watching and syncing
- `status` command to check configuration and sync pairs
- Support for `SAMBA_PASSWORD` environment variable for scripting
- Cross-platform support (Linux/macOS)
- Secure credential storage with restricted file permissions
- Comprehensive error handling and user-friendly messages
- File system monitoring via watchdog

### Features
- Local machine as source of truth
- Recursive directory synchronization
- Automatic platform-specific configuration paths
- systemd service example for daemon mode
- Support for cron-based backup scheduling
- Detailed logging of sync operations

## Future Roadmap
- **macOS `.app` bundle**: a `nasync install-desktop` equivalent for macOS that
  generates `~/Applications/nasync.app` (Info.plist + `.icns` icon assembled
  from the SVG via `iconutil` + a launcher that opens the TUI in Terminal), so
  Mac users get a clickable app too. Deferred until it can be verified on real
  macOS hardware (Gatekeeper behaviour and the Terminal launch need testing).
- Delta/incremental sync mode (only transfer changed files)
- Conflict resolution strategies
- Performance optimizations for large directories
