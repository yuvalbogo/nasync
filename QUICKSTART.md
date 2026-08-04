# Quick Start Guide

Get NASync running in 5 minutes.

## Installation

### 1. Install system dependencies

**Ubuntu/Debian:**
```bash
sudo apt update
sudo apt install python3-pip smbclient
```

**Fedora/RHEL:**
```bash
sudo dnf install python3-pip samba-client
```

**macOS:**
```bash
brew install python samba
```

### 2. Install NASync

```bash
pip install git+https://github.com/YuvalBogo/nasync.git
```

Or from source:
```bash
git clone https://github.com/YuvalBogo/nasync.git
cd nasync
pip install -e .
```

## Configuration

### Fastest: one guided command

```bash
nasync setup
```

This walks you through the server details (testing the connection as you go),
lets you pick your share from an auto-detected list, and adds your sync
folders — then you can jump straight to [Usage](#usage). The manual steps
below are the same thing, broken out.

### 1. Initialize your server

```bash
nasync init
```

You'll be prompted for:
- Server IP (e.g., `192.168.1.100`)
- SAMBA username
- SAMBA password
- Share name (e.g., `backup`)

### 2. Add directories to sync

```bash
nasync add ~/Documents /docs
nasync add ~/Downloads /downloads
nasync add ~/Projects /code
```

> The second argument is relative to the **share root** — don't repeat the
> share name. See [Understanding server paths](README.md#understanding-server-paths).

### 3. View your configuration

```bash
nasync status
```

Should output:
```
📊 SAMBA Sync Status
  Server: 192.168.1.100:backup
  User: yourname
  Pairs: 3

  Configured pairs:
    • Documents: /home/user/Documents → /docs
    • Downloads: /home/user/Downloads → /downloads
    • Projects: /home/user/Projects → /code
```

## Usage

### Manual Sync

Sync everything once:
```bash
nasync sync
```

Sync only one directory:
```bash
nasync sync --pair Documents
```

### Automatic Watching

Watch and auto-sync on changes:
```bash
nasync watch
```

To watch just one pair:
```bash
nasync watch --pair Documents
```

### Restore from Backup

Restore everything from server:
```bash
nasync restore
```

Restore one directory:
```bash
nasync restore --pair Documents
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
0 2 * * * export SAMBA_PASSWORD=$(cat ~/.samba-password) && /usr/local/bin/nasync sync
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
- Run `nasync init` again to update credentials

### Credentials not working after init
Try using environment variable:
```bash
export SAMBA_PASSWORD="your_password"
nasync sync
```

## Next Steps

- Read the full [README.md](README.md) for advanced options
- Set up [systemd service](README.md#watch-and-sync-in-background) for daemon mode
- Configure multiple sync profiles for different use cases
- Check the [CHANGELOG.md](CHANGELOG.md) for what's new

## Support

Found an issue? 
- Check [existing issues](https://github.com/YuvalBogo/nasync/issues)
- [Create a new issue](https://github.com/YuvalBogo/nasync/issues/new?template=bug_report.md)

Want to contribute?
- See [CONTRIBUTING.md](.github/CONTRIBUTING.md)
