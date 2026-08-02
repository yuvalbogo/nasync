# Changelog

All notable changes to this project will be documented in this file.

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
