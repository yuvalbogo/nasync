# SAMBA Sync Auto

A lightweight, cross-platform CLI tool for automatic synchronization between your local machine and a SAMBA-based home NAS server. Perfect for simple backup and file sync workflows.

## Features

- 🔄 **Bidirectional Sync**: Push local changes to server or restore from backup
- 🎯 **Simple Configuration**: Terminal-based setup with pre-defined directory pairs
- ⚡ **Manual & Automatic Modes**: Sync on demand or auto-watch for file changes
- 🖥️ **Cross-Platform**: Works on Linux (Fedora, Ubuntu, etc.) and macOS
- 🔐 **Secure**: Credentials stored locally with restricted permissions
- 📦 **Lightweight**: Minimal dependencies, low memory footprint

## Installation

### From Source

```bash
git clone https://github.com/yourusername/ServerSyncAuto.git
cd ServerSyncAuto
pip install -e .
```

### Via pip (when published)

```bash
pip install samba-sync-auto
```

## Quick Start

### 1. Initialize Server Configuration

```bash
samba-sync init
```

You'll be prompted for:
- Server IP address (default: `10.100.102.200`)
- SAMBA username
- SAMBA password
- Share name (default: `backup`)

### 2. Add Sync Pairs

Add directories to sync. Local and server paths can have different names:

```bash
samba-sync add ~/Documents /backups/docs
samba-sync add ~/Downloads /backups/downloads
samba-sync add ~/Projects /server/work
```

### 3. List Your Configuration

```bash
samba-sync list
```

### 4. Sync

**Push to server** (manual):
```bash
samba-sync sync
samba-sync sync --pair Documents  # specific pair
```

**Restore from server** (manual):
```bash
samba-sync restore
samba-sync restore --pair Documents
```

**Auto-sync on file changes** (daemon):
```bash
samba-sync watch
samba-sync watch --pair Documents  # watch single pair
```

### 5. Manage Pairs

Remove a sync pair:
```bash
samba-sync remove Documents
```

## Configuration

Configuration is stored in:
- **Linux/macOS**: `~/.config/samba-sync/config.json`
- **Credentials**: `~/.config/samba-sync/.creds` (mode 0600)

Example config structure:
```json
{
  "server": {
    "ip": "10.100.102.200",
    "username": "backup_user",
    "share": "backup"
  },
  "pairs": [
    {
      "name": "Documents",
      "local": "/home/user/Documents",
      "server": "/backups/docs"
    }
  ]
}
```

## Commands Reference

| Command | Description |
|---------|-------------|
| `samba-sync init` | Initialize server configuration |
| `samba-sync add <local> <server>` | Add a sync pair |
| `samba-sync list` | Show all sync pairs |
| `samba-sync remove <name>` | Remove a sync pair |
| `samba-sync sync [--pair NAME]` | Manual sync to server |
| `samba-sync restore [--pair NAME]` | Restore from server |
| `samba-sync watch [--pair NAME]` | Auto-sync on file changes |

## How It Works

- **Push (sync)**: Uploads all files from local directories to server
- **Pull (restore)**: Downloads all files from server to local directories
- **Watch**: Monitors local directories with inotify (Linux) / FSEvents (macOS); syncs when files change
- **Local is source of truth**: Server is always overwritten during sync

## Requirements

- Python 3.8+
- SAMBA share accessible on your network
- Read/write permissions on SAMBA share

## Contributing

Contributions welcome! Please feel free to submit issues or pull requests.

## License

MIT License

## Acknowledgments

Built with:
- [Click](https://click.palletsprojects.com/) - CLI framework
- [smb-protocol](https://github.com/jborean93/smbprotocol) - SAMBA client
- [watchdog](https://github.com/gorakhargosh/watchdog) - File system monitoring
- [appdirs](https://github.com/ActiveState/appdirs) - Platform-specific paths
