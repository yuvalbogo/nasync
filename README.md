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
pip install nasync
```

## Quick Start

**⏱️ 5-minute setup guide:** See [QUICKSTART.md](QUICKSTART.md)

### 1. Initialize Server Configuration

```bash
nasync init
```

You'll be prompted for:
- Server IP address (e.g. `192.168.1.100`)
- SAMBA username
- SAMBA password
- Share name (default: `backup`)
- SMB port (default: `445` — press Enter unless your server uses a custom port)

Then confirm it all works before going further:

```bash
nasync test-connection
```

This checks the server is reachable, your credentials are accepted, and the
share exists. If the share name is wrong, it lists the shares that *are*
available so you can fix it.

### 2. Add Sync Pairs

Add directories to sync. Local and server paths can have different names:

```bash
nasync add ~/Documents /docs
nasync add ~/Downloads /downloads
nasync add ~/Projects /work
```

> **The server path is relative to the share root — do _not_ repeat the share
> name in it.** If your share is `backup` and you pass `/docs`, files land in
> `backup/docs` on the server. Passing `/backup/docs` would create a *nested*
> `backup/backup/docs`.

### 3. List Your Configuration

```bash
nasync list
```

### 4. Sync

**Push to server** (manual):
```bash
nasync sync
nasync sync --pair Documents  # specific pair
```

**Restore from server** (manual):
```bash
nasync restore
nasync restore --pair Documents
```

**Auto-sync on file changes** (daemon):
```bash
nasync watch
nasync watch --pair Documents  # watch single pair
```

### 5. Manage Pairs

Remove a sync pair:
```bash
nasync remove Documents
```

## Configuration

Configuration is stored in:
- **Linux/macOS**: `~/.config/nasync/config.json`
- **Credentials**: `~/.config/nasync/.creds` (mode 0600)

Example config structure:
```json
{
  "server": {
    "ip": "192.168.1.100",
    "username": "backup_user",
    "share": "backup",
    "port": 445
  },
  "pairs": [
    {
      "name": "Documents",
      "local": "/home/user/Documents",
      "server": "/docs"
    }
  ]
}
```

### Understanding server paths

A pair's `server` path is **relative to the root of the SMB share**, not an
absolute path on the server's filesystem. Think of the share as the top level.

With share `backup` mounted at, say, `/srv/samba/backup` on the server:

| `share`  | pair `server` path | Ends up on the server as        |
|----------|--------------------|---------------------------------|
| `backup` | `/docs`            | `backup/docs` → `/srv/samba/backup/docs`  |
| `backup` | `/photos/2024`     | `backup/photos/2024`            |
| `backup` | `/backup/docs` ⚠️   | `backup/backup/docs` (double-nested — usually a mistake) |

So the share name comes from `nasync init`; the `server` path in each pair
is just where *inside* that share the files should go.

### Using Environment Variables

For scripted/automated usage, set `SAMBA_PASSWORD`:
```bash
export SAMBA_PASSWORD="your_password"
nasync sync
```

This allows you to create cron jobs or scripts without hardcoding passwords:
```bash
#!/bin/bash
export SAMBA_PASSWORD=$(cat /path/to/secret/file)
nasync sync
```

## Commands Reference

| Command | Description |
|---------|-------------|
| `nasync init` | Initialize server configuration |
| `nasync test-connection` | Verify server, credentials, and share access |
| `nasync add <local> <server>` | Add a sync pair |
| `nasync list` | Show all sync pairs |
| `nasync remove <name>` | Remove a sync pair |
| `nasync sync [--pair NAME]` | Manual sync to server |
| `nasync restore [--pair NAME]` | Restore from server |
| `nasync watch [--pair NAME]` | Auto-sync on file changes |

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

## Testing

No NAS needed — `scripts/local-test.sh` spins up a throwaway Samba server in a
container (podman or docker), runs a full push → verify → restore → dry-run
cycle against it in an isolated config, and tears everything down afterwards:

```bash
pip install -e .
./scripts/local-test.sh          # full end-to-end test
KEEP=1 ./scripts/local-test.sh   # leave the server/config up to poke at
```

Pure-logic unit tests (no server required):

```bash
python -m samba_sync.test_config
python -m samba_sync.test_samba
```

## Contributing

Contributions welcome! Please feel free to submit issues or pull requests.

## License

MIT License

## Common Use Cases

### Daily Backup via Cron

Schedule automatic daily sync at 2 AM:

```bash
0 2 * * * export SAMBA_PASSWORD=$(cat ~/.samba-pass) && nasync sync
```

### Watch and Sync in Background

Run as daemon to sync on file changes:

```bash
nasync watch &
```

Or in systemd user service (`~/.config/systemd/user/nasync.service`):

```ini
[Unit]
Description=SAMBA Sync Auto Watcher
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/nasync watch
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Enable and start:
```bash
systemctl --user enable nasync
systemctl --user start nasync
```

### Multi-Directory Sync

Sync different directories on different schedules:

```bash
# Sync everything at once
nasync sync

# Sync only Documents (faster for quick backups)
nasync sync --pair Documents

# Restore only Downloads if needed
nasync restore --pair Downloads
```

## Acknowledgments

Built with:
- [Click](https://click.palletsprojects.com/) - CLI framework
- [watchdog](https://github.com/gorakhargosh/watchdog) - File system monitoring
- [appdirs](https://github.com/ActiveState/appdirs) - Platform-specific paths
