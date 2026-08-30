# Notifications Setup Guide

NASync notifications work automatically on most desktop Linux systems. This guide covers setup on different platforms and how to verify they're working.

## Quick Start

Notifications are **on by default**. To verify they're working:

```bash
nasync notifications --test
```

If you see `📨 Sent a test notification via gdbus` (or `notify-send`), you're done!

---

## Platform-Specific Setup

### GNOME Desktop / KDE Plasma / Budgie / Cinnamon
**Status**: ✅ Works out-of-the-box

These desktops ship with gdbus and a notification daemon. Just run:
```bash
nasync notifications --test
```

No additional setup needed.

### Ubuntu / Debian
**Status**: ✅ Works out-of-the-box

gdbus comes with glib2 (installed by default). Verify:
```bash
which gdbus
# Should print: /usr/bin/gdbus
```

### Fedora / RHEL / CentOS
**Status**: ✅ Works out-of-the-box

gdbus comes with glib2. Verify:
```bash
which gdbus
```

### Arch Linux / Manjaro
**Status**: ✅ Works out-of-the-box

gdbus comes with glib2. If missing (rare):
```bash
sudo pacman -S glib2
```

### NixOS
**Status**: ⚠️ Requires environment setup

Add to your `flake.nix` or `shell.nix`:
```nix
buildInputs = [
  glib  # for gdbus
  python3
];
```

Or set `LD_LIBRARY_PATH`:
```bash
export LD_LIBRARY_PATH=${glib}/lib:$LD_LIBRARY_PATH
```

### Flatpak
**Status**: ✅ Works via portal (no setup needed)

Notifications automatically use `org.freedesktop.portal.Notification`. No manifest permissions required.

```bash
flatpak run com.example.NASync notifications --test
```

---

## Troubleshooting

### No notification daemon found

**Problem**: `❌ No notification daemon here (looked for gdbus and notify-send).`

**Solution**: Install one:

**Ubuntu/Debian:**
```bash
sudo apt install libnotify-bin  # adds notify-send
# OR
sudo apt install glib2.0        # adds gdbus (usually already installed)
```

**Fedora/RHEL:**
```bash
sudo dnf install libnotify       # adds notify-send
# OR
sudo dnf install glib2           # adds gdbus (usually already installed)
```

**Arch:**
```bash
sudo pacman -S libnotify         # adds notify-send
# OR
sudo pacman -S glib2             # adds gdbus (usually already installed)
```

### Notifications enabled but not appearing

**Problem**: `nasync notifications` shows "on", but nothing appears when syncing.

**Possible causes:**

1. **Notification daemon not running**
   - Check if your desktop's notification service is running
   - For systemd systems: `systemctl --user status notification-daemon`
   - Restart your desktop session

2. **Do Not Disturb enabled**
   - Check your desktop's settings
   - Critical notifications (sync failures) should still appear

3. **App not associated with NASync icon**
   - Ensure `nasync-icon` or `nasync` is in your icon theme
   - Fall back: install a generic icon or use notification daemon's app settings

### Testing without a running desktop

If you're testing on a server or in a headless environment:

```bash
# Start a minimal notification daemon
dbus-daemon --session &
notification-daemon &

# Now test
nasync notifications --test
```

---

## How Notifications Work

NASync uses three backends (in order of preference):

### 1. **gdbus** (preferred)
- Part of glib2 (on every desktop Linux system)
- Uses `org.freedesktop.Notifications` D-Bus interface
- **Feature**: Notification replacement by ID
  - "Syncing…" message is replaced by "Sync complete"
  - One notification per sync run (clean, non-spammy)

### 2. **notify-send** (fallback)
- Part of libnotify (many systems, but optional)
- Command-line tool that sends notifications
- **Limitation**: No message replacement
  - "Syncing…" and "Sync complete" both appear
  - Minor UI clutter, no functional impact

### 3. **Portal** (Flatpak only)
- `org.freedesktop.portal.Notification`
- Used automatically inside a Flatpak sandbox
- No extra manifest permission needed

### None available
- Syncs complete silently (no error)
- Perfect for servers and headless systems

---

## Configuration

### Enable/Disable Notifications

**CLI:**
```bash
nasync notifications on       # enable
nasync notifications off      # disable
nasync notifications          # check status
```

**GUI:**
Settings → Notifications toggle

**Persisted**: Changes are saved in `~/.config/nasync/config.json`

### Notification Types

NASync sends three types:

1. **Sync started**: `"Syncing…"` (replaced when sync ends)
2. **Sync complete**: Shows file counts
3. **Sync failed** (critical): Stays on screen until dismissed
   - Includes error reason (away from home, server down, etc.)

---

## For Developers

### Testing notifications

**Unit tests:**
```bash
python -m pytest tests/test_notify.py -v
```

**Integration tests (with automation):**
```bash
python -m pytest tests/test_automation.py::test_a_background_sync_announces_itself -v
```

### Stubbing the notification system

For testing without a real daemon:

```python
from nasync.system.notify import Notifier

# Disable notifications in tests
notifier = Notifier(enabled=False)

# Or provide a fake sender
sent = []
notifier = Notifier(sender=lambda *a, **k: sent.append(a) or 1)
```

### Adding a new notification

In automation or CLI code:

```python
from nasync.system.notify import Notifier

notifier = Notifier(enabled=config.get_notifications())
notifier.notify("Sync complete", "12 files moved", urgency="normal")
```

The ID is tracked automatically for replacement.

---

## Further Reading

- **README.md**: Feature overview and usage
- **NOTIFICATION_AUDIT.md**: Implementation details and test results
- `nasync/system/notify.py`: Full implementation with docstrings
