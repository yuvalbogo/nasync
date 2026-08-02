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

**⏱️ 5-minute setup guide:** See [QUICKSTART.md](QUICKSTART.md)

### 1. Initialize Server Configuration

```bash
samba-sync init
```

You'll be prompted for:
- Server IP address (e.g. `192.168.1.100`)
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
    "ip": "192.168.1.100",
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

### Using Environment Variables

For scripted/automated usage, set `SAMBA_PASSWORD`:
```bash
export SAMBA_PASSWORD="your_password"
samba-sync sync
```

This allows you to create cron jobs or scripts without hardcoding passwords:
```bash
#!/bin/bash
export SAMBA_PASSWORD=$(cat /path/to/secret/file)
samba-sync sync
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
- `samba-client` package (for smbclient tool)
  - **Ubuntu/Debian**: `sudo apt install smbclient`
  - **Fedora/RHEL**: `sudo dnf install samba-client`
  - **macOS**: `brew install samba`
- SAMBA share accessible on your network
- Read/write permissions on SAMBA share

## Contributing

Contributions welcome! Please feel free to submit issues or pull requests.

## License

MIT License

## Common Use Cases

### Daily Backup via Cron

Schedule automatic daily sync at 2 AM:

```bash
0 2 * * * export SAMBA_PASSWORD=$(cat ~/.samba-pass) && samba-sync sync
```

### Watch and Sync in Background

Run as daemon to sync on file changes:

```bash
samba-sync watch &
```

Or in systemd user service (`~/.config/systemd/user/samba-sync.service`):

```ini
[Unit]
Description=SAMBA Sync Auto Watcher
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/samba-sync watch
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start:
```bash
systemctl --user enable samba-sync
systemctl --user start samba-sync
```

### Multi-Directory Sync

Sync different directories on different schedules:

```bash
# Sync everything at once
samba-sync sync

# Sync only Documents (faster for quick backups)
samba-sync sync --pair Documents

# Restore only Downloads if needed
samba-sync restore --pair Downloads
```

## Acknowledgments

Built with:
- [Click](https://click.palletsprojects.com/) - CLI framework
- [watchdog](https://github.com/gorakhargosh/watchdog) - File system monitoring
- [appdirs](https://github.com/ActiveState/appdirs) - Platform-specific paths
