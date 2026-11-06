# Architecture

## How NASync talks to the server

NASync speaks the **native SMB/CIFS protocol** — the same one your file
manager uses for `smb://192.168.1.100/`. It does **not** use SSH and does
**not** shell out to any external tool: it talks SMB 2/3 directly from Python
via [`smbprotocol`](https://github.com/jborean93/smbprotocol).

```
nasync (Python)
   └─ smbprotocol                      (pure-Python SMB 2/3 client)
        └─ SMB/CIFS over TCP port 445  →  your server's Samba share
```

- **One connection per sync.** A session is negotiated once and reused for
  every file in the run, which keeps large directory trees fast.
- **Comparing costs one request per directory, not per file.** Each file's
  size and modification time arrive in the directory listing, so NASync reads them there
  rather than asking about each file in turn: a 3,000-file share is 121
  requests instead of 3,241. Every sync pays this cost, including the ones
  with nothing to do.
- **Transfers move in 1 MiB chunks**, which is what makes per-byte progress
  and mid-transfer cancellation possible.
- **Containers are transparent.** If your server runs Samba in Docker, NASync
  connects to the host's published `IP:445`, exactly like your file manager.

**Why this design:**

- ✅ **No external binary** — nothing to install beyond NASync itself, and it
  works inside a Flatpak sandbox, where `smbclient` isn't present.
- ✅ **Cancellable, with real progress** — a sync can stop between chunks,
  and one large file reports smooth progress instead of jumping 0% → 100%.
- ✅ **Arbitrary filenames** — quotes and newlines work, with nothing to
  escape for a subprocess.
- ⚠️ **Trade-off:** SMB is a Python dependency rather than a system one, so
  protocol fixes arrive by upgrading the package.

## How the code is laid out

```
nasync/
  core/       the engine — nothing here knows which front-end is calling
    config.py       config.json + the encrypted .creds under ~/.config/nasync
    samba.py        SMB 2/3: indexing, transfers, browsing, usage reports
    srvsvc.py       listing a server's shares (the SRVSVC RPC service)
    sync.py         Syncer — compares two trees and moves what differs
    state.py        the sync record (sync-state.db)
    activity.py     which sync is running, across processes — the one-at-a-time lock
    automation.py   the live and scheduled triggers, and the loop running them
  system/     how NASync fits the machine it runs on
    portal.py       XDG Desktop Portal — Background, Notification, Secret
    service.py      background syncing: a systemd user unit, or the portal
    notify.py       desktop notifications
    vault.py        encrypts saved passwords, keyed from the portal or keyring
    lan.py          "is the NAS on this network?" — the gate on automatic syncing
    launcher.py     the .desktop entry and icon
  gui/        the desktop app (GTK4 + libadwaita)
  cli.py      the command line — the reference surface for every feature
  format.py   presentation helpers both front-ends share
tests/        unit tests, one file per module
  lab/          data-safety lab: the real engine against a throwaway Samba container
  e2e/          end-to-end: commands, the background service and the real window
packaging/    Flatpak manifest, desktop entry and AppStream metadata
scripts/      install, test and screenshot helpers
docs/         this documentation
```

`core/` depends on nothing above it. `system/` is the part that decides which
permissions a Flatpak build has to ask for, which is why it's grouped rather
than scattered. `cli.py` and `gui/` are two callers of the same engine.

What the engine promises about your files, and how each promise is tested, is
in [Data safety](data-safety.md).
