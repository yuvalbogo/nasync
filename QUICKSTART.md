# Quick Start Guide

Get NASync running in 5 minutes.

## Installation

### 1. Install NASync

NASync speaks SMB directly from Python, so there's no Samba tooling to install
first — just Python 3.8+ and pip.

```bash
pip install git+https://github.com/YuvalBogo/nasync.git
```

Or from source:
```bash
git clone https://github.com/YuvalBogo/nasync.git
cd nasync
pip install -e .
```

### 2. Optional: the desktop app

The CLI works on its own. For a graphical interface:

**Desktop app** (`nasync gui`) — needs GTK4 + libadwaita 1.5+ from your
distribution. Fedora 40+ and Ubuntu 24.04 LTS+ both qualify:

```bash
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
pipx install --system-site-packages nasync

# Ubuntu / Debian
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 pipx
pipx install --system-site-packages nasync
```

Then put it in your application menu:

```bash
nasync install-desktop
```

> `--system-site-packages` matters: GTK can't come from pip, so it lives in the
> system Python. Without the flag the isolated install can't see it. Same for a
> manual venv: `python -m venv --system-site-packages .venv`.

> On Ubuntu 22.04 (libadwaita 1.1) the desktop app can't run. Use the CLI, or
> the Flatpak, which carries its own runtime.

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

Pairs belong to the active server, so each NAS keeps its own list. Add to a
different one with `--server NAME`, and see them all with `nasync list --all`.

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

### Sync mode: mirror or update

A sync always uploads new and changed files. What differs is files you deleted
locally, which are still on the server:

```bash
nasync mode              # show the current mode
nasync mode mirror       # default — the server matches your folders exactly,
                         #   so files deleted locally are deleted there too
nasync mode update       # only ever adds and updates; nothing is deleted
```

In the desktop app: menu (⋯) → **Settings**. Restoring never deletes local
files, in either mode. Not sure what a sync would do? `nasync sync --dry-run`
lists it without transferring anything.

### Automatic syncing

Both are off until you turn them on, and both install a background service so
they keep working with NASync closed:

```bash
nasync live-sync on                  # sync moments after a file changes
nasync scheduled-sync on --at 02:00  # and/or once a day at 02:00
nasync service status                # is it running?
```

Live sync only runs while you're on the same network as the server, so a laptop
away from home waits and syncs on the way back in. A scheduled run missed
because the machine was asleep happens at the next opportunity that day.

On a pip or pipx install that background service is a systemd user service. On
a Flatpak it's your desktop's Background portal instead, so the first time you
turn either trigger on you'll be asked once for permission to keep syncing with
NASync closed — say yes, or automatic syncing will only run while the window is
open. `nasync service status` tells you which one is in play.

You'll get a desktop notification when a background sync runs, and — because
this is the one worth catching — when a scheduled one couldn't run at all:

```bash
nasync notifications --test   # check your desktop shows them
nasync notifications off      # if you'd rather it stayed quiet
```

In the desktop app: the **Automatic** button at the bottom of the main window
shows the current state and opens the same settings.

To watch in the foreground instead, without changing any settings:
```bash
nasync watch
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

### "The desktop app needs PyGObject with GTK4 and libadwaita"
Install them from your distribution (see Installation step 2). If nasync lives
in a virtualenv, that venv also has to be created with
`--system-site-packages` to see them. The CLI needs none of this and works
regardless.

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
