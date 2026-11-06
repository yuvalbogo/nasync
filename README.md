<img src="nasync/assets/nasync_logo.png" width="96" align="right" alt="NASync logo"/>

# NASync

### Sync your NAS with class.

Back up your local folders to a Samba/SMB home NAS — from the command line
(`nasync`) or a native GNOME desktop app. Linux first; the command line also
runs on macOS.

![The NASync main window](docs/screenshots/gui-main.png)

## Features

- **Mirror or update** — make the NAS match your folders, or only ever add to it
- **Hard to lose data with** — a [sync root](docs/syncing.md#the-sync-root)
  fences off the rest of the share, files changed on the NAS are kept, and
  runs that look like a mistake [wait for you](docs/syncing.md#what-nasync-refuses-to-do)
- **Automatic** — live sync while you're on your home network, or a daily
  schedule that catches up on missed runs
- **Secure** — saved passwords encrypted with your keyring; optional SMB 3
  encryption that refuses to fall back to plaintext
- **Nothing else to install** — speaks SMB 2/3 directly from Python
- **Server tools** — browse the share, see what's using space, find duplicates

## Install

```bash
pipx install git+https://github.com/YuvalBogo/nasync.git
```

The desktop app also needs GTK4 and libadwaita 1.5+ from your distribution —
see [Installation](docs/install.md).

## Quick start

```bash
nasync setup                         # add your NAS, pick a share, choose folders
nasync sync --dry-run                # see what a sync would do
nasync sync                          # back up
nasync restore                       # bring files back (never deletes locally)
nasync scheduled-sync on --at 02:00  # back up every night
nasync gui                           # or do all of it in the desktop app
```

Full walkthrough: [Getting started](docs/getting-started.md).

## Documentation

- [Installation](docs/install.md)
- [Getting started](docs/getting-started.md)
- [How syncing works](docs/syncing.md)
- [Automatic syncing](docs/automation.md)
- [Desktop app](docs/desktop-app.md)
- [Security](docs/security.md)
- [Server tools](docs/server-tools.md)
- [Command reference](docs/cli-reference.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Data safety](docs/data-safety.md)
- [Architecture](docs/architecture.md)

## Contributing

Bug reports and ideas go in [GitHub Issues](https://github.com/YuvalBogo/nasync/issues).
To work on the code, see [CONTRIBUTING.md](CONTRIBUTING.md). Release notes are
in [CHANGELOG.md](CHANGELOG.md).

## License

MIT — see [LICENSE](LICENSE). Built on
[smbprotocol](https://github.com/jborean93/smbprotocol),
[Click](https://click.palletsprojects.com/),
[watchdog](https://github.com/gorakhargosh/watchdog) and
[cryptography](https://cryptography.io/).
