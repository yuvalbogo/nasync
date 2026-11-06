# Troubleshooting

First, `nasync status` — it reports the active server, where each pair
resolves, how passwords are stored, what automatic syncing is doing, and
whether the app launcher and background service can start.

## Connecting

### "Connection refused" or the server can't be reached

- Check the server address and port (`nasync servers`, default port `445`).
- Check the NAS is on and on the same network.
- Check a firewall isn't blocking SMB (TCP port 445).

`nasync test-connection` checks reachability, credentials and the share in
turn, and says which one failed.

### "Authentication failed"

- Check the username and password on the NAS itself.
- Re-enter them in the app's server editor, or run `nasync init` again for
  that server.
- If NASync says your **keyring is locked**, unlock it (log in to the desktop
  session, or open Seahorse) — the password may be right all along.

### "This server needs SMB 3"

You turned [encryption](security.md#encrypting-the-connection) on and the
server can't do it. NASync refuses rather than sending files in the clear.
Enable SMB 3 on the NAS, or turn encryption off: `nasync encryption off`.

## The app won't open

### "The desktop app needs PyGObject with GTK4 and libadwaita"

Install them from your distribution (see [Installation](install.md#with-the-desktop-app-linux)).
If NASync lives in a virtualenv, it must be created with
`--system-site-packages` to see them. The CLI needs none of this.

### Clicking the launcher does nothing, or shows "NASync can't start"

If the copy of NASync the launcher points at was moved or deleted, or a
Python upgrade broke its virtualenv, clicking the icon shows a notification
saying why and how to fix it. The output of the last launch is in
`~/.local/state/nasync/gui.log`, `nasync status` reports the same, and
opening the app any other way (`nasync gui`) repairs the launcher.

### Search + Enter opens NASync, but clicking its grid icon does nothing

GNOME is holding a stale copy of that icon from before the launcher last
changed: search looks the app up afresh, but a grid icon keeps what it had
when it was drawn. Drag the icon out of its folder and back, or log out and
in. Installs made with `make install-user` keep the launcher fixed, so this
shouldn't recur after updating to one.

## Automatic syncing

### Nothing syncs automatically

```bash
nasync service status    # installed? running? can it start?
nasync live-sync         # is a trigger on for this server?
nasync scheduled-sync
```

- **Paused** in the app means *Run in the background* is off in App settings.
- Live sync only runs on the **NAS's own network**; away from home it waits.
- A pip/pipx install uses a systemd **user** service, which stops at logout
  unless you run `loginctl enable-linger $USER`.
- If the service points at a copy of NASync that no longer runs,
  `nasync service status` says so and opening the app repairs it.

### A background sync failed with an unhelpful message

Run the service with tracebacks turned on:

```bash
systemctl --user edit nasync-auto.service   # add: Environment=NASYNC_DEBUG=1
```

## Notifications don't appear

`nasync notifications --test` sends one immediately and names the transport
it used. If it reports success and nothing appears, the desktop is accepting
the notification and then discarding it — silently, so nothing in NASync's
output will say so. Watch the bus to see what happens to it:

```bash
dbus-monitor --session "interface='org.freedesktop.Notifications'"
```

A `NotificationClosed` signal arriving milliseconds after the `Notify` call
means the shell threw it away. GNOME Shell 50 did this to every notification
carrying a `desktop-entry` hint, which NASync no longer sends (0.18.1).

## Still stuck?

Check [existing issues](https://github.com/YuvalBogo/nasync/issues), or
[open a new one](https://github.com/YuvalBogo/nasync/issues/new?template=bug_report.md)
with the output of `nasync --version` and `nasync status`.
