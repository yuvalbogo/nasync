# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
  distribution and config directory are now `nasync` too
  (`~/.config/nasync/`). The internal Python package remains `samba_sync`.

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
- Delta/incremental sync mode
- Dry-run capability
- Conflict resolution strategies
- GUI implementation
- Performance optimizations for large directories
