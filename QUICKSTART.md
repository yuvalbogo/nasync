# Quick Start Guide

Get SAMBA Sync Auto running in 5 minutes.

## Installation

### 1. Install system dependencies

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install python3-pip samba-client
```

**Fedora/RHEL:**
```bash
sudo dnf install python3-pip samba-client
```

**macOS:**
```bash
brew install python samba
```

### 2. Install SAMBA Sync Auto

```bash
pip install git+https://github.com/yourusername/ServerSyncAuto.git
```

Or from source:
```bash
git clone https://github.com/yourusername/ServerSyncAuto.git
cd ServerSyncAuto
pip install -e .
```

## Configuration

### 1. Initialize your server

```bash
samba-sync init
```

You'll be prompted for:
- Server IP (e.g., `10.100.102.200`)
- SAMBA username
- SAMBA password
- Share name (e.g., `backup`)

### 2. Add directories to sync

```bash
samba-sync add ~/Documents /server/docs
samba-sync add ~/Downloads /server/downloads
samba-sync add ~/Projects /server/code
```

### 3. View your configuration

```bash
samba-sync status
```

Should output:
```
📊 SAMBA Sync Status
  Server: 10.100.102.200:backup
  User: yourname
  Pairs: 3

  Configured pairs:
    • Documents: /home/user/Documents → /server/docs
    • Downloads: /home/user/Downloads → /server/downloads
    • Projects: /home/user/Projects → /server/code
```

## Usage

### Manual Sync

Sync everything once:
```bash
samba-sync sync
```

Sync only one directory:
```bash
samba-sync sync --pair Documents
```

### Automatic Watching

Watch and auto-sync on changes:
```bash
samba-sync watch
```

To watch just one pair:
```bash
samba-sync watch --pair Documents
```

### Restore from Backup

Restore everything from server:
```bash
samba-sync restore
```

Restore one directory:
```bash
samba-sync restore --pair Documents
```

### Schedule with Cron

Daily backup at 2 AM:

```bash
# Save password to secure file
echo "your_password" > ~/.samba-password
chmod 600 ~/.samba-password

# Add to crontab
crontab -e
```

Add this line:
```bash
0 2 * * * export SAMBA_PASSWORD=$(cat ~/.samba-password) && /usr/local/bin/samba-sync sync
```

## Troubleshooting

### "smbclient not found"
Install samba-client for your system (see Installation step 1)

### "Connection refused"
- Check server IP is correct
- Verify server is online
- Check firewall allows SMB (port 445)

### "Authentication failed"
- Verify username and password
- Run `samba-sync init` again to update credentials

### Credentials not working after init
Try using environment variable:
```bash
export SAMBA_PASSWORD="your_password"
samba-sync sync
```

## Next Steps

- Read the full [README.md](README.md) for advanced options
- Set up [systemd service](README.md#watch-and-sync-in-background) for daemon mode
- Configure multiple sync profiles for different use cases
- Check the [CHANGELOG.md](CHANGELOG.md) for what's new

## Support

Found an issue? 
- Check [existing issues](https://github.com/yourusername/ServerSyncAuto/issues)
- [Create a new issue](https://github.com/yourusername/ServerSyncAuto/issues/new?template=bug_report.md)

Want to contribute?
- See [CONTRIBUTING.md](.github/CONTRIBUTING.md)
